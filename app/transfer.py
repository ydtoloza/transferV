from __future__ import annotations

import asyncio
import shlex

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
    command = build_transfer_command(settings, transfer)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output_bytes, _ = await process.communicate()
    output = output_bytes.decode("utf-8", errors="replace")
    if process.returncode != 0:
        tail = "\n".join(output.splitlines()[-25:])
        raise TransferError(tail or f"rsync exited with code {process.returncode}")
    return "\n".join(output.splitlines()[-25:])

