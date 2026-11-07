import asyncio

from app import db, worker
from app.models import AppSettings, Torrent, TransferCreate, TransferStatus


def queue(n, s):
    return [db.create_transfer(TransferCreate(torrent_hash=f"h{i}", torrent_name=f"T{i}", source_path=f"/s/{i}"), s).id
            for i in range(n)]


def test_requeue_interrupted():
    s = AppSettings()
    (tid,) = queue(1, s)
    db.update_transfer(tid, TransferStatus.transferring, "16%")
    assert db.requeue_interrupted() == 1
    assert db.get_transfer(tid).status == TransferStatus.waiting


def test_parallel_limit(monkeypatch):
    s = AppSettings(max_parallel_transfers=2)
    ids = queue(4, s)
    gate = asyncio.Event()
    started = []

    class FakeQbit:
        def __init__(self, _settings): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): ...
        async def torrents(self, hashes=None, **_):
            return [Torrent(hash=h, name=h, progress=1) for h in hashes]

    async def fake_run(settings, transfer, on_progress):
        started.append(transfer.id)
        await gate.wait()
        return "x 100% (xfr#1, to-chk=0/1)"

    async def no_webhook(*a, **k): ...

    monkeypatch.setattr(worker, "QbitClient", FakeQbit)
    monkeypatch.setattr(worker, "run_transfer", fake_run)
    monkeypatch.setattr(worker, "send_webhook", no_webhook)

    async def run():
        await worker.process_queue(s)
        await asyncio.sleep(0)
        assert sorted(started) == ids[:2]                     # only 2 at once
        assert db.get_transfer(ids[2]).status == TransferStatus.pending
        await worker.process_queue(s)                          # no free slot: nothing new
        await asyncio.sleep(0)
        assert len(started) == 2
        gate.set()
        while worker.running:
            await asyncio.sleep(0.01)
        await worker.process_queue(s)                          # slots freed: the rest start
        while worker.running:
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert sorted(started) == ids
    assert all(db.get_transfer(i).status == TransferStatus.completed for i in ids)
