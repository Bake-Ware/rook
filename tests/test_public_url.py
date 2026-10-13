"""One public base URL decides http vs https for everything the hub hands out:
a LAN quickstart hub (plain HTTP on <ip>:7005) gets http:// installers, a
public hub behind a TLS proxy keeps https:// exactly as before."""

import base64

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer
from rook.remote.enrollment import EnrollmentStore
from rook.remote.public_url import public_base, split_domain
from rook.worker import enroll

LAN = "192.0.2.10"   # TEST-NET-1: stands in for a LAN address in rendered scripts
PRIVATE = "10.0.0.5"
AUTH = "Basic " + base64.b64encode(b"owner:password").decode()


@pytest.mark.parametrize("value,port,env,expected", [
    ("rook.example.com", 7005, {}, "https://rook.example.com"),
    ("rook.example.com:443", 7005, {}, "https://rook.example.com:443"),
    (f"{LAN}:7005", 7005, {}, f"http://{LAN}:7005"),        # straight at the dashboard
    (f"{LAN}:8443", 7005, {}, f"https://{LAN}:8443"),       # some other port: unchanged
    (f"{LAN}:80", 7005, {}, f"http://{LAN}:80"),
    ("localhost:7005", 7005, {}, "http://localhost:7005"),  # .env.example's value
    ("http://rook.example.com", 7005, {}, "http://rook.example.com"),
    ("https://rook.example.com:7005/", 7005, {}, "https://rook.example.com:7005"),
    ("HTTP://Box:9000/x", 7005, {}, "http://Box:9000"),
    ("rook.example.com", 7005, {"ROOK_PUBLIC_BASE": "http://rook.example.com"},
     "http://rook.example.com"),
    ("rook.example.com", 7005, {"ROOK_PUBLIC_BASE": "http://other.example.com"},
     "https://rook.example.com"),
])
def test_public_base(value, port, env, expected):
    assert public_base(value, port, env) == expected
    assert split_domain(value, port, env)[1] == expected.split("://", 1)[1]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    def make(domain, hub_public, port=7005):
        monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
        monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
        monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
        monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.delenv("ROOK_PUBLIC_BASE", raising=False)
        monkeypatch.delenv("ROOK_HUB_PUBLIC_WS_URL", raising=False)
        setup_store.save({"band_name": "home", "band_psk": "piano-haste-nugget-drift-mangle",
                          "hub_public": hub_public, "pyz_domain": domain})
        store = EnrollmentStore()
        store.import_config()
        server = CombinedServer(web_user="owner", web_pass="password", port=port,
                                domain=domain, hub_public=hub_public)
        primary = next(b for b in store.bands(secrets_visible=True) if b["is_primary"])
        return server, store, primary
    return make


async def _scripts(server, store, band):
    code = store.issue(band["id"])["code"]
    out = {}
    async with TestClient(TestServer(server._app)) as client:
        for name, url in [("unix", f"/worker?band={code}&os=unix"),
                          ("windows", f"/worker?band={code}&os=windows"),
                          ("install", "/install"), ("install_ps", "/install?os=windows"),
                          ("hub", "/hub"), ("help", "/")]:
            response = await client.get(url, headers={"Accept": "text/plain", "Authorization": AUTH})
            assert response.status == 200, (name, await response.text())
            out[name] = await response.text()
    return code, out


@pytest.mark.asyncio
async def test_lan_http_hub_installers_use_http(hub):
    server, store, band = hub(f"{LAN}:7005", f"{LAN}:8765")
    assert server.public_base == f"http://{LAN}:7005"
    assert server._accounts.origin == f"http://{LAN}:7005"
    code, out = await _scripts(server, store, band)
    for name, text in out.items():
        assert f"https://{LAN}" not in text, name
        assert f"http://{LAN}:7005" in text, name
    assert f"--enroll http://{LAN}:7005 --pair-code {code}" in out["unix"]
    assert f"http://{LAN}:7005/band-worker-enrollment.pyz" in out["windows"]
    assert f"http://{LAN}:7005/worker?band={code}&os=windows" in out["windows"]
    # Relay: the WebSocket bridge on the MCP port (ws:// for a non-443 port).
    assert "--enrolled --ws" in out["unix"]
    for text in (out["windows"], out["install_ps"]):
        assert text.isascii()


@pytest.mark.asyncio
async def test_public_https_hub_installers_unchanged(hub):
    server, store, band = hub("rook.example.com", "hub.example.com:443")
    assert server.public_base == "https://rook.example.com"
    assert server._accounts.origin == "https://rook.example.com"
    code, out = await _scripts(server, store, band)
    assert f"--enroll https://rook.example.com --pair-code {code}" in out["unix"]
    assert "curl -fsSL https://rook.example.com/band-worker-enrollment.pyz" in out["unix"]
    assert 'Invoke-WebRequest -Uri "https://rook.example.com/band-worker-enrollment.pyz"' in out["windows"]
    assert f"https://rook.example.com/worker?band={code}&os=windows" in out["windows"]
    assert 'BASE="${ROOK_BASE:-https://rook.example.com}"' in out["install"]
    assert 'BASE="${ROOK_BASE:-https://rook.example.com}"' in out["hub"]
    assert 'Invoke-WebRequest -Uri "https://rook.example.com/rook.py"' in out["install_ps"]
    assert "curl -fsSL https://rook.example.com/worker | bash" in out["help"]
    for text in out.values():
        assert "http://rook.example.com" not in text


def test_settings_domain_with_scheme_is_split(hub):
    server, _store, _band = hub("http://rook.lan:7005", "rook.lan:8765")
    assert server.domain == "rook.lan:7005" and server.public_base == "http://rook.lan:7005"
    server._apply_domain("https://rook.example.com")
    assert server.public_base == "https://rook.example.com"
    assert server._accounts.origin == "https://rook.example.com"


@pytest.mark.parametrize("server,ok", [
    ("https://rook.example.com", True),
    (f"http://{PRIVATE}:7005", True),
    ("http://127.0.0.1:7005", True),
    ("http://localhost:7005", True),
    ("http://rook.example.com", False),     # a public name never gets plain HTTP
    ("http://100.64.0.1:7005", True),       # CGNAT / tailnet
    ("http://11.0.0.1:7005", False),        # nor a public address
    ("http://0.0.0.0:7005", False),
    ("ftp://10.0.0.5", False),
    ("https://rook.example.com/path", False),
])
def test_worker_enrollment_origin(server, ok):
    if ok:
        enroll.check_origin(server)
    else:
        with pytest.raises(ValueError):
            enroll.check_origin(server)


# -- cookies and the dashboard -> account bridge ------------------------------

def _set_cookies(response):
    return {c.split("=", 1)[0]: c for c in response.headers.getall("Set-Cookie", [])}


async def _dashboard_login(client):
    response = await client.post("/login", data={"user": "owner", "pass": "password"},
                                 allow_redirects=False)
    assert response.status == 302
    return response


@pytest.mark.asyncio
async def test_http_lan_hub_cookies_are_not_secure_and_bridge_takes_one_hop(hub):
    server, _store, _band = hub(f"{LAN}:7005", f"{LAN}:8765")
    async with TestClient(TestServer(server._app)) as client:
        login = await _dashboard_login(client)
        assert "Secure" not in _set_cookies(login)["rook_session"]
        for path in ("/account/tokens/api", "/account/session", "/account/bands/api"):
            client.session.cookie_jar.clear(lambda c: c.key in ("rook_account", "rook_bridge"))
            response = await client.get(path, headers={"Accept": "application/json"})
            # 503: the token service (the MCP process) is not running here;
            # what matters is that the handler answered, not a sign-in error.
            assert response.status in (200, 503), (path, await response.text())
            assert len(response.history) == 1, path
            hop = _set_cookies(response.history[0])
            assert "Secure" not in hop["rook_account"] and "HttpOnly" in hop["rook_account"]
            await response.json()
        page = await client.get("/account/login")
        assert "Secure" not in _set_cookies(page)["rook_login_form"]


@pytest.mark.asyncio
async def test_https_hub_over_plain_http_explains_instead_of_looping(hub):
    server, _store, _band = hub("rook.example.com", "hub.example.com:443")
    async with TestClient(TestServer(server._app)) as client:
        await _dashboard_login(client)
        response = await client.get("/account/tokens/api", headers={"Accept": "application/json"})
        # One bridge hop: the Secure account cookie is dropped over http (the
        # client's jar behaves like a browser), so the second visit explains.
        assert len(response.history) == 1
        assert "Secure" in _set_cookies(response.history[0])["rook_account"]
        assert response.status == 400
        assert "https://rook.example.com" in (await response.json())["error"]
        page = await client.get("/account", headers={"Accept": "text/html"})
        assert page.status == 302 or len(page.history) <= 1
        # The mark is cleared with the error, so a reload tries once more.
        page = await client.get("/account", headers={"Accept": "text/html"})
        assert page.status == 400 and "ROOK_DOMAIN" in await page.text()
        assert len(page.history) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["rook.example.com", f"{LAN}:7005"])
@pytest.mark.parametrize("headers", [{"X-Forwarded-Proto": "https"},
                                     {"Forwarded": "for=192.0.2.1;proto=https"},
                                     {"CF-Visitor": '{"scheme":"https"}'}])
async def test_https_requests_get_secure_cookies(hub, domain, headers):
    server, _store, _band = hub(domain, "hub.example.com:443")
    async with TestClient(TestServer(server._app)) as client:
        login = await client.post("/login", data={"user": "owner", "pass": "password"},
                                  headers=headers, allow_redirects=False)
        assert "Secure" in _set_cookies(login)["rook_session"]
        session = login.cookies["rook_session"].value
        bridge = await client.get("/account/session", allow_redirects=False,
                                  headers={**headers, "Cookie": "rook_session=" + session})
        assert bridge.status == 302 and bridge.headers["Location"] == "/account/session"
        assert "Secure" in _set_cookies(bridge)["rook_account"]
        page = await client.get("/account/login", headers=headers)
        assert "Secure" in _set_cookies(page)["rook_login_form"]


@pytest.mark.asyncio
async def test_https_hub_keeps_secure_account_cookies_without_proxy_headers(hub):
    # A proxy that forgets X-Forwarded-Proto must not weaken an https hub.
    server, _store, _band = hub("rook.example.com", "hub.example.com:443")
    async with TestClient(TestServer(server._app)) as client:
        page = await client.get("/account/login")
        assert "Secure" in _set_cookies(page)["rook_login_form"]
        session = server._accounts.store.dashboard_session()
        bridge = await client.get("/account/session", allow_redirects=False,
                                  headers={"Cookie": "rook_session=" + session})
        assert "Secure" in _set_cookies(bridge)["rook_account"]
        assert "Secure" not in _set_cookies(bridge)["rook_bridge"]
