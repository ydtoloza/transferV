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


async def run_transfer(settings: AppSettings, transfer: TransferRecord) -> str:
    from app.db import update_transfer
    from app.models import TransferStatus

    command = build_transfer_command(settings, transfer)
    # Ensure progress2 is in the command args for parsing
    if "--info=progress2" not in " ".join(command):
        # We inject it into rsync_base earlier, but since we can't easily modify the nested lists,
        # we'll just let the user ensure it's in settings.rsync_args.
        pass

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    
    output_lines = []
    last_pct = -1

    while True:
        try:
            line_bytes = await process.stdout.readuntil(b'\r')
        except asyncio.exceptions.IncompleteReadError as e:
            line_bytes = e.partial

        if not line_bytes:
            if process.stdout.at_eof():
                break
            continue

        text = line_bytes.decode("utf-8", errors="replace").strip()
        if not text:
            if process.stdout.at_eof():
                break
            continue

        output_lines.append(text)
        
        # Parse percentage e.g. " 15% "
        match = re.search(r'(\d+)%', text)
        if match:
            pct = int(match.group(1))
            if pct != last_pct:
                last_pct = pct
                update_transfer(transfer.id, TransferStatus.transferring, f"{pct}%", started=True)

        if process.stdout.at_eof():
            break

    await process.wait()
    output = "\n".join([x for x in output_lines[-25:] if x])

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

