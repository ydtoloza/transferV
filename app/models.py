from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TransferMode(str, Enum):
    local_pull = "local_pull"
    orchestrated_pull = "orchestrated_pull"
    remote_push = "remote_push"


class AuthMethod(str, Enum):
    key = "key"
    password = "password"


class TransferStatus(str, Enum):
    pending = "pending"
    waiting = "waiting"
    transferring = "transferring"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class SshSettings(BaseModel):
    host: str = ""
    port: int = 22
    username: str = ""
    auth_method: AuthMethod = AuthMethod.key
    password: str = ""
    key_path: str = ""


class QbitSettings(BaseModel):
    url: str = ""
    username: str = ""
    password: str = ""
    downloads_path: str = "/downloads"
    host_downloads_path: str = ""


class WebhookSettings(BaseModel):
    enabled: bool = False
    url: str = ""
    method: str = "POST"
    headers_json: str = "{}"
    body_template: str = (
        '{"text":"TransferV: {{status}} - {{torrent_name}} -> {{destination_path}}"}'
    )


class AppSettings(BaseModel):
    transfer_mode: TransferMode = TransferMode.local_pull
    poll_interval_seconds: int = Field(default=30, ge=10, le=3600)
    destination_path: str = "/media/imports"
    vps_ssh: SshSettings = Field(default_factory=SshSettings)
    destination_ssh: SshSettings = Field(default_factory=SshSettings)
    qbit: QbitSettings = Field(default_factory=QbitSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    rsync_args: str = "-a --partial --info=progress2 --protect-args"


class TorrentFile(BaseModel):
    name: str
    size: int = 0
    progress: float = 0


class Torrent(BaseModel):
    hash: str
    name: str
    state: str = ""
    progress: float = 0
    size: int = 0
    save_path: str = ""
    content_path: str = ""
    files: list[TorrentFile] = Field(default_factory=list)
    queued: bool = False


class TransferCreate(BaseModel):
    torrent_hash: str
    torrent_name: str
    source_path: str
    destination_path: str | None = None
    size: int = 0


class TransferRecord(BaseModel):
    id: int
    torrent_hash: str
    torrent_name: str
    source_path: str
    destination_path: str
    size: int = 0
    status: TransferStatus
    message: str = ""
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None


class ApiMessage(BaseModel):
    ok: bool
    message: str
    data: Any | None = None
