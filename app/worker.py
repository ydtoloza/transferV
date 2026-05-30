from __future__ import annotations

import asyncio
from contextlib import suppress

from app import db
from app.models import TransferStatus
from app.qbit import QbitClient, is_complete
from app.transfer import TransferError, run_transfer, verify_destination
from app.webhook import send_webhook
import time
from app.webhook import send_webhook


class TransferWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        last_verify = 0.0
        while not self._stop.is_set():
            settings = db.get_settings()
            try:
                await process_next_transfer()
            except Exception:
                pass
                
            # Verify missing files every 10 minutes
            if time.time() - last_verify > 600:
                try:
                    await verify_missing_transfers(settings)
                except Exception:
                    pass
                last_verify = time.time()
                
            await asyncio.sleep(settings.poll_interval_seconds)

async def verify_missing_transfers(settings) -> None:
    transfers = db.list_transfers(limit=1000)
    completed = [t for t in transfers if t.status == TransferStatus.completed]
    for t in completed:
        exists = await verify_destination(settings, t)
        if not exists:
            db.update_transfer(t.id, TransferStatus.missing, "El archivo ya no existe en el destino")


async def process_next_transfer() -> None:
    transfer = db.next_pending_transfer()
    if not transfer:
        return

    settings = db.get_settings()
    try:
        async with QbitClient(settings) as qbit:
            torrent = await qbit.torrent(transfer.torrent_hash)
    except Exception as exc:
        db.update_transfer(
            transfer.id,
            TransferStatus.waiting,
            f"Waiting for qBittorrent: {exc}",
        )
        return

    if not torrent:
        db.update_transfer(transfer.id, TransferStatus.failed, "Torrent no longer exists.", completed=True)
        await notify(settings, transfer, TransferStatus.failed, "Torrent no longer exists.")
        return

    if not is_complete(torrent):
        percent = round(torrent.progress * 100, 2)
        db.update_transfer(
            transfer.id,
            TransferStatus.waiting,
            f"Waiting for torrent completion: {percent}%",
        )
        return

    db.update_transfer(transfer.id, TransferStatus.transferring, "Transfer started.", started=True)
    current = db.get_transfer(transfer.id) or transfer
    try:
        output = await run_transfer(settings, current)
    except TransferError as exc:
        message = str(exc)
        db.update_transfer(transfer.id, TransferStatus.failed, message, completed=True)
        updated = db.get_transfer(transfer.id) or current
        await notify(settings, updated, TransferStatus.failed, message)
        return

    message = output or "Transfer completed."
    db.update_transfer(transfer.id, TransferStatus.completed, message, completed=True)
    updated = db.get_transfer(transfer.id) or current
    await notify(settings, updated, TransferStatus.completed, message)


async def notify(settings, transfer, status: TransferStatus, message: str) -> None:
    with suppress(Exception):
        await send_webhook(settings, transfer, status, message)
