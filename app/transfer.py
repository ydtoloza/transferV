from __future__ import annotations

import asyncio
import shlex
import re
import os
import posixpath

from app.models import AppSettings, SshSettings, TransferMode, TransferRecord


class TransferError(RuntimeError):
    pass


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def ssh_options(settings: SshSettings) -> list[str]:
    options = [
        "-p",
        str(settings.port),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ServerAliveInterval=30",
    ]
    if settings.auth_method == "key" and settings.key_path:
        options.extend(["-i", settings.key_path])
    return options


def ssh_prefix(settings: SshSettings) -> list[str]:
    command: list[str] = []
    if settings.auth_method == "password" and settings.password:
        command.extend(["sshpass", "-p", settings.password])
    command.append("ssh")
    command.extend(ssh_options(settings))
    return command


def rsync_ssh_arg(settings: SshSettings) -> str:
    return "ssh " + " ".join(shell_quote(part) for part in ssh_options(settings))


def remote_ref(settings: SshSettings, path: str) -> str:
    if not settings.host or not settings.username:
        raise TransferError("SSH host and username are required.")
    return f"{settings.username}@{settings.host}:{path}"


def rsync_base(settings: AppSettings, ssh_settings: SshSettings) -> list[str]:
    command: list[str] = []
    if ssh_settings.auth_method == "password" and ssh_settings.password:
        command.extend(["sshpass", "-p", ssh_settings.password])
    command.append("rsync")
    command.extend(shlex.split(settings.rsync_args))
    command.extend(["-e", rsync_ssh_arg(ssh_settings)])
    return command


def build_local_pull(settings: AppSettings, transfer: TransferRecord) -> list[str]:
    command = rsync_base(settings, settings.vps_ssh)
    command.append(remote_ref(settings.vps_ssh, transfer.source_path))
    command.append(ensure_trailing_slash(transfer.destination_path))
    return command


def build_remote_push_inner(settings: AppSettings, transfer: TransferRecord) -> str:
    command = rsync_base(settings, settings.destination_ssh)
    command.append(transfer.source_path)
    command.append(remote_ref(settings.destination_ssh, transfer.destination_path))
    return " ".join(shell_quote(part) for part in command)


def build_remote_push(settings: AppSettings, transfer: TransferRecord) -> list[str]:
    if not settings.vps_ssh.host or not settings.vps_ssh.username:
        raise TransferError("VPS SSH settings are required for remote push.")
    command = ssh_prefix(settings.vps_ssh)
    command.append(f"{settings.vps_ssh.username}@{settings.vps_ssh.host}")
    command.append(build_remote_push_inner(settings, transfer))
    return command


def build_orchestrated_pull_inner(settings: AppSettings, transfer: TransferRecord) -> str:
    command = rsync_base(settings, settings.vps_ssh)
    command.append(remote_ref(settings.vps_ssh, transfer.source_path))
    command.append(ensure_trailing_slash(transfer.destination_path))
    return " ".join(shell_quote(part) for part in command)


def build_orchestrated_pull(settings: AppSettings, transfer: TransferRecord) -> list[str]:
    if not settings.destination_ssh.host or not settings.destination_ssh.username:
        raise TransferError("Destination SSH settings are required for orchestrated pull.")
    command = ssh_prefix(settings.destination_ssh)
    command.append(f"{settings.destination_ssh.username}@{settings.destination_ssh.host}")
    command.append(build_orchestrated_pull_inner(settings, transfer))
    return command


def ensure_trailing_slash(path: str) -> str:
    return path if path.endswith("/") else f"{path}/"


def build_transfer_command(settings: AppSettings, transfer: TransferRecord) -> list[str]:
    if settings.transfer_mode == TransferMode.local_pull:
        return build_local_pull(settings, transfer)
    if settings.transfer_mode == TransferMode.orchestrated_pull:
        return build_orchestrated_pull(settings, transfer)
    if settings.transfer_mode == TransferMode.remote_push:
        return build_remote_push(settings, transfer)
    raise TransferError(f"Unsupported transfer mode: {settings.transfer_mode}")


SPEED_RE = re.compile(r"([\d.]+)\s*([KMGT])?B/s")


def ensure_progress_args(rsync_args: str) -> str:
    """Guarantee flags needed to parse progress in real time over pipes."""
    args = shlex.split(rsync_args)
    for extra in ("--info=progress2", "--outbuf=Line"):
        if extra not in args:
            args.append(extra)
    return " ".join(shlex.quote(a) for a in args)


def parse_progress(text: str) -> tuple[int, str]:
    """Extract percent and transfer speed from an rsync progress line."""
    match = re.search(r"(\d+)%", text)
    pct = int(match.group(1)) if match else -1
    speed = ""
    speed_match = SPEED_RE.search(text)
    if speed_match:
        value = float(speed_match.group(1))
        unit = speed_match.group(2) or ""
        speed = f"{value:g}{unit}B/s"
    return pct, speed


async def run_transfer(settings: AppSettings, transfer: TransferRecord) -> str:
    from app.db import update_transfer
    from app.models import TransferStatus

    settings = settings.model_copy(
        update={"rsync_args": ensure_progress_args(settings.rsync_args)}
    )
    command = build_transfer_command(settings, transfer)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError) as exc:
        raise TransferError(f"Failed to start rsync: {exc}") from exc

    output_chunks: list[str] = []
    last_pct = -1
    buffer = b""

    while True:
        chunk = await process.stdout.read(65536)
        if not chunk:
            break
        buffer += chunk
        lines = buffer.split(b"\r")
        buffer = lines.pop()
        for line in lines:
            line = line.strip(b"\n \t")
            if not line:
                continue
            text = line.decode("utf-8", errors="replace").strip()
            output_chunks.append(text)
            if len(output_chunks) > 200:
                output_chunks.pop(0)
            pct, speed = parse_progress(text)
            if pct >= 0 and pct != last_pct:
                last_pct = pct
                message = f"{pct}%" + (f" · {speed}" if speed else "")
                update_transfer(
                    transfer.id,
                    TransferStatus.transferring,
                    message,
                    started=True,
                )

    if buffer.strip():
        output_chunks.append(buffer.decode("utf-8", errors="replace").strip())

    await process.wait()
    output = "\n".join(output_chunks[-25:])

    if process.returncode != 0:
        raise TransferError(output or f"rsync exited with code {process.returncode}")
    return output

async def verify_destination(settings: AppSettings, transfer: TransferRecord) -> bool:
    target_name = posixpath.basename(transfer.source_path.rstrip("/"))
    dest_path = posixpath.join(transfer.destination_path, target_name)
    
    if settings.transfer_mode == TransferMode.local_pull:
        return os.path.exists(dest_path)
        
    if not settings.destination_ssh.host or not settings.destination_ssh.username:
        return False
        
    cmd = ssh_prefix(settings.destination_ssh)
    cmd.extend([f"{settings.destination_ssh.username}@{settings.destination_ssh.host}", "test", "-e", shell_quote(dest_path)])
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    return process.returncode == 0

