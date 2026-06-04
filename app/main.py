from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.models import ApiMessage, AppSettings, TransferCreate, TransferRecord
from app.qbit import QbitClient
from app.worker import TransferWorker, process_next_transfer
import asyncio
import subprocess


app = FastAPI(title="TransferV")
worker = TransferWorker()
static_dir = Path(__file__).parent / "static"


@app.on_event("startup")
async def startup() -> None:
    db.init_db()
    worker.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await worker.stop()


@app.get("/api/settings", response_model=AppSettings)
async def get_settings() -> AppSettings:
    return db.get_settings()


@app.put("/api/settings", response_model=AppSettings)
async def put_settings(settings: AppSettings) -> AppSettings:
    return db.save_settings(settings)


@app.get("/api/torrents")
async def get_torrents() -> list[dict]:
    settings = db.get_settings()
    try:
        async with QbitClient(settings) as qbit:
            torrents = await qbit.torrents(db.active_hashes())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
        
    transfers = db.list_transfers()
    completed_hashes = {t.torrent_hash for t in transfers if t.status.value == "completed"}
    
    result = []
    for torrent in torrents:
        data = torrent.model_dump()
        if torrent.hash in completed_hashes:
            data["transfer_status"] = "completed"
        result.append(data)
    return result


@app.post("/api/transfers", response_model=TransferRecord)
async def post_transfer(item: TransferCreate) -> TransferRecord:
    settings = db.get_settings()
    try:
        return db.create_transfer(item, settings)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/transfers", response_model=list[TransferRecord])
async def get_transfers() -> list[TransferRecord]:
    return db.list_transfers()


@app.delete("/api/transfers/{transfer_id}", response_model=ApiMessage)
async def delete_transfer(transfer_id: int) -> ApiMessage:
    deleted = db.delete_transfer(transfer_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Transfer not found.")
    return ApiMessage(ok=True, message="Transfer deleted.")


@app.post("/api/worker/run-once", response_model=ApiMessage)
async def run_worker_once() -> ApiMessage:
    await process_next_transfer()
    return ApiMessage(ok=True, message="Worker cycle completed.")


@app.get("/api/status")
async def get_status() -> dict:
    settings = db.get_settings()
    
    # Check QBit
    qbit_ok = False
    try:
        async with QbitClient(settings) as qbit:
            qbit_ok = True
    except Exception:
        pass

    async def check_ssh(ssh_settings):
        if not ssh_settings.host or not ssh_settings.username:
            return False
        try:
            from app.transfer import ssh_prefix
            cmd = ssh_prefix(ssh_settings)
            cmd.extend([f"{ssh_settings.username}@{ssh_settings.host}", "echo", "1"])
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(process.communicate(), timeout=10.0)
            return process.returncode == 0
        except Exception:
            return False

    vps_ok = await check_ssh(settings.vps_ssh)
    dest_ok = await check_ssh(settings.destination_ssh)

    return {
        "qbit": qbit_ok,
        "vps": vps_ok,
        "destination": dest_ok
    }

@app.post("/api/system/restart-ssh/{target}", response_model=ApiMessage)
async def restart_ssh(target: str) -> ApiMessage:
    settings = db.get_settings()
    ssh_settings = settings.vps_ssh if target == "vps" else settings.destination_ssh
    if not ssh_settings.host or not ssh_settings.username:
        raise HTTPException(status_code=400, detail="SSH no configurado")

    try:
        from app.transfer import ssh_prefix
        cmd = ssh_prefix(ssh_settings)
        # Force sudo restart sshd
        cmd.extend([
            f"{ssh_settings.username}@{ssh_settings.host}", 
            "sudo systemctl restart ssh || sudo systemctl restart sshd"
        ])
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(process.communicate(), timeout=15.0)
        if process.returncode != 0:
            raise Exception(f"Exit {process.returncode}: {err.decode('utf-8', 'replace')}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    return ApiMessage(ok=True, message=f"SSH reiniciado en {target}")

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


app.mount("/static", StaticFiles(directory=static_dir), name="static")
