"""Failed-login throttle (rook/core/throttle.py) and every place it guards a
secret: dashboard login form and Basic auth, account sign-in, MCP bearer
tokens, the OAuth /token endpoint, /healthz and the skill download."""
import asyncio
import base64
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from rook.core import throttle as throttle_mod
from rook.core.throttle import LoginThrottle, client_keys, from_env


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(clock=None, **kw):
    kw.setdefault("max_failures", 3)
    kw.setdefault("window", 60)
    kw.setdefault("lockout", 30)
    kw.setdefault("lockout_max", 120)
    return LoginThrottle(clock=clock or Clock(), **kw)


# -- the throttle itself ---------------------------------------------------------

def test_locks_after_n_failures_and_backs_off_exponentially():
    clock = Clock()
    th = make(clock)
    assert th.failure("192.0.2.1") == 0 and th.failure("192.0.2.1") == 0
    assert th.retry_after("192.0.2.1") == 0
    assert th.failure("192.0.2.1") == 30
    assert th.retry_after("192.0.2.1") == 30
    clock.t += 10.5
    assert th.retry_after("192.0.2.1") == 20       # rounded up
    assert th.retry_after("192.0.2.2") == 0        # other addresses unaffected
    clock.t += 20
    assert th.retry_after("192.0.2.1") == 0
    # After a lockout, one more failure locks again, twice as long, capped.
    assert th.failure("192.0.2.1") == 60
    clock.t += 61
    assert th.failure("192.0.2.1") == 120
    clock.t += 121
    assert th.failure("192.0.2.1") == 120


def test_failures_while_locked_do_not_extend_or_log_again(caplog):
    clock = Clock()
    th = make(clock)
    with caplog.at_level(logging.WARNING, logger="rook.core.throttle"):
        for _ in range(3):
            th.failure("192.0.2.1", "test login")
        for _ in range(10):
            assert th.failure("192.0.2.1") == 0
    warnings = [r for r in caplog.records if "locked out" in r.getMessage()]
    assert len(warnings) == 1 and "192.0.2.1" in warnings[0].getMessage()
    assert "test login" in warnings[0].getMessage()
    assert th.retry_after("192.0.2.1") == 30


def test_success_resets_failures_and_backoff():
    clock = Clock()
    th = make(clock)
    th.failure("192.0.2.1"); th.failure("192.0.2.1")
    th.success("192.0.2.1")
    th.failure("192.0.2.1"); th.failure("192.0.2.1")
    assert th.retry_after("192.0.2.1") == 0
    th.failure("192.0.2.1")
    clock.t += 31
    th.success("192.0.2.1")
    # A fresh start: N failures again before a lockout, and back to 30 s.
    assert th.failure("192.0.2.1") == 0 and th.failure("192.0.2.1") == 0
    assert th.failure("192.0.2.1") == 30


def test_sliding_window_and_quiet_spell():
    clock = Clock()
    th = make(clock)
    th.failure("192.0.2.1"); th.failure("192.0.2.1")
    clock.t += 61                                  # those two fell out of the window
    assert th.failure("192.0.2.1") == 0
    th.failure("192.0.2.1")
    assert th.failure("192.0.2.1") == 30
    clock.t += 30 + 121                            # quiet longer than max(window, lockout_max)
    assert th.failure("192.0.2.1") == 0            # strikes forgotten


def test_memory_is_bounded_and_idle_entries_pruned():
    clock = Clock()
    th = make(clock, max_keys=100)
    for i in range(1000):
        th.failure(f"key-{i}")
    assert len(th) == 100
    assert th.retry_after("key-999") == 0 and "key-999" in th._entries
    clock.t += 200                                 # past window, lockout_max and the prune tick
    th.failure("fresh")
    assert len(th) == 1


def test_multiple_keys_lock_together():
    th = make()
    for _ in range(3):
        th.failure(("198.51.100.7", "203.0.113.9"))
    assert th.retry_after(("203.0.113.9",)) == 30
    assert th.retry_after(("192.0.2.200", "198.51.100.7")) == 30


def test_disabled_and_env_settings(monkeypatch):
    off = make(max_failures=0)
    for _ in range(50):
        off.failure("192.0.2.1")
    assert off.retry_after("192.0.2.1") == 0
    monkeypatch.setenv("ROOK_LOGIN_MAX_FAILURES", "7")
    monkeypatch.setenv("ROOK_LOGIN_WINDOW_SECS", "120")
    monkeypatch.setenv("ROOK_LOGIN_LOCKOUT_SECS", "10")
    monkeypatch.setenv("ROOK_LOGIN_LOCKOUT_MAX_SECS", "nonsense")
    th = from_env()
    assert (th.max_failures, th.window, th.lockout, th.lockout_max) == (7, 120, 10, 900)


def test_client_keys_trust_proxy_headers_only_from_loopback():
    hdr = {"X-Forwarded-For": "198.51.100.7", "CF-Connecting-IP": "203.0.113.9"}
    # A remote peer's headers are ignored: it can neither dodge nor frame.
    assert client_keys("192.0.2.66", hdr) == ("192.0.2.66",)
    # Behind a local proxy: the addresses the proxy reported.
    assert client_keys("127.0.0.1", hdr) == ("203.0.113.9", "198.51.100.7")
    assert client_keys("::1", {"X-Forwarded-For": "198.51.100.7"}) == ("198.51.100.7",)
    assert client_keys("::ffff:127.0.0.1", {"CF-Connecting-IP": "203.0.113.9"}) == ("203.0.113.9",)
    # X-Forwarded-For: the entry the nearest proxy appended, not what the client wrote.
    assert client_keys("127.0.0.1", {"X-Forwarded-For": "192.0.2.1, 198.51.100.7"}) == ("198.51.100.7",)
    assert client_keys("127.0.0.1", {"X-Forwarded-For": "198.51.100.7, 127.0.0.1"}) == ("198.51.100.7",)
    # No usable header: the peer itself.
    assert client_keys("127.0.0.1", {}) == ("127.0.0.1",)
    assert client_keys("127.0.0.1", {"CF-Connecting-IP": "not-an-ip"}) == ("127.0.0.1",)
    assert client_keys(None) == ("unknown",)
    # IPv6 counts per /64.
    assert client_keys("2001:db8:1:2::5") == client_keys("2001:db8:1:2:ffff::1") == ("2001:db8:1:2::/64",)


def test_remote_peer_cannot_frame_or_dodge_with_forwarded_headers():
    th = make()
    for i in range(3):
        th.failure(client_keys("192.0.2.66", {"X-Forwarded-For": f"192.0.2.{i + 10}",
                                              "CF-Connecting-IP": "192.0.2.10"}))
    assert th.retry_after(client_keys("192.0.2.66")) == 30            # the attacker is locked
    assert th.retry_after(client_keys("192.0.2.10")) == 0             # the named victim is not
    assert th.retry_after(client_keys("127.0.0.1", {"X-Forwarded-For": "192.0.2.10"})) == 0


# -- dashboard -------------------------------------------------------------------

SETUP = {"band_name": "b", "band_psk": "alpha-bravo-charlie-delta-echo",
         "hub_public": "hub.example.com:443", "pyz_domain": "hub.example.com"}


@pytest.fixture
def dash(tmp_path, monkeypatch):
    from rook.remote import setup_store
    from rook.remote.bootstrap import CombinedServer
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.delenv("ROOK_GOOGLE_CLIENT_FILE", raising=False)
    setup_store.save(SETUP)
    th = make()
    monkeypatch.setattr(throttle_mod, "_shared", th)
    servers = []

    def build(**kw):
        servers.append(CombinedServer(**kw))
        return servers[-1]
    yield SimpleNamespace(build=build, throttle=th)
    for s in servers:
        if s._chat:
            s._chat.close()


def _basic(password):
    return {"Authorization": "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()}


@pytest.mark.asyncio
async def test_dashboard_login_form_locks_out_without_checking(dash):
    server = dash.build(web_user="owner", web_pass="test-password-1")
    assert server._login_throttle is dash.throttle
    async with TestClient(TestServer(server._app)) as client:
        for _ in range(2):
            r = await client.post("/login", data={"user": "owner", "pass": "wrong"})
            assert r.status == 200 and "not right" in await r.text()
        r = await client.post("/login", data={"user": "owner", "pass": "wrong"})
        assert r.status == 429 and r.headers["Retry-After"] == "30"
        assert "Too many attempts, try again in 30 seconds" in await r.text()
        server._check_creds = Mock(return_value=True)
        r = await client.post("/login", data={"user": "owner", "pass": "test-password-1"},
                              allow_redirects=False)
        assert r.status == 429 and int(r.headers["Retry-After"]) > 0
        server._check_creds.assert_not_called()
        # Another client behind the same local proxy is not affected.
        r = await client.post("/login", data={"user": "owner", "pass": "test-password-1"},
                              headers={"X-Forwarded-For": "192.0.2.20"}, allow_redirects=False)
        assert r.status == 302


@pytest.mark.asyncio
async def test_dashboard_success_resets_and_constant_time_check(dash):
    server = dash.build(web_user="owner", web_pass="test-password-1")
    assert server._check_creds("owner", "test-password-1")
    assert not server._check_creds("owner", "test-password-2")
    assert not server._check_creds("someone", "test-password-1")
    async with TestClient(TestServer(server._app)) as client:
        for _ in range(2):
            await client.post("/login", data={"user": "owner", "pass": "wrong"})
        r = await client.post("/login", data={"user": "owner", "pass": "test-password-1"},
                              allow_redirects=False)
        assert r.status == 302
        assert dash.throttle.retry_after("127.0.0.1") == 0 and not dash.throttle._entries


@pytest.mark.asyncio
async def test_dashboard_basic_auth_locks_out(dash):
    server = dash.build(web_user="owner", web_pass="test-password-1")
    async with TestClient(TestServer(server._app)) as client:
        for _ in range(2):
            assert (await client.get("/api/bands", headers=_basic("wrong"))).status == 401
        r = await client.get("/api/bands", headers=_basic("wrong"))
        assert r.status == 429 and r.headers["Retry-After"] == "30"
        r = await client.get("/api/bands", headers=_basic("test-password-1"))
        assert r.status == 429
        # A client with a valid session is not asked for a password, so not blocked.
        dash.throttle.clear()
        assert (await client.get("/api/bands", headers=_basic("test-password-1"))).status == 200


@pytest.mark.asyncio
async def test_account_sign_in_locks_out(dash):
    server = dash.build(band_psk="old-key", web_user="operator", web_pass="operator-password",
                        domain="rook.example.com")
    server._accounts.store.create_local("alice", "correct horse battery staple")
    origin = server._accounts.origin
    form_cookie = {"Cookie": "rook_login_form=tok", "Origin": origin}

    async def attempt(password, **extra):
        return await client.post("/account/login", headers={**form_cookie, **extra},
                                 data={"form_token": "tok", "username": "alice",
                                       "password": password}, allow_redirects=False)

    async with TestClient(TestServer(server._app)) as client:
        for _ in range(2):
            assert (await attempt("wrong")).status == 401
        r = await attempt("wrong")
        assert r.status == 429 and r.headers["Retry-After"] == "30"
        assert "Too many attempts" in await r.text()
        calls = []
        real = server._accounts.store.login
        server._accounts.store.login = lambda *a: calls.append(a) or real(*a)
        assert (await attempt("correct horse battery staple")).status == 429
        assert calls == []
        dash.throttle.clear()
        assert (await attempt("correct horse battery staple")).status == 302
        assert calls


# -- MCP side --------------------------------------------------------------------

def _guarded_app(throttle, checks):
    from rook.band_mcp.auth_throttle import ThrottleMiddleware, note

    async def mcp(request):
        checks.append(1)
        ok = request.headers.get("authorization") == "Bearer good-token"
        note(ok)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 401)

    async def other(request):
        note(False)                                # unguarded: must not count
        return JSONResponse({}, status_code=401)
    return ThrottleMiddleware(Starlette(routes=[Route("/mcp", mcp, methods=["POST"]),
                                                Route("/jobs/account-api", other)]), throttle)


def _client(app, peer="192.0.2.10"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
                             base_url="http://127.0.0.1:8765")


@pytest.mark.asyncio
async def test_mcp_middleware_locks_out_without_calling_the_check():
    th, checks = make(), []
    app = _guarded_app(th, checks)
    async with _client(app) as http:
        bad = {"Authorization": "Bearer wrong"}
        for _ in range(3):
            assert (await http.post("/mcp", headers=bad)).status_code == 401
        r = await http.post("/mcp", headers={"Authorization": "Bearer good-token"})
        assert r.status_code == 429 and r.headers["retry-after"] == "30"
        assert r.json()["error"] == "too_many_attempts"
        assert len(checks) == 3
        # No credential (OAuth discovery): passes to the app, still 401 there.
        assert (await http.post("/mcp")).status_code == 401
    async with _client(app, "192.0.2.11") as http:
        assert (await http.post("/mcp", headers={"Authorization": "Bearer good-token"})).status_code == 200


@pytest.mark.asyncio
async def test_mcp_middleware_ignores_unguarded_paths_and_success_resets():
    th, checks = make(), []
    app = _guarded_app(th, checks)
    async with _client(app) as http:
        for _ in range(10):
            await http.get("/jobs/account-api", headers={"Authorization": "Bearer internal"})
        assert not th._entries
        await http.post("/mcp", headers={"Authorization": "Bearer wrong"})
        await http.post("/mcp", headers={"Authorization": "Bearer wrong"})
        await http.post("/mcp", headers={"Authorization": "Bearer good-token"})
        assert not th._entries


@pytest.mark.asyncio
async def test_mcp_middleware_ignores_headers_from_remote_peers():
    th, checks = make(), []
    app = _guarded_app(th, checks)
    async with _client(app, "192.0.2.66") as http:
        for i in range(3):
            await http.post("/mcp", headers={"Authorization": "Bearer wrong",
                                             "X-Forwarded-For": f"192.0.2.{i + 100}"})
        r = await http.post("/mcp", headers={"Authorization": "Bearer wrong",
                                             "X-Forwarded-For": "192.0.2.150"})
        assert r.status_code == 429
    assert th.retry_after("192.0.2.100") == 0


class FakeBand:
    workers: dict = {}

    async def call(self, *a, **k):
        return {"ok": True, "result": {}}


STATIC = "static-token-0123456789abcdef"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "pytest", "version": "1"}}}


@pytest.mark.asyncio
async def test_mcp_bearer_skill_and_healthz_lock_out(tmp_path):
    from rook.band_mcp.auth_throttle import ThrottleMiddleware
    from rook.band_mcp.healthz import route as healthz_route
    from rook.band_mcp.server import build_server
    mcp, store = build_server(FakeBand(), persist_path=str(tmp_path / "tokens.json"),
                              static_token=STATIC, journal_path=str(tmp_path / "journal.db"))
    calls = []
    real = store.verify_bearer
    store.verify_bearer = lambda t: calls.append(t) or real(t)
    starlette_app = mcp.streamable_http_app()
    starlette_app.router.routes.insert(0, healthz_route(mcp._session_manager, FakeBand(), STATIC))
    th = make()
    app = ThrottleMiddleware(starlette_app, th)
    bad = {"Authorization": "Bearer wrong-token-0123456789", **ACCEPT}
    async with mcp.session_manager.run():
        async with _client(app) as http:
            assert (await http.post("/mcp", json=INIT, headers=bad)).status_code == 401
            # The skill download and /healthz count one failure each, not two.
            await http.get("/skill/rook.skill", headers=bad)
            assert (await http.get("/healthz", headers=bad)).status_code == 401
            assert th.retry_after("192.0.2.10") == 30
            n = len(calls)
            r = await http.post("/mcp", json=INIT, headers={**bad, "Authorization": "Bearer " + STATIC})
            assert r.status_code == 429 and r.headers["retry-after"]
            assert len(calls) == n                 # the token was not looked at
        async with _client(app, "192.0.2.11") as http:
            r = await http.post("/mcp", json=INIT, headers={**ACCEPT, "Authorization": "Bearer " + STATIC})
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_oauth_token_endpoint_locks_out():
    from rook.band_mcp.auth_throttle import ThrottleMiddleware
    from rook.band_mcp.oauth_shim import OAuthShim
    store = Mock()
    store.verify_bearer.return_value = None
    th = make()
    app = ThrottleMiddleware(OAuthShim(Starlette(), store, "https://rook.example.com"), th)
    async with _client(app) as http:
        for _ in range(3):
            r = await http.post("/token", data={"grant_type": "refresh_token",
                                                "refresh_token": "guess"})
            assert r.status_code == 400
        assert store.verify_bearer.call_count == 3
        r = await http.post("/token", data={"grant_type": "refresh_token", "refresh_token": "guess"})
        assert r.status_code == 429 and r.headers["retry-after"] == "30"
        assert store.verify_bearer.call_count == 3
        # Discovery stays open while locked out.
        assert (await http.get("/.well-known/oauth-authorization-server")).status_code == 200


# -- merged hub: one throttle for both halves --------------------------------------

def free_port(kind=None) -> int:
    import socket
    with socket.socket(socket.AF_INET, kind or socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EchoRelay(asyncio.DatagramProtocol):
    """Stands in for the relay; ignores band traffic."""

    def connection_made(self, transport):
        self.transport = transport


@pytest.mark.asyncio
async def test_merged_hub_shares_one_throttle(tmp_path, monkeypatch):
    import socket
    from rook.hub.serve import Hub, parse_args
    from rook.remote.bootstrap import prepare
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    for var in ("ROOK_HUB", "ROOK_HUB_HOST", "ROOK_HUB_PORT", "ROOK_MCP_BIND", "ROOK_CHAT_DB",
                "ROOK_MCP_PERSIST", "ROOK_MCP_STATIC_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(throttle_mod, "_shared", make())
    port, relay_port = free_port(), free_port(socket.SOCK_DGRAM)
    argv = ["--bind", "127.0.0.1", "--port", str(port), "--mcp-bind", "off",
            "--hub-port", str(relay_port), "--web-pass", "pw", "--psk", "alpha-bravo-charlie",
            "--static-token", STATIC, "--domain", f"http://127.0.0.1:{port}"]
    loop = asyncio.get_running_loop()
    relay, _ = await loop.create_datagram_endpoint(EchoRelay, local_addr=("127.0.0.1", relay_port))
    ap, args = parse_args(argv)
    prepare(ap, args)
    hub = Hub(args, argv)
    await hub.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as http:
            proxied = {"X-Forwarded-For": "192.0.2.50"}
            bad = {"Authorization": "Bearer wrong-token-0123456789", **ACCEPT, **proxied}
            for _ in range(3):
                async with http.post(base + "/mcp", json=INIT, headers=bad) as r:
                    assert r.status == 401
            async with http.post(base + "/mcp", json=INIT, headers=bad) as r:
                assert r.status == 429 and r.headers["Retry-After"]
            # The dashboard password is refused for that address too, unchecked.
            async with http.post(base + "/login", data={"user": "admin", "pass": "pw"},
                                 headers=proxied, allow_redirects=False) as r:
                assert r.status == 429
            async with http.post(base + "/login", data={"user": "admin", "pass": "pw"},
                                 headers={"X-Forwarded-For": "192.0.2.51"},
                                 allow_redirects=False) as r:
                assert r.status == 302
    finally:
        await hub.stop()
        relay.close()
