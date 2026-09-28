"""First-run /setup is guarded, and dashboard sessions are random and revocable."""
import base64

import pytest
from aiohttp import DummyCookieJar
from aiohttp.test_utils import TestClient, TestServer

from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer

AUTH = {"Authorization": "Basic " + base64.b64encode(b"owner:test-password-1").decode()}
HTML = {"Accept": "text/html"}
SETUP = {"band_name": "b", "band_psk": "alpha-bravo-charlie-delta-echo",
         "hub_public": "hub.example.com:443", "pyz_domain": "hub.example.com"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    servers = []

    def make(**kw):
        servers.append(CombinedServer(**kw))
        return servers[-1]
    yield make
    for s in servers:
        if s._chat:
            s._chat.close()


async def login(client, password="test-password-1"):
    response = await client.post("/login", data={"user": "owner", "pass": password},
                                 allow_redirects=False)
    assert response.status == 302
    return response


@pytest.mark.asyncio
async def test_password_hub_setup_needs_login_then_works(env):
    server = env(web_user="owner", web_pass="test-password-1")
    async with TestClient(TestServer(server._app)) as client:
        response = await client.get("/setup", headers=HTML, allow_redirects=False)
        assert response.status == 302 and response.headers["Location"] == "/login"
        response = await client.post("/setup", data={**SETUP, "csrf": server._setup_csrf},
                                     allow_redirects=False)
        assert response.status == 401 and not setup_store.is_configured()
        # The login page stays reachable and leads straight to the wizard.
        assert (await client.get("/login")).status == 200
        response = await login(client)
        assert response.headers["Location"] == "/setup"
        response = await client.get("/", headers=HTML, allow_redirects=False)
        assert response.headers["Location"] == "/setup"
        assert (await client.get("/setup")).status == 200
        response = await client.post("/setup", data={**SETUP, "csrf": server._setup_csrf},
                                     allow_redirects=False)
        assert response.status == 302 and setup_store.load()["band_psk"] == SETUP["band_psk"]


@pytest.mark.asyncio
async def test_passwordless_loopback_setup_stays_open_but_needs_form_token(env):
    server = env(bind="127.0.0.1")
    async with TestClient(TestServer(server._app)) as client:
        response = await client.get("/setup")
        assert response.status == 200 and server._setup_csrf in await response.text()
        response = await client.post("/setup", data=SETUP)
        assert response.status == 400 and not setup_store.is_configured()
        response = await client.post("/setup", data={**SETUP, "csrf": server._setup_csrf},
                                     allow_redirects=False)
        assert response.status == 302 and setup_store.is_configured()


@pytest.mark.asyncio
async def test_sessions_are_random_and_revoked_on_logout(env):
    setup_store.save(SETUP)
    server = env(web_user="owner", web_pass="test-password-1")
    async with TestClient(TestServer(server._app), cookie_jar=DummyCookieJar()) as client:
        first = (await login(client)).cookies["rook_session"].value
        second = (await login(client)).cookies["rook_session"].value
        assert first != second and "owner" not in first
        cookie = {"Cookie": "rook_session=" + first}
        assert (await client.post("/api/bands/psk", headers=cookie)).status == 200
        await client.get("/logout", headers=cookie, allow_redirects=False)
        assert (await client.post("/api/bands/psk", headers=cookie)).status == 401
        other = {"Cookie": "rook_session=" + second}
        assert (await client.post("/api/bands/psk", headers=other)).status == 200


@pytest.mark.asyncio
async def test_password_change_revokes_sessions(env):
    setup_store.save(SETUP)
    server = env(web_user="owner", web_pass="test-password-1")
    async with TestClient(TestServer(server._app)) as client:
        token = (await login(client)).cookies["rook_session"].value
    server = env(web_user="owner", web_pass="test-password-2")
    async with TestClient(TestServer(server._app)) as client:
        response = await client.post("/api/bands/psk", headers={"Cookie": "rook_session=" + token})
        assert response.status == 401


@pytest.mark.asyncio
async def test_basic_auth_works_without_minting_a_cookie(env):
    setup_store.save(SETUP)
    server = env(web_user="owner", web_pass="test-password-1")
    async with TestClient(TestServer(server._app)) as client:
        response = await client.post("/api/bands/psk", headers=AUTH)
        assert response.status == 200 and "rook_session" not in response.cookies
