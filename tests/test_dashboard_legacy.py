"""Legacy dashboard surfaces that were removed stay removed."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer

SETUP = {"band_name": "b", "band_psk": "alpha-bravo-charlie-delta-echo",
         "hub_public": "hub.example.com:443", "pyz_domain": "hub.example.com"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    setup_store.save(SETUP)
    s = CombinedServer(bind="127.0.0.1")
    yield s
    if s._chat:
        s._chat.close()


@pytest.mark.asyncio
async def test_legacy_exec_worker_endpoints_are_gone(server):
    # No password: the auth gate admits everything, so a 404 means the route
    # itself is gone rather than hidden behind a login.
    async with TestClient(TestServer(server._app)) as client:
        assert (await client.get("/worker.py")).status == 404
        assert (await client.get("/ws")).status == 404
        assert not CombinedServer._auth_exempt("/ws")
        assert not CombinedServer._auth_exempt("/worker.py")
        health = await (await client.get("/health")).json()
        assert "workers" not in health and health["status"] == "ok"
