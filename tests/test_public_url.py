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
    ("rookhub:7005", 7005, {}, "http://rookhub:7005"),
    ("hub.lan:7005", 7005, {}, "http://hub.lan:7005"),
    # A public name stays https even on the dashboard's port (a TLS proxy
    # elsewhere may listen there); write http:// to say otherwise.
    ("rook.example.com:7005", 7005, {}, "https://rook.example.com:7005"),
    ("11.0.0.1:7005", 7005, {}, "https://11.0.0.1:7005"),
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


