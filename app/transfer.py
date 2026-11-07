from __future__ import annotations

import asyncio
import os
import posixpath
import re
import shlex
import tempfile
import time
from typing import Callable, Iterable

from app.models import AppSettings, SshSettings, TransferMode, TransferRecord


class TransferError(RuntimeError):
    pass


# Shared SSH connections: every ssh/rsync launched from this process reuses one
# TCP+auth session per host instead of opening a new one per command. This is
# what keeps the remote sshd from being flooded with handshakes.
CONTROL_DIR = os.getenv("TRANSFERV_SSH_CONTROL_DIR", "/tmp")
CONTROL_PERSIST = os.getenv("TRANSFERV_SSH_PERSIST", "10m")


def ssh_options(settings: SshSettings, multiplex: bool = True) -> list[str]:
    options = [
        "-p", str(settings.port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",  # keep "Permanently added…" noise out of error messages
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]
    if multiplex:
        options += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={CONTROL_DIR}/tv-%C",
            "-o", f"ControlPersist={CONTROL_PERSIST}",
        ]
    if settings.auth_method == "key":
        options += ["-o", "BatchMode=yes"]
        if settings.key_path:
            options += ["-i", settings.key_path]
    return options


def uses_password(settings: SshSettings) -> bool:
    return settings.auth_method == "password" and bool(settings.password)


def sshpass_prefix(settings: SshSettings, remote: bool = False) -> list[str]:
    """Locally the password goes through $SSHPASS so it never shows in `ps`.
    For commands executed on another host there is no env to pass, so -p is used."""
    if not uses_password(settings):
        return []
    return ["sshpass", "-p", settings.password] if remote else ["sshpass", "-e"]


def local_env(*hosts: SshSettings) -> dict[str, str] | None:
    """Environment for a local subprocess that may need $SSHPASS."""
    for host in hosts:
        if uses_password(host):
            return {**os.environ, "SSHPASS": host.password}
    return None


def target(settings: SshSettings) -> str:
    if not settings.host or not settings.username:
        raise TransferError("SSH host and username are required.")
    return f"{settings.username}@{settings.host}"


def ssh_command(settings: SshSettings, remote_command: str) -> list[str]:
    return [*sshpass_prefix(settings), "ssh", *ssh_options(settings), target(settings), remote_command]


def is_configured(settings: SshSettings) -> bool:
    return bool(settings.host and settings.username)


def _read_tail(fh, limit: int = 4000) -> str:
    fh.seek(0)
    return fh.read()[-limit:].decode("utf-8", "replace").strip()


async def run_ssh(
    settings: SshSettings,
    remote_command: str,
    stdin: bytes | None = None,
    timeout: float = 30,
) -> tuple[int, str]:
    """Run one command over the shared SSH connection. Returns (exit code, output).

    stderr goes to a temp file, not a pipe: a ControlPersist master forked by this
    ssh may inherit stderr and would keep a pipe open for minutes.
    """
    with tempfile.TemporaryFile() as err:
        process = await asyncio.create_subprocess_exec(
            *ssh_command(settings, remote_command),
            env=local_env(settings),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=err,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return 255, "timeout"
        text = (out or b"").decode("utf-8", "replace")
        if process.returncode != 0:
            text = _read_tail(err) or text
        return process.returncode, text


# ── rsync command building ─────────────────────────────────

def rsync_base(settings: AppSettings, ssh: SshSettings, multiplex: bool) -> list[str]:
    # multiplex=False means rsync runs on another host (no shared socket there).
    ssh_arg = "ssh " + " ".join(shlex.quote(p) for p in ssh_options(ssh, multiplex))
    return [*sshpass_prefix(ssh, remote=not multiplex), "rsync", *shlex.split(settings.rsync_args), "-e", ssh_arg]


def ensure_trailing_slash(path: str) -> str:
    return path if path.endswith("/") else f"{path}/"


def quote_all(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)


def build_transfer_command(settings: AppSettings, transfer: TransferRecord) -> list[str]:
    mode = settings.transfer_mode
    if mode == TransferMode.local_pull:
        # rsync runs here and reuses our multiplexed connection to the VPS.
        return [
            *rsync_base(settings, settings.vps_ssh, multiplex=True),
            f"{target(settings.vps_ssh)}:{transfer.source_path}",
            ensure_trailing_slash(transfer.destination_path),
        ]
    if mode == TransferMode.orchestrated_pull:
        # The destination host runs rsync against the VPS.
        inner = quote_all([
            *rsync_base(settings, settings.vps_ssh, multiplex=False),
            f"{target(settings.vps_ssh)}:{transfer.source_path}",
            ensure_trailing_slash(transfer.destination_path),
        ])
        return ssh_command(settings.destination_ssh, inner)
    if mode == TransferMode.remote_push:
        # The VPS runs rsync and pushes to the destination host.
        inner = quote_all([
            *rsync_base(settings, settings.destination_ssh, multiplex=False),
            transfer.source_path,
            f"{target(settings.destination_ssh)}:{transfer.destination_path}",
        ])
        return ssh_command(settings.vps_ssh, inner)
    raise TransferError(f"Unsupported transfer mode: {mode}")


# ── progress parsing ───────────────────────────────────────

PCT_RE = re.compile(r"(\d+)%")
LINE_SPLIT_RE = re.compile(rb"[\r\n]")
SPEED_RE = re.compile(r"([\d.]+)\s*([KMGT])?B/s")


def ensure_progress_args(rsync_args: str) -> str:
    """Guarantee flags needed to parse progress in real time over pipes."""
    args = shlex.split(rsync_args)
    for extra in ("--info=progress2", "--outbuf=Line"):
        if extra not in args:
            args.append(extra)
    return quote_all(args)


def parse_progress(text: str) -> tuple[int, str]:
    match = PCT_RE.search(text)
    pct = int(match.group(1)) if match else -1
    speed = ""
    speed_match = SPEED_RE.search(text)
    if speed_match:
        speed = f"{float(speed_match.group(1)):g}{speed_match.group(2) or ''}B/s"
    return pct, speed


async def run_transfer(
    settings: AppSettings,
    transfer: TransferRecord,
    on_progress: Callable[[str], None],
) -> str:
    settings = settings.model_copy(update={"rsync_args": ensure_progress_args(settings.rsync_args)})
    command = build_transfer_command(settings, transfer)
    # Whichever host the local process authenticates against.
    local_host = settings.vps_ssh if settings.transfer_mode != TransferMode.orchestrated_pull else settings.destination_ssh

    with tempfile.TemporaryFile() as err:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=local_env(local_host),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=err,
            )
        except (OSError, ValueError) as exc:
            raise TransferError(f"Failed to start rsync: {exc}") from exc

        tail: list[str] = []
        last_pct, last_emit = -1, 0.0
        buffer = b""

        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = LINE_SPLIT_RE.split(buffer)
            for raw in lines:
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                tail.append(text)
                del tail[:-50]
                pct, speed = parse_progress(text)
                now = time.monotonic()
                # Throttle: at most one update per second, and only when % changes.
                if pct >= 0 and pct != last_pct and now - last_emit >= 1:
                    last_pct, last_emit = pct, now
                    on_progress(f"{pct}%" + (f" · {speed}" if speed else ""))

        if buffer.strip():
            tail.append(buffer.decode("utf-8", errors="replace").strip())

        await process.wait()
        output = "\n".join(tail[-25:])
        if process.returncode != 0:
            errors = _read_tail(err)
            raise TransferError(
                "\n".join(x for x in (output, errors) if x)
                or f"rsync exited with code {process.returncode}"
            )
        return output


# ── destination checks (batched) ───────────────────────────

def destination_file(source_path: str, destination_path: str) -> str:
    return posixpath.join(destination_path, posixpath.basename(source_path.rstrip("/")))


# Reads one path per line on stdin and prints 1/0 per line: N checks, 1 connection.
_EXISTS_SCRIPT = "sh -c " + shlex.quote(
    'while IFS= read -r p; do if [ -e "$p" ]; then echo 1; else echo 0; fi; done'
)


async def paths_exist(settings: AppSettings, paths: list[str]) -> dict[str, bool] | None:
    """Check many destination paths at once. Returns None if the check could not run."""
    paths = [p for p in dict.fromkeys(paths) if p and "\n" not in p]
    if not paths:
        return {}

    if settings.transfer_mode == TransferMode.local_pull:
        return {p: os.path.exists(p) for p in paths}

    if not is_configured(settings.destination_ssh):
        return None
    payload = ("\n".join(paths) + "\n").encode()
    code, out = await run_ssh(settings.destination_ssh, _EXISTS_SCRIPT, stdin=payload, timeout=60)
    results = out.split()
    if code != 0 or len(results) != len(paths):
        return None
    return {p: r == "1" for p, r in zip(paths, results)}
