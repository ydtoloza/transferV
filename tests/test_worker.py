import asyncio

import pytest

from app import db, worker
from app.models import AppSettings, Torrent, TransferCreate, TransferMode, TransferStatus

GIB = 1 << 30


def queue(sizes, s):
    return [
        db.create_transfer(TransferCreate(torrent_hash=f"h{i}", torrent_name=f"T{i}", source_path=f"/s/{i}", size=size), s).id
        for i, size in enumerate(sizes)
    ]


def test_requeue_interrupted():
    s = AppSettings()
    (tid,) = queue([0], s)
    db.update_transfer(tid, TransferStatus.transferring, "16%")
    assert db.requeue_interrupted() == 1
    assert db.get_transfer(tid).status == TransferStatus.waiting


@pytest.fixture
def fake(monkeypatch):
    """Fake qBittorrent (everything complete) and a run_transfer that blocks until released."""
    state = {"started": {}, "gate": None}

    class FakeQbit:
        def __init__(self, _settings): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): ...
        async def torrents(self, hashes=None, **_):
            return [Torrent(hash=h, name=h, progress=1) for h in hashes]

    async def fake_run(settings, transfer, on_progress, streams=1, on_fallback=None):
        state["started"][transfer.id] = streams
        await state["gate"].wait()
        return "x 100% (xfr#1, to-chk=0/1)"

    async def no_webhook(*a, **k): ...

    monkeypatch.setattr(worker, "QbitClient", FakeQbit)
    monkeypatch.setattr(worker, "run_transfer", fake_run)
    monkeypatch.setattr(worker, "send_webhook", no_webhook)
    worker.running.clear()
    return state


async def drain():
    while worker.running:
        await asyncio.sleep(0.01)


def run_cycle(fake, s, release=True):
    async def go():
        fake["gate"] = asyncio.Event()
        await worker.process_queue(s)
        await asyncio.sleep(0)
        snapshot = dict(fake["started"])
        if release:
            fake["gate"].set()
            await drain()
        return snapshot
    return asyncio.run(go())


def test_connection_limit_and_queueing(fake):
    s = AppSettings(max_parallel_transfers=2)
    ids = queue([0, 0, 0, 0], s)

    async def go():
        fake["gate"] = asyncio.Event()
        await worker.process_queue(s)
        await asyncio.sleep(0)
        assert sorted(fake["started"]) == ids[:2]          # only 2 connections
        assert db.get_transfer(ids[2]).status == TransferStatus.pending
        await worker.process_queue(s)                      # no free connection: nothing new
        assert len(fake["started"]) == 2
        fake["gate"].set()
        await drain()
        await worker.process_queue(s)                      # freed: the rest start
        await drain()

    asyncio.run(go())
    assert sorted(fake["started"]) == ids
    assert all(db.get_transfer(i).status == TransferStatus.completed for i in ids)


def test_lone_big_file_gets_all_connections(fake):
    s = AppSettings(max_parallel_transfers=4, transfer_mode=TransferMode.orchestrated_pull)
    (big,) = queue([14 * GIB], s)
    assert run_cycle(fake, s) == {big: 4}


def test_spare_connections_go_to_big_files_only(fake):
    s = AppSettings(max_parallel_transfers=4, transfer_mode=TransferMode.orchestrated_pull)
    big, small = queue([14 * GIB, 100 << 20], s)
    assert run_cycle(fake, s) == {big: 3, small: 1}


def test_many_files_one_connection_each(fake):
    s = AppSettings(max_parallel_transfers=4, transfer_mode=TransferMode.orchestrated_pull)
    ids = queue([14 * GIB] * 5, s)
    assert run_cycle(fake, s) == {i: 1 for i in ids[:4]}


def test_remote_push_never_segments(fake):
    s = AppSettings(max_parallel_transfers=4, transfer_mode=TransferMode.remote_push)
    (big,) = queue([14 * GIB], s)
    assert run_cycle(fake, s) == {big: 1}
