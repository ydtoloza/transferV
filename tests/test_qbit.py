import asyncio

import httpx
import pytest

from app import qbit
from app.models import AppSettings, QbitSettings


@pytest.fixture(autouse=True)
def reset_sessions():
    qbit._sessions.clear()
    yield
    qbit._sessions.clear()


def make(handler, **q):
    s = AppSettings(qbit=QbitSettings(url="http://q", **q))
    client = qbit.QbitClient(s)
    client.session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=client.client.headers)
    return s


async def list_torrents(s):
    async with qbit.QbitClient(s) as q:
        return await q.torrents()


def test_login_once_and_reuse_session():
    calls = {"login": 0}

    def handler(req):
        if req.url.path.endswith("/auth/login"):
            calls["login"] += 1
            return httpx.Response(200, text="Ok.")
        return httpx.Response(200, json={"rid": 1, "full_update": True, "torrents": {}})

    s = make(handler, username="admin", password="good")

    async def run():
        for _ in range(5):
            await list_torrents(s)

    asyncio.run(run())
    assert calls["login"] == 1


def test_failed_login_backs_off():
    calls = {"login": 0}

    def handler(req):
        calls["login"] += 1
        return httpx.Response(200, text="Fails.")

    s = make(handler, username="admin", password="bad")

    async def run():
        for _ in range(5):
            with pytest.raises(qbit.QbitError):
                await list_torrents(s)

    asyncio.run(run())
    assert calls["login"] == 1


def test_empty_password_never_hits_the_server():
    def handler(req):
        raise AssertionError("must not send a request")

    s = make(handler, username="admin", password="")
    with pytest.raises(qbit.QbitError, match="Falta"):
        asyncio.run(list_torrents(s))


def test_api_key_skips_login():
    seen = []

    def handler(req):
        assert not req.url.path.endswith("/auth/login")
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, json={"rid": 1, "full_update": True, "torrents": {}})

    s = make(handler, username="admin", password="", api_key="k123")
    asyncio.run(list_torrents(s))
    assert seen == ["Bearer k123"]


def test_maindata_is_merged_incrementally():
    responses = iter([
        {"rid": 1, "full_update": True, "torrents": {
            "a": {"name": "A", "progress": 0.5, "state": "downloading", "content_path": "/downloads/A", "added_on": 1},
            "b": {"name": "B", "progress": 1, "state": "uploading", "content_path": "/downloads/B", "added_on": 2},
        }},
        {"rid": 2, "torrents": {"a": {"progress": 1, "state": "uploading"}}, "torrents_removed": ["b"]},
    ])
    rids = []

    def handler(req):
        rids.append(req.url.params.get("rid"))
        return httpx.Response(200, json=next(responses))

    s = make(handler, api_key="k")
    s.qbit.downloads_path, s.qbit.host_downloads_path = "/downloads", "/srv/seedbox"

    first = asyncio.run(list_torrents(s))
    second = asyncio.run(list_torrents(s))
    assert rids == ["0", "1"]
    assert {t.hash for t in first} == {"a", "b"}
    assert [(t.hash, t.progress, t.content_path) for t in second] == [("a", 1, "/srv/seedbox/A")]
