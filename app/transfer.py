from __future__ import annotations

import asyncio
import contextlib
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


def ssh_command(settings: SshSettings, remote_command: str, tty: bool = False) -> list[str]:
    # tty=True: the remote command gets SIGHUP and dies if this connection
    # drops (e.g. TransferV restarts), instead of running on orphaned.
    flags = ["-tt"] if tty else []
    return [*sshpass_prefix(settings), "ssh", *flags, *ssh_options(settings), target(settings), remote_command]


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
        return ssh_command(settings.destination_ssh, inner, tty=True)
    if mode == TransferMode.remote_push:
        # The VPS runs rsync and pushes to the destination host.
        inner = quote_all([
            *rsync_base(settings, settings.destination_ssh, multiplex=False),
            transfer.source_path,
            f"{target(settings.destination_ssh)}:{ensure_trailing_slash(transfer.destination_path)}",
        ])
        return ssh_command(settings.vps_ssh, inner, tty=True)
    raise TransferError(f"Unsupported transfer mode: {mode}")


# ── progress parsing ───────────────────────────────────────

PCT_RE = re.compile(r"(\d+)%")
LINE_SPLIT_RE = re.compile(rb"[\r\n]")
SPEED_RE = re.compile(r"([\d.]+)\s*([KMGT])?B/s")


def ensure_progress_args(rsync_args: str) -> str:
    """Guarantee flags needed to parse progress in real time over pipes."""
    args = shlex.split(rsync_args)
    # --partial would leave an interrupted file under its final name, where
    # Plex/qBittorrent see it as complete. Keep partial data in a hidden dir
    # instead (implies --partial; a later run resumes from it).
    args = ["--partial-dir=.rsync-partial" if a == "--partial" else a for a in args]
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


async def _stream_process(
    command: list[str],
    env: dict[str, str] | None,
    on_progress: Callable[[str], None],
) -> tuple[int, str, str]:
    """Run a command, report rsync-style progress lines, return (code, output tail, stderr tail)."""
    with tempfile.TemporaryFile() as err:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=err,
                start_new_session=True,  # own process group: cancel kills every child
            )
        except (OSError, ValueError) as exc:
            raise TransferError(f"No se pudo iniciar la transferencia: {exc}") from exc

        tail: list[str] = []
        last_pct, last_emit = -1, 0.0
        buffer = b""
        try:
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
            await process.wait()
        except asyncio.CancelledError:
            # Shutdown: stop it now (whole group); the restart requeues and resumes it.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, 9)
            raise

        if buffer.strip():
            tail.append(buffer.decode("utf-8", errors="replace").strip())
        return process.returncode, "\n".join(tail[-25:]), _read_tail(err)


async def run_transfer(
    settings: AppSettings,
    transfer: TransferRecord,
    on_progress: Callable[[str], None],
    streams: int = 1,
    on_fallback: Callable[[], None] = lambda: None,
) -> str:
    """Copy one torrent. With streams > 1, a single large file is pulled in parallel
    segments (one SSH connection each); anything else goes through rsync."""
    if streams > 1 and can_segment(settings, transfer):
        command, env = build_segmented_command(settings, transfer, streams)
        code, output, errors = await _stream_process(command, env, on_progress)
        if code == 0:
            return output
        if code != SEGMENT_FALLBACK:
            raise TransferError("\n".join(x for x in (output, errors) if x) or f"Transferencia por segmentos falló ({code})")
        # A folder, or smaller than it looked: plain rsync below, one connection.
        on_fallback()

    settings = settings.model_copy(update={"rsync_args": ensure_progress_args(settings.rsync_args)})
    command = build_transfer_command(settings, transfer)
    # Whichever host the local process authenticates against.
    local_host = settings.vps_ssh if settings.transfer_mode != TransferMode.orchestrated_pull else settings.destination_ssh
    code, output, errors = await _stream_process(command, local_env(local_host), on_progress)
    if code != 0:
        raise TransferError("\n".join(x for x in (output, errors) if x) or f"rsync exited with code {code}")
    return output


# ── segmented pull (one big file, N SSH connections) ───────

SEGMENT_MODES = (TransferMode.local_pull, TransferMode.orchestrated_pull)
SEGMENT_MIN_BYTES = 1 << 30
SEGMENT_FALLBACK = 99  # script exit code: "not a single big file, use rsync"

_SEGMENT_SCRIPT = r"""
set -u -o pipefail
trap 'trap - EXIT; kill 0 2>/dev/null' EXIT HUP INT TERM
SSH=(__SSH__)
N=__N__
INFO=$("${SSH[@]}" __STAT__) || { echo "No se pudo leer el origen en el servidor origen" >&2; exit 3; }
[ "$INFO" = dir ] && { trap - EXIT; exit __FALLBACK__; }
SIZE=${INFO%% *}; MTIME=${INFO##* }
[ "$SIZE" -lt __MIN__ ] && { trap - EXIT; exit __FALLBACK__; }
mkdir -p __PARTDIR__ || exit 4
TMP=__TMP__
: > "$TMP" || exit 4
CH=$(( SIZE / 1048576 / N + 1 ))
pids=()
for i in $(seq 0 $((N - 1))); do
  ( "${SSH[@]}" "__DD__ $((i * CH)) $CH" | dd of="$TMP" bs=1M seek=$((i * CH)) conv=notrunc iflag=fullblock status=none ) &
  pids+=($!)
done
start=$(date +%s); last=0; lt=$start
while :; do
  alive=0
  for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && alive=1; done
  done_b=$(du -B1 "$TMP" 2>/dev/null | cut -f1); done_b=${done_b:-0}
  now=$(date +%s); dt=$(( now - lt )); [ "$dt" -lt 1 ] && dt=1
  pct=$(( done_b * 100 / SIZE )); [ "$pct" -gt 99 ] && pct=99
  echo "$done_b $pct% $(awk -v r=$(( (done_b - last) / dt )) 'BEGIN{printf "%.2f", r/1048576}')MB/s"
  last=$done_b; lt=$now
  [ "$alive" = 0 ] && break
  sleep 2
done
rc=0
for p in "${pids[@]}"; do wait "$p" || rc=1; done
[ "$rc" = 0 ] || { echo "Falló uno de los segmentos" >&2; exit 5; }
GOT=$(stat -c %s "$TMP")
[ "$GOT" = "$SIZE" ] || { echo "Tamaño incorrecto: $GOT de $SIZE bytes" >&2; exit 6; }
touch -d "@$MTIME" "$TMP" && mv -f "$TMP" __FINAL__ || exit 7
secs=$(( $(date +%s) - start )); [ "$secs" -lt 1 ] && secs=1
trap - EXIT
echo "$SIZE 100% $(awk -v r=$(( SIZE / secs )) 'BEGIN{printf "%.2f", r/1048576}')MB/s ${secs}s (xfr#1, $N segmentos)"
"""


def can_segment(settings: AppSettings, transfer: TransferRecord) -> bool:
    return settings.transfer_mode in SEGMENT_MODES and transfer.size >= SEGMENT_MIN_BYTES


def build_segmented_command(settings: AppSettings, transfer: TransferRecord, streams: int) -> tuple[list[str], dict | None]:
    """Command (and local env) that pulls one file in `streams` byte ranges in parallel.

    It runs where rsync would run in that mode: here (local pull) or on the
    destination (orchestrated). Each segment opens its own SSH connection (no
    multiplexing), because the per-connection window is exactly the limit.
    """
    vps = settings.vps_ssh
    remote = settings.transfer_mode == TransferMode.orchestrated_pull
    src = transfer.source_path.rstrip("/")
    name = posixpath.basename(src)
    dest_dir = transfer.destination_path.rstrip("/") or "/"
    qsrc = shlex.quote(src)
    ssh_words = [*sshpass_prefix(vps, remote=remote), "ssh", *ssh_options(vps, multiplex=False), target(vps)]
    stat_cmd = f"if [ -f {qsrc} ]; then stat -c '%s %Y' -- {qsrc}; else echo dir; fi"
    dd_cmd = "sh -c " + shlex.quote('dd if="$0" bs=1M skip="$1" count="$2" status=none') + " " + qsrc
    script = (
        _SEGMENT_SCRIPT
        .replace("__SSH__", quote_all(ssh_words))
        .replace("__N__", str(max(2, streams)))
        .replace("__STAT__", shlex.quote(stat_cmd))
        .replace("__FALLBACK__", str(SEGMENT_FALLBACK))
        .replace("__MIN__", str(SEGMENT_MIN_BYTES))
        .replace("__PARTDIR__", shlex.quote(f"{dest_dir}/.rsync-partial"))
        .replace("__TMP__", shlex.quote(f"{dest_dir}/.rsync-partial/{name}.tvseg"))
        .replace("__DD__", dd_cmd.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$"))
        .replace("__FINAL__", shlex.quote(f"{dest_dir}/{name}"))
    )
    if remote:
        return ssh_command(settings.destination_ssh, "bash -c " + shlex.quote(script), tty=True), None
    return ["bash", "-c", script], local_env(vps)


XFR_RE = re.compile(r"xfr#(\d+)")


def summarize_output(output: str) -> str:
    """rsync's last line, or a plain message when nothing had to be copied."""
    counts = [int(n) for n in XFR_RE.findall(output or "")]
    if not counts or max(counts) == 0:
        return "Ya estaba en el destino; no hubo que copiar nada."
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
