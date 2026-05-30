from __future__ import annotations

from pathlib import PurePosixPath

import httpx

from app.models import AppSettings, Torrent, TorrentFile


class QbitError(RuntimeError):
    pass


class QbitClient:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.base_url = settings.qbit.url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    async def __aenter__(self) -> "QbitClient":
        if not self.base_url:
            raise QbitError("qBittorrent URL is not configured.")
        await self.login()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.client.aclose()

    async def login(self) -> None:
        response = await self.client.post(
            f"{self.base_url}/api/v2/auth/login",
            data={
                "username": self.settings.qbit.username,
                "password": self.settings.qbit.password,
            },
        )
        if response.text.strip() != "Ok.":
            raise QbitError("qBittorrent login failed.")

    async def torrents(self, queued_hashes: set[str] | None = None) -> list[Torrent]:
        response = await self.client.get(f"{self.base_url}/api/v2/torrents/info")
        response.raise_for_status()
        queued_hashes = queued_hashes or set()
        items = []
        for raw in response.json():
            torrent_hash = raw.get("hash", "")
            files = await self.files(torrent_hash)
            source_path = map_source_path(
                infer_source_path(raw, files),
                self.settings.qbit.downloads_path,
                self.settings.qbit.host_downloads_path,
            )
            tracker = extract_tracker(raw.get("tracker", "") or raw.get("trackers_count", ""))
            items.append(
                Torrent(
                    hash=torrent_hash,
                    name=raw.get("name", ""),
                    state=raw.get("state", ""),
                    progress=raw.get("progress", 0),
                    size=raw.get("size", 0),
                    save_path=raw.get("save_path", ""),
                    content_path=source_path,
                    files=files,
                    queued=torrent_hash in queued_hashes,
                    added_on=raw.get("added_on", 0),
                    tracker=tracker,
                )
            )
        # Sort by added_on descending (newest first)
        items.sort(key=lambda t: t.added_on, reverse=True)
        return items

    async def torrent(self, torrent_hash: str) -> Torrent | None:
        response = await self.client.get(
            f"{self.base_url}/api/v2/torrents/info",
            params={"hashes": torrent_hash},
        )
        response.raise_for_status()
        items = response.json()
        if not items:
            return None
        files = await self.files(torrent_hash)
        raw = items[0]
        source_path = map_source_path(
            infer_source_path(raw, files),
            self.settings.qbit.downloads_path,
            self.settings.qbit.host_downloads_path,
        )
        tracker = extract_tracker(raw.get("tracker", "") or "")
        return Torrent(
            hash=raw.get("hash", ""),
            name=raw.get("name", ""),
            state=raw.get("state", ""),
            progress=raw.get("progress", 0),
            size=raw.get("size", 0),
            save_path=raw.get("save_path", ""),
            content_path=source_path,
            files=files,
            added_on=raw.get("added_on", 0),
            tracker=tracker,
        )

    async def files(self, torrent_hash: str) -> list[TorrentFile]:
        response = await self.client.get(
            f"{self.base_url}/api/v2/torrents/files",
            params={"hash": torrent_hash},
        )
        response.raise_for_status()
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
