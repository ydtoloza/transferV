from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from app import db, events
from app.models import AppSettings, TransferCreate, TransferRecord, TransferStatus
from app.qbit import QbitClient, is_complete
from app.transfer import TransferError, destination_file, paths_exist, run_transfer
from app.webhook import send_webhook

log = logging.getLogger("transferv.worker")

ACTIVE = (TransferStatus.pending, TransferStatus.waiting, TransferStatus.transferring)


class TransferWorker:
    """Single background loop: processes the queue, then (rarely) verifies the
    destination with one batched SSH call. Sleeps poll_interval or until woken."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._last_verify = 0.0

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    def wake(self, verify: bool = False) -> None:
        if verify:
            self._last_verify = 0.0
        self._wake.set()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            settings = db.get_settings()
            try:
                await process_queue(settings)
            except Exception:
                log.exception("Queue processing failed")

            interval = settings.verify_interval_minutes * 60
            if interval and time.monotonic() - self._last_verify >= interval:
                self._last_verify = time.monotonic()
                try:
                    await verify_destination(settings)
                except Exception:
                    log.exception("Destination verification failed")

            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), settings.poll_interval_seconds)
            self._wake.clear()


# ── queue ──────────────────────────────────────────────────

def set_status(transfer: TransferRecord, status: TransferStatus, message: str) -> None:
    """Write only when something changed, so idle cycles don't spam SSE clients."""
    if transfer.status != status or transfer.message != message:
        db.update_transfer(transfer.id, status, message)


async def process_queue(settings: AppSettings) -> None:
    queued = db.list_by_status(TransferStatus.pending, TransferStatus.waiting)
    if not queued:
        return

    try:
        async with QbitClient(settings) as qbit:
            torrents = {t.hash: t for t in await qbit.torrents(hashes=[t.torrent_hash for t in queued])}
    except Exception as exc:
        for transfer in queued:
            set_status(transfer, TransferStatus.waiting, f"Esperando a qBittorrent: {exc}")
        return

    # Every ready transfer runs this cycle; incomplete ones no longer block the queue.
    for transfer in queued:
        torrent = torrents.get(transfer.torrent_hash)
        if not torrent:
            await finish(settings, transfer, TransferStatus.failed, "El torrent ya no existe.")
        elif not is_complete(torrent):
            set_status(
                transfer,
                TransferStatus.waiting,
                f"Esperando que termine la descarga: {round(torrent.progress * 100, 1)}%",
            )
        else:
            await execute(db.get_settings(), transfer)


async def execute(settings: AppSettings, transfer: TransferRecord) -> None:
    db.update_transfer(transfer.id, TransferStatus.transferring, "Iniciando…", started=True)
    current = db.get_transfer(transfer.id) or transfer

    def on_progress(message: str) -> None:
        # Progress goes straight to the UI; no full-list refetch per percent.
        db.update_transfer(transfer.id, TransferStatus.transferring, message, publish=False)
        events.broker.publish("progress", {"id": transfer.id, "message": message})

    try:
        output = await run_transfer(settings, current, on_progress)
    except TransferError as exc:
        await finish(settings, current, TransferStatus.failed, str(exc))
        return
    await finish(settings, current, TransferStatus.completed, output or "Transferencia completada.")


async def finish(settings: AppSettings, transfer: TransferRecord, status: TransferStatus, message: str) -> None:
    db.update_transfer(transfer.id, status, message, completed=True)
    updated = db.get_transfer(transfer.id) or transfer
    try:
        await send_webhook(settings, updated, status, message)
    except Exception:
        log.exception("Webhook failed")
    events.broker.publish("torrents")


# ── destination verification ───────────────────────────────

async def verify_destination(settings: AppSettings) -> None:
    """One SSH round-trip checks every known transfer plus untracked finished torrents."""
    records = db.list_by_status(TransferStatus.completed, TransferStatus.missing)
    known = db.hashes_with_status(TransferStatus.completed, TransferStatus.missing, *ACTIVE)

    candidates = []
    try:
        async with QbitClient(settings) as qbit:
            candidates = [
                t for t in await qbit.torrents()
                if is_complete(t) and t.hash not in known and t.content_path
            ]
    except Exception as exc:
        log.warning("qBittorrent unavailable during verification: %s", exc)

    record_paths = {r.id: destination_file(r.source_path, r.destination_path) for r in records}
    candidate_paths = {t.hash: destination_file(t.content_path, settings.destination_path) for t in candidates}
    if not record_paths and not candidate_paths:
        return

    exists = await paths_exist(settings, [*record_paths.values(), *candidate_paths.values()])
    if exists is None:
        # Connection failed: do NOT mark anything missing. Try again next interval.
        log.warning("Destination check skipped: SSH connection failed")
        return

    changed = False
    for record in records:
        found = exists.get(record_paths[record.id])
        if record.status == TransferStatus.completed and found is False:
            db.update_transfer(record.id, TransferStatus.missing, "El archivo ya no existe en el destino", publish=False)
            changed = True
        elif record.status == TransferStatus.missing and found:
            db.update_transfer(record.id, TransferStatus.completed, "Encontrado nuevamente en el destino", publish=False)
            changed = True

    for torrent in candidates:
        if exists.get(candidate_paths[torrent.hash]):
            db.upsert_completed_transfer(
                TransferCreate(
                    torrent_hash=torrent.hash,
                    torrent_name=torrent.name,
                    source_path=torrent.content_path,
                    destination_path=settings.destination_path,
                    size=torrent.size,
                ),
                settings,
            )
            changed = True

    if changed:
        events.broker.publish("transfers")
        events.broker.publish("torrents")
