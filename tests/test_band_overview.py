import asyncio
import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.remote.band_overview import BandOverview
from rook.remote.bootstrap import CombinedServer


def row(wid, band="a", **extra):
    return {"worker_id": wid, "name": wid, "band": band,
            "caps": ["chat.rooms"], "last_seen_age_secs": 1, **extra}


@pytest.mark.asyncio
async def test_many_readers_share_nonblocking_bounded_poll():
    rows = [row(str(i)) for i in range(9)]
    release = asyncio.Event()
    started = asyncio.Event()
    calls = []
    active = peak = 0

    async def call(cap, target, **kwargs):
        nonlocal active, peak
        calls.append(target)
        active += 1
        peak = max(peak, active)
        if active == 4:
            started.set()
        await release.wait()
        active -= 1
        return {"ok": True, "result": {"rooms": [{"room": "test", "last_ts": 1}]}}

    cache = BandOverview(lambda _: rows, call)
    try:
        assert cache.snapshot()["chats"] == []
        await asyncio.wait_for(started.wait(), 1)
        for _ in range(100):
            result = cache.snapshot()
            assert len(result["workers"]) == 9
        assert len(calls) == 4  # HTTP readers never create per-client worker polls.
        release.set()
        for _ in range(100):
            if len(cache._cache) == 9:
                break
            await asyncio.sleep(.005)
        assert len(cache.snapshot()["chats"]) == 9
        assert len(calls) == 9 and peak == 4
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_filter_move_ban_and_cache_expiration():
    rows = [row("one"), row("two", "b"), row("banned", banned=True), row("offline", last_seen_age_secs=99)]
    call = AsyncMock(return_value={"ok": True, "result": {"rooms": [{"room": "op", "last_ts": 5, "last_text": "hello"}]}})
    cache = BandOverview(lambda band: [w for w in rows if not band or w["band"] == band], call)
    await cache.refresh()
    assert call.await_count == 2
    try:
        assert [c["wid"] for c in cache.snapshot("a")["chats"]] == ["one"]
        # Moving a worker cannot expose summaries cached under its old band.
        rows[0]["band"] = "b"
        assert [c["wid"] for c in cache.snapshot("b")["chats"]] == ["two"]
        updated, rooms = cache._cache[("b", "two")]
        cache._cache[("b", "two")] = (time.time() - 35, rooms)
        assert cache.snapshot("b")["chats"][0]["stale"]
        cache._cache[("b", "two")] = (time.time() - 65, rooms)
        assert cache.snapshot("b")["chats"] == []
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_failed_worker_keeps_recent_summary():
    call = AsyncMock(return_value={"ok": True, "result": {"rooms": [{"room": "op", "last_ts": "bad", "last_text": "x" * 1000}]}})
    cache = BandOverview(lambda _: [row("one")], call)
    await cache.refresh()
    call.side_effect = TimeoutError
    await cache.refresh()
    try:
        chat = cache.snapshot()["chats"][0]
        assert chat["last_ts"] == 0 and len(chat["last_text"]) == 280
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_overview_uses_existing_operator_auth(tmp_path, monkeypatch):
    from rook.remote import setup_store
    for key, filename in (("ROOK_SETUP_PATH", "setup.json"), ("ROOK_ENROLLMENT_DB", "enrollment.db"), ("ROOK_CHAT_DB", "chat.db")):
        monkeypatch.setenv(key, str(tmp_path / filename))
    monkeypatch.delenv("ROOK_GOOGLE_CLIENT_FILE", raising=False)
    setup_store.save({"band_name": "test", "band_psk": "test-band", "hub_public": "hub.example.com:443", "pyz_domain": "rook.example.com"})
    server = CombinedServer(web_user="operator", web_pass="operator-password")
    server._band = SimpleNamespace(workers={"one": {**row("one"), "last_seen": time.time()}}, call=AsyncMock(return_value={"ok": True, "result": {"rooms": []}}))
    guest = server._accounts.store.create_local("guest", "guest-password-long")
    token = server._accounts.store.new_session(guest)
    async with TestClient(TestServer(server._app)) as client:
        for headers in ({"Accept": "application/json"}, {"Accept": "application/json", "Cookie": "rook_account=" + token}):
            response = await client.get("/api/band/overview", headers=headers)
            assert response.status == 401
        assert server._overview._task is None
        auth = "Basic " + base64.b64encode(b"operator:operator-password").decode()
        response = await client.get("/api/band/overview", headers={"Authorization": auth})
        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
        assert (await response.json())["workers"][0]["worker_id"] == "one"
    assert server._overview._task is None
