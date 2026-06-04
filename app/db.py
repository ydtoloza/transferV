from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.config import database_path
from app.models import AppSettings, TransferCreate, TransferRecord, TransferStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(database_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torrent_hash TEXT NOT NULL,
                torrent_name TEXT NOT NULL,
                source_path TEXT NOT NULL,
                destination_path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_hash_active
            ON transfers(torrent_hash)
            WHERE status IN ('pending', 'waiting', 'transferring')
            """
        )


def get_settings() -> AppSettings:
    with connect() as conn:
        row = conn.execute("SELECT json FROM settings WHERE id = 1").fetchone()
    if not row:
        return AppSettings()
    return AppSettings.model_validate_json(row["json"])


def save_settings(settings: AppSettings) -> AppSettings:
    payload = settings.model_dump_json()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (id, json, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET json = excluded.json, updated_at = excluded.updated_at
            """,
            (payload, utc_now()),
        )
    return settings


def row_to_transfer(row: sqlite3.Row) -> TransferRecord:
    data = dict(row)
    data["status"] = TransferStatus(data["status"])
    return TransferRecord(**data)


def list_transfers(limit: int = 5000) -> list[TransferRecord]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transfers ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_transfer(row) for row in rows]


def active_hashes() -> set[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT torrent_hash FROM transfers
            WHERE status IN ('pending', 'waiting', 'transferring')
            """
        ).fetchall()
    return {row["torrent_hash"] for row in rows}


def create_transfer(item: TransferCreate, settings: AppSettings) -> TransferRecord:
    now = utc_now()
    destination = item.destination_path or settings.destination_path
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO transfers (
                torrent_hash, torrent_name, source_path, destination_path, size,
                status, message, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                item.torrent_hash,
                item.torrent_name,
                item.source_path,
                destination,
                item.size,
                TransferStatus.pending.value,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM transfers WHERE id = last_insert_rowid()"
        ).fetchone()
    return row_to_transfer(row)


def update_transfer(
    transfer_id: int,
    status: TransferStatus,
    message: str = "",
    started: bool = False,
    completed: bool = False,
) -> None:
    now = utc_now()
    fields = ["status = ?", "message = ?", "updated_at = ?"]
    params: list[str | int | None] = [status.value, message, now]
    if started:
        fields.append("started_at = COALESCE(started_at, ?)")
        params.append(now)
    if completed:
        fields.append("completed_at = ?")
        params.append(now)
    params.append(transfer_id)
    with connect() as conn:
        conn.execute(
            f"UPDATE transfers SET {', '.join(fields)} WHERE id = ?",
            params,
        )


def delete_transfer(transfer_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM transfers WHERE id = ?",
            (transfer_id,),
        )
    return cursor.rowcount > 0


def next_pending_transfer() -> TransferRecord | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM transfers
            WHERE status IN ('pending', 'waiting')
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
    return row_to_transfer(row) if row else None


def get_transfer(transfer_id: int) -> TransferRecord | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    return row_to_transfer(row) if row else None
