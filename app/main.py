from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import db, events
from app.models import ApiMessage, AppSettings, SshSettings, TransferCreate, TransferMode, TransferRecord, TransferStatus
from app.qbit import QbitClient
from app.transfer import is_configured, quote_all, run_ssh, ssh_options, sshpass_prefix, target
from app.worker import TransferWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

worker = TransferWorker()
static_dir = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    worker.start()
    yield
    await worker.stop()


app = FastAPI(title="TransferV", lifespan=lifespan)


# ── small TTL cache shared by every browser tab ────────────

class TtlCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self.value = None
        self.at = 0.0
        self.lock = asyncio.Lock()

    def clear(self) -> None:
        self.at = 0.0

    async def get(self, compute, force: bool = False):
        async with self.lock:  # concurrent callers share one computation
            if force or time.monotonic() - self.at > self.ttl:
                self.value = await compute()
                self.at = time.monotonic()
            return self.value


status_cache = TtlCache(ttl=60)
torrents_cache = TtlCache(ttl=4)


# ── settings ───────────────────────────────────────────────

@app.get("/api/settings", response_model=AppSettings)
async def get_settings() -> AppSettings:
    return db.get_settings()


@app.put("/api/settings", response_model=AppSettings)
async def put_settings(settings: AppSettings) -> AppSettings:
    saved = db.save_settings(settings)
    status_cache.clear()
    torrents_cache.clear()
    worker.wake()
    return saved


# ── torrents / transfers ───────────────────────────────────

async def _load_torrents() -> list[dict]:
    settings = db.get_settings()
    async with QbitClient(settings) as qbit:
        torrents = await qbit.torrents(db.active_hashes())
    completed = db.hashes_with_status(TransferStatus.completed)
    result = []
    for torrent in torrents:
        data = torrent.model_dump()
        if torrent.hash in completed:
            data["transfer_status"] = "completed"
        result.append(data)
    return result


@app.get("/api/torrents")
async def get_torrents() -> list[dict]:
    try:
        return await torrents_cache.get(_load_torrents)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/transfers", response_model=TransferRecord)
async def post_transfer(item: TransferCreate) -> TransferRecord:
    try:
        record = db.create_transfer(item, db.get_settings())
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    torrents_cache.clear()
    worker.wake()
    return record


@app.get("/api/transfers", response_model=list[TransferRecord])
async def get_transfers(limit: int = 1000) -> list[TransferRecord]:
    return db.list_transfers(limit=min(max(limit, 1), 5000))


@app.delete("/api/transfers/{transfer_id}", response_model=ApiMessage)
async def delete_transfer(transfer_id: int) -> ApiMessage:
    if not db.delete_transfer(transfer_id):
        raise HTTPException(status_code=404, detail="Transfer not found.")
    torrents_cache.clear()
    return ApiMessage(ok=True, message="Transfer deleted.")


@app.post("/api/worker/run-once", response_model=ApiMessage)
async def run_worker_once() -> ApiMessage:
    worker.wake()
    return ApiMessage(ok=True, message="Ciclo solicitado.")


@app.post("/api/worker/verify", response_model=ApiMessage)
async def run_verify() -> ApiMessage:
    worker.wake(verify=True)
    return ApiMessage(ok=True, message="Verificación solicitada.")


# ── real-time events ───────────────────────────────────────

@app.get("/api/events")
async def events_stream(request: Request) -> StreamingResponse:
    queue = events.broker.subscribe()

    async def stream():
        try:
            yield "retry: 4000\n\n"
            while not await request.is_disconnected():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                payload = json.dumps(item["data"]) if item["data"] is not None else "{}"
                yield f"event: {item['event']}\ndata: {payload}\n\n"
        finally:
            events.broker.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── connection status ──────────────────────────────────────

async def _compute_status() -> dict:
    settings = db.get_settings()

    async def check_qbit() -> bool:
        try:
            async with QbitClient(settings) as qbit:
                await qbit._get("/api/v2/app/version")  # a real authenticated request
                return True
        except Exception:
            return False

    async def check_ssh(ssh: SshSettings, via: SshSettings | None = None) -> dict:
        """Check `ssh` from where the transfer actually connects: directly, or
        from the `via` host (the second hop of orchestrated/remote-push modes)."""
        if not is_configured(ssh) or (via is not None and not is_configured(via)):
            return {"ok": False, "error": "Sin configurar"}
        if via is None:
            code, out = await run_ssh(ssh, "true", timeout=20)
        else:
            hop = quote_all([*sshpass_prefix(ssh, remote=True), "ssh", *ssh_options(ssh, multiplex=False), target(ssh), "true"])
            code, out = await run_ssh(via, hop, timeout=30)
        return {"ok": code == 0, "error": "" if code == 0 else out[-300:]}

    mode = settings.transfer_mode
    vps, dest = settings.vps_ssh, settings.destination_ssh
    checks = {"vps": check_ssh(vps)}
    if mode == TransferMode.orchestrated_pull:
        checks = {"destination": check_ssh(dest), "vps": check_ssh(vps, via=dest)}
    elif mode == TransferMode.remote_push:
        checks = {"vps": check_ssh(vps), "destination": check_ssh(dest, via=vps)}
    results = await asyncio.gather(check_qbit(), *checks.values())
    status: dict = {"qbit": results[0], "vps": None, "destination": None}
    status.update(dict(zip(checks, results[1:])))
    return status


@app.get("/api/health")
async def health() -> dict:
    """Liveness only: touches neither qBittorrent nor SSH (used by the Docker healthcheck)."""
    return {"ok": True}


@app.get("/api/status")
async def get_status(force: bool = False) -> dict:
    return await status_cache.get(_compute_status, force=force)


@app.post("/api/system/restart-ssh/{target}", response_model=ApiMessage)
async def restart_ssh(target: str) -> ApiMessage:
    settings = db.get_settings()
    if target not in ("vps", "destination"):
        raise HTTPException(status_code=400, detail="Destino inválido")
    ssh = settings.vps_ssh if target == "vps" else settings.destination_ssh
    if not is_configured(ssh):
        raise HTTPException(status_code=400, detail="SSH no configurado")
    code, out = await run_ssh(ssh, "sudo systemctl restart ssh || sudo systemctl restart sshd", timeout=20)
    if code != 0:
        raise HTTPException(status_code=500, detail=f"Exit {code}: {out}")
    status_cache.clear()
    return ApiMessage(ok=True, message=f"SSH reiniciado en {target}")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


app.mount("/static", StaticFiles(directory=static_dir), name="static")
