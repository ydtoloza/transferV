from __future__ import annotations

import asyncio
import time
from pathlib import PurePosixPath

import httpx

from app.models import AppSettings, Torrent, TorrentFile


class QbitError(RuntimeError):
    pass


# After a failed login, wait this long before trying again. qBittorrent bans
# the client IP after a few failures (default: 5 tries -> 1 hour ban).
LOGIN_BACKOFF = 300


class _Session:
    """One logged-in HTTP session per (url, user, password), shared by every caller."""

    def __init__(self, base_url: str, api_key: str = "") -> None:
        headers = {"Referer": base_url}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers)
        self.api_key = bool(api_key)
        self.logged_in = bool(api_key)  # API key: every request is already authenticated
        self.failed_at = 0.0
        self.error = ""
        self.lock = asyncio.Lock()
        # sync/maindata state: after the first full snapshot qBittorrent only
        # sends what changed since `rid`.
        self.rid = 0
        self.raw: dict[str, dict] = {}
        self.sync_lock = asyncio.Lock()


_sessions: dict[tuple[str, str, str, str], _Session] = {}


def _session_for(base_url: str, username: str, password: str, api_key: str) -> _Session:
    key = (base_url, username, password, api_key)
    session = _sessions.get(key)
    if session is None:
        # New credentials: forget old sessions (and their back-off) entirely.
        for old in _sessions.values():
            asyncio.ensure_future(old.client.aclose())
        _sessions.clear()
        session = _sessions[key] = _Session(base_url, api_key)
    return session


class QbitClient:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.base_url = settings.qbit.url.rstrip("/")
        q = settings.qbit
        self.session = _session_for(self.base_url, q.username, q.password, q.api_key.strip())
        self.client = self.session.client

    async def __aenter__(self) -> "QbitClient":
        if not self.base_url:
            raise QbitError("qBittorrent URL is not configured.")
        if not self.session.logged_in:
            await self.login()
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass  # the session is reused across requests

    async def login(self) -> None:
        s = self.session
        async with s.lock:
            if s.logged_in:
                return
            if self.settings.qbit.username and not self.settings.qbit.password:
                # Never send an empty password: every failure counts toward a ban.
                raise QbitError("Falta la contraseña de qBittorrent.")
            wait = LOGIN_BACKOFF - (time.monotonic() - s.failed_at)
            if s.failed_at and wait > 0:
                raise QbitError(f"{s.error} Reintento en {int(wait)} s.")
            response = await self.client.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.settings.qbit.username, "password": self.settings.qbit.password},
            )
            if response.text.strip() == "Ok.":
                s.logged_in, s.failed_at, s.error = True, 0.0, ""
                return
            s.failed_at = time.monotonic()
            if response.status_code == 403:
                s.error = "qBittorrent bloqueó esta IP por intentos fallidos (reinícialo o espera a que expire)."
            elif response.status_code == 401:
                s.error = "qBittorrent rechazó la petición (revisa la URL: Host/CSRF)."
            else:
                s.error = "qBittorrent rechazó el usuario o la contraseña."
            raise QbitError(s.error)

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        response = await self.client.get(f"{self.base_url}{path}", params=params)
        if self.session.api_key and response.status_code in (401, 403):
            raise QbitError("qBittorrent rechazó la API key.")
        if response.status_code == 403:  # session expired: log in once and retry
            self.session.logged_in = False
            await self.login()
            response = await self.client.get(f"{self.base_url}{path}", params=params)
        response.raise_for_status()
        return response

    async def torrents(
        self,
        queued_hashes: set[str] | None = None,
        hashes: list[str] | None = None,
    ) -> list[Torrent]:
        raws = await self._raw_torrents()
        if hashes:
            wanted = set(hashes)
            raws = [r for r in raws if r.get("hash") in wanted]
        queued_hashes = queued_hashes or set()
        items = await asyncio.gather(*(
            self._build_torrent(raw, raw.get("hash", "") in queued_hashes)
            for raw in raws
        ))
        return sorted(items, key=lambda t: t.added_on, reverse=True)

    async def _raw_torrents(self) -> list[dict]:
        """Current torrent list, kept up to date with incremental sync/maindata."""
        s = self.session
        async with s.sync_lock:
            try:
                data = (await self._get("/api/v2/sync/maindata", {"rid": s.rid})).json()
                if data.get("full_update"):
                    s.raw = {}
                for torrent_hash, fields in (data.get("torrents") or {}).items():
                    s.raw.setdefault(torrent_hash, {"hash": torrent_hash}).update(fields)
                for torrent_hash in data.get("torrents_removed") or []:
                    s.raw.pop(torrent_hash, None)
                s.rid = int(data.get("rid", 0))
                return [dict(r) for r in s.raw.values()]
            except QbitError:
                raise
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                # Older qBittorrent or a hiccup: start over with the plain list.
                s.rid, s.raw = 0, {}
                return (await self._get("/api/v2/torrents/info")).json()

    async def _build_torrent(self, raw: dict, queued: bool = False) -> Torrent:
        torrent_hash = raw.get("hash", "")
        files: list[TorrentFile] = []
        content_path = raw.get("content_path") or ""
        if not content_path:
            files = await self.files(torrent_hash)
        source_path = map_source_path(
            infer_source_path(raw, files),
            self.settings.qbit.downloads_path,
            self.settings.qbit.host_downloads_path,
        )
        tracker = extract_tracker(raw.get("tracker", "") or "")
        return Torrent(
            hash=torrent_hash,
            name=raw.get("name", ""),
            state=raw.get("state", ""),
            progress=raw.get("progress", 0),
            size=raw.get("size", 0),
            save_path=raw.get("save_path", ""),
            content_path=source_path,
            files=files,
            queued=queued,
            added_on=raw.get("added_on", 0),
            tracker=tracker,
            num_seeds=raw.get("num_seeds", 0),
            num_complete=raw.get("num_complete", 0),
            num_leechs=raw.get("num_leechs", 0),
            num_incomplete=raw.get("num_incomplete", 0),
            dlspeed=raw.get("dlspeed", 0),
            upspeed=raw.get("upspeed", 0),
            eta=raw.get("eta", 0),
            ratio=raw.get("ratio", 0),
        )

    async def files(self, torrent_hash: str) -> list[TorrentFile]:
        response = await self._get("/api/v2/torrents/files", {"hash": torrent_hash})
        return [
            TorrentFile(
                name=item.get("name", ""),
                size=item.get("size", 0),
                progress=item.get("progress", 0),
            )
            for item in response.json()
        ]


def extract_tracker(tracker_url: str) -> str:
    """Extract a clean tracker domain name from a tracker URL."""
    if not tracker_url or not isinstance(tracker_url, str):
        return ""
    try:
        # Remove protocol
        url = tracker_url.strip()
        for prefix in ("https://", "http://", "udp://"):
            if url.startswith(prefix):
                url = url[len(prefix):]
                break
        # Get domain only
        domain = url.split("/")[0].split(":")[0]
        # Remove www.
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def infer_source_path(raw: dict, files: list[TorrentFile]) -> str:
    content_path = raw.get("content_path") or ""
    if content_path:
        return content_path

    save_path = raw.get("save_path") or ""
    name = raw.get("name") or ""
    if len(files) == 1 and files[0].name:
        return str(PurePosixPath(save_path) / files[0].name)
    return str(PurePosixPath(save_path) / name)


def map_source_path(path: str, qbit_path: str, host_path: str) -> str:
    if not path or not qbit_path or not host_path:
        return path
    normalized_qbit = qbit_path.rstrip("/")
    normalized_host = host_path.rstrip("/")
    if path == normalized_qbit:
        return normalized_host
    if path.startswith(normalized_qbit + "/"):
        return normalized_host + path[len(normalized_qbit):]
    return path


def is_complete(torrent: Torrent) -> bool:
    return torrent.progress >= 1 or torrent.state in {
        "uploading",
        "stalledUP",
        "queuedUP",
        "checkingUP",
        "forcedUP",
    }
