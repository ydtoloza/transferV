from app.models import AppSettings, SshSettings, TransferMode, TransferRecord, TransferStatus
from app.transfer import build_transfer_command, ensure_progress_args, parse_progress, summarize_output


def record(dest="/media/in"):
    return TransferRecord(
        id=1, torrent_hash="h", torrent_name="My Movie", source_path="/dl/My Movie", destination_path=dest,
        status=TransferStatus.pending, created_at="", updated_at="",
    )


def settings(mode):
    return AppSettings(
        transfer_mode=mode,
        vps_ssh=SshSettings(host="vps", username="u", key_path="/k1"),
        destination_ssh=SshSettings(host="dst", username="d", key_path="/k2"),
    )


def test_local_pull_runs_rsync_here_with_multiplexing():
    cmd = build_transfer_command(settings(TransferMode.local_pull), record())
    assert cmd[0] == "rsync"
    assert any("ControlMaster=auto" in part for part in cmd)
    assert cmd[-2:] == ["u@vps:/dl/My Movie", "/media/in/"]


def test_orchestrated_runs_on_destination_with_tty():
    cmd = build_transfer_command(settings(TransferMode.orchestrated_pull), record())
    assert cmd[:2] == ["ssh", "-tt"]
    assert "d@dst" in cmd
    assert "u@vps:" in cmd[-1] and cmd[-1].endswith("/media/in/")


def test_remote_push_destination_has_trailing_slash():
    cmd = build_transfer_command(settings(TransferMode.remote_push), record("/media/in"))
    assert cmd[:2] == ["ssh", "-tt"]
    assert cmd[-1].endswith("d@dst:/media/in/")


def test_password_is_not_on_the_local_command_line():
    s = settings(TransferMode.local_pull)
    s.vps_ssh.auth_method, s.vps_ssh.password = "password", "secret"
    cmd = build_transfer_command(s, record())
    assert cmd[:2] == ["sshpass", "-e"]
    assert "secret" not in " ".join(cmd)


def test_partial_becomes_hidden_partial_dir():
    args = ensure_progress_args("-a --partial --protect-args")
    assert "--partial-dir=.rsync-partial" in args
    assert " --partial " not in f" {args} "
    assert "--info=progress2" in args and "--outbuf=Line" in args


def test_parse_progress():
    assert parse_progress("  1,234,567  45%   12.5MB/s    0:01:00") == (45, "12.5MB/s")
    assert parse_progress("sending incremental file list") == (-1, "")


def test_summarize_output():
    assert "Ya estaba" in summarize_output("0   0%    0.00kB/s    0:00:00 (xfr#0, to-chk=0/1)")
    assert "Ya estaba" in summarize_output("")
    real = "2,140,309,780 100%   41.55MB/s    0:00:49 (xfr#1, to-chk=0/1)"
    assert summarize_output(real) == real


def test_segmented_command_orchestrated():
    from app.transfer import build_segmented_command
    s = settings(TransferMode.orchestrated_pull)
    t = record("/mnt/storage/downloads")
    t.source_path = "/srv/seedbox/It's a Movie.mkv"
    cmd, env = build_segmented_command(s, t, 4)
    assert cmd[:2] == ["ssh", "-tt"] and env is None
    script = cmd[-1]
    assert "ControlMaster" not in script          # one real TCP connection per segment
    assert "N=4" in script and ".rsync-partial" in script
    assert "u@vps" in script
