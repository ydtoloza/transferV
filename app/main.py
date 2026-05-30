from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.models import ApiMessage, AppSettings, TransferCreate, TransferRecord
from app.qbit import QbitClient
from app.worker import TransferWorker, process_next_transfer


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
    return [torrent.model_dump() for torrent in torrents]


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


@app.post("/api/worker/run-once", response_model=ApiMessage)
async def run_worker_once() -> ApiMessage:
    await process_next_transfer()
    return ApiMessage(ok=True, message="Worker cycle completed.")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


app.mount("/static", StaticFiles(directory=static_dir), name="static")

