"""The MCP door's OAuth shim (rook/core2/hub/doors/mcp_oauth.py): claude.ai's
connectors use an agent token as their client secret and get that same token
back as their access token. Unit tests on the shim, then the whole flow over
a real door on a hub in-process. Every test runs under a hard timeout."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from rook.core2.hub import runtime  # noqa: E402
from rook.core2.hub.doors.mcp_oauth import OAuthShim, check_url  # noqa: E402
from test_core2_hub_runtime import WHY, WID, FakeWorker, config, run, until  # noqa: E402

AGENT = runtime.Caller(person="person:bake", agent="agent:claude")
PERSON = runtime.Caller(person="person:bake")
VERIFIER = "v" * 50
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
CB = "https://claude.ai/api/mcp/auth_callback"


# -- the shim alone -----------------------------------------------------------------

class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def shim(tokens=None, clock=None):
    tokens = {"agent-tok": AGENT, "person-tok": PERSON} if tokens is None else tokens
    return OAuthShim("https://rook.example", "/mcp", tokens.get, clock or Clock())


def authorize(s, **over):
    q = {"response_type": "code", "client_id": "claude", "redirect_uri": CB, "code_challenge": CHALLENGE,
         "code_challenge_method": "S256", "state": "st", **over}
    return s.handle("GET", "/authorize", urlencode(q), b"")


def code_of(reply):
    status, _, headers = reply
    assert status == 302, reply
    loc = urlsplit(headers["Location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CB
    q = parse_qs(loc.query)
    assert q["state"] == ["st"]
    return q["code"][0]


def token(s, auth="", **form):
    return s.handle("POST", "/token", "", urlencode(form).encode(), auth)


def exchange(s, code, secret="agent-tok", **over):
    return token(s, **{"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
                       "redirect_uri": CB, "client_id": "claude", "client_secret": secret, **over})


def test_discovery_names_the_door_and_no_registration():
    s = shim()
    assert s.challenge() == 'Bearer resource_metadata="https://rook.example/.well-known/oauth-protected-resource/mcp"'
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        status, prm, _ = s.handle("GET", path, "", b"")
        assert status == 200 and prm["resource"] == "https://rook.example/mcp"
        assert prm["authorization_servers"] == ["https://rook.example"]
    status, meta, _ = s.handle("GET", "/.well-known/oauth-authorization-server", "", b"")
    assert status == 200 and meta["issuer"] == "https://rook.example"
    assert meta["authorization_endpoint"] == "https://rook.example/authorize"
    assert meta["token_endpoint"] == "https://rook.example/token"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "registration_endpoint" not in meta
    assert s.owns("GET", "/authorize") and s.owns("POST", "/token") and not s.owns("GET", "/token")
    assert not s.owns("POST", "/mcp") and not s.owns("GET", "/oauth/register")


def test_the_token_is_the_secret_and_the_access_token():
    s = shim()
    status, body, headers = exchange(s, code_of(authorize(s)))
    assert status == 200 and body["access_token"] == "agent-tok" == body["refresh_token"]
    assert body["token_type"] == "Bearer"
    status, body, _ = token(s, grant_type="refresh_token", refresh_token="agent-tok")
    assert status == 200 and body["access_token"] == "agent-tok"


def test_basic_auth_carries_the_secret():
    s = shim()
    basic = "Basic " + base64.b64encode(b"claude:agent-tok").decode()
    form = {"grant_type": "authorization_code", "code": code_of(authorize(s)), "code_verifier": VERIFIER}
    status, body, _ = token(s, basic, **form)
    assert status == 200 and body["access_token"] == "agent-tok"


def test_bad_secrets_are_refused():
    s = shim()
    assert exchange(s, code_of(authorize(s)), secret="nope")[:2] == \
        (401, {"error": "invalid_client", "error_description": "not a valid Rook token"})
    status, body, _ = exchange(s, code_of(authorize(s)), secret="person-tok")
    assert status == 401 and "agent's token" in body["error_description"]
    assert exchange(s, code_of(authorize(s)), secret="")[0] == 401
    assert token(s, grant_type="refresh_token", refresh_token="person-tok")[0] == 400
    assert token(s, grant_type="refresh_token", refresh_token="gone")[1]["error"] == "invalid_grant"
    assert token(s, grant_type="password")[1]["error"] == "unsupported_grant_type"


def test_codes_are_once_short_lived_and_bound():
    clock = Clock()
    s = shim(clock=clock)
    code = code_of(authorize(s))
    assert exchange(s, code)[0] == 200
    assert exchange(s, code)[1]["error"] == "invalid_grant"                  # once
    code = code_of(authorize(s))
    assert exchange(s, code, code_verifier="w" * 50)[1]["error_description"] == "PKCE verification failed"
    assert exchange(s, code)[0] == 400                                        # a wrong try spends it
    assert exchange(s, code_of(authorize(s)), redirect_uri="https://evil.example/cb")[0] == 400
    assert exchange(s, code_of(authorize(s)), client_id="other")[0] == 400
    code = code_of(authorize(s))
    clock.t += 301
    assert exchange(s, code)[1]["error_description"] == "invalid or expired code"
    assert s._codes == {}


@pytest.mark.parametrize("over, error", [
    ({"redirect_uri": "http://evil.example/cb"}, "invalid_request"),
    ({"redirect_uri": "https://a.example/cb#frag"}, "invalid_request"),
    ({"redirect_uri": "https://user@a.example/cb"}, "invalid_request"),
    ({"redirect_uri": "javascript:alert(1)"}, "invalid_request"),
    ({"redirect_uri": ""}, "invalid_request"),
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"code_challenge": ""}, "invalid_request"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"state": "x" * 5000}, "invalid_request"),
])
def test_authorize_refuses_bad_requests(over, error):
    status, body, headers = authorize(shim(), **over)
    assert status == 400 and body["error"] == error and "Location" not in headers


def test_loopback_callbacks_and_capacity():
    s = shim()
    for cb in ("http://127.0.0.1:9/cb", "http://localhost:33418/callback?x=1", "http://[::1]:9/cb"):
        status, _, headers = authorize(s, redirect_uri=cb)
        assert status == 302 and headers["Location"].startswith(cb + ("&" if "?" in cb else "?"))
    assert authorize(s, redirect_uri="http://127.0.0.1:99999/cb")[0] == 400
    s._codes = {str(i): {"exp": 10 ** 12} for i in range(1024)}
    assert authorize(s)[0] == 503


@pytest.mark.parametrize("url", ["", "rook.example", "http://rook.example", "https://rook.example/mcp",
                                 "https://rook.example/?x=1", 5])
def test_the_address_must_be_an_https_origin(url):
    with pytest.raises(ValueError):
        check_url(url)


def test_good_addresses():
    assert check_url("https://rook.example/") == "https://rook.example"
    assert check_url("http://127.0.0.1:8741") == "http://127.0.0.1:8741"


# -- the whole flow over a real door --------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_open = urllib.request.build_opener(_NoRedirect).open


def http(url, data=None, headers=None, method=None):
    """(status, headers, parsed body or None), in a thread, never following a redirect."""
    def go():
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _open(req, timeout=10) as r:
                status, hdrs, text = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, text = e.code, e.headers, e.read()
        return status, hdrs, json.loads(text) if text else None
    return asyncio.to_thread(go)


async def start(tmp_path, oauth=True):
    band = runtime.Loopback()
    cfg = config(tmp_path)
    cfg.doors = {"mcp": {"bind": "127.0.0.1:0", **({"oauth": "http://127.0.0.1:1"} if oauth else {})}}
    hub = runtime.Hub(cfg, transport=band)
    await hub.start()
    w = FakeWorker(hub, band, tmp_path / "w")
    await w.announce()
    await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
    return hub, f"http://127.0.0.1:{hub.doors[0].port}"


def rpc_body(method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()


def test_claude_ai_flow_end_to_end(tmp_path):
    async def go():
        hub, base = await start(tmp_path)
        try:
            agent_token, person_token = hub.add_token(AGENT), hub.add_token(PERSON)
            status, hdrs, _ = await http(base + "/mcp", rpc_body("ping"), {"Content-Type": "application/json"})
            assert status == 401
            meta_url = hdrs["WWW-Authenticate"].split('resource_metadata="')[1].rstrip('"')
            assert meta_url == "http://127.0.0.1:1/.well-known/oauth-protected-resource/mcp"
            status, _, prm = await http(base + urlsplit(meta_url).path)
            assert status == 200 and prm["resource"] == "http://127.0.0.1:1/mcp"
            status, _, meta = await http(base + "/.well-known/oauth-authorization-server")
            assert status == 200 and "registration_endpoint" not in meta

            async def sign_in(secret):
                q = urlencode({"response_type": "code", "client_id": "claude", "redirect_uri": CB, "state": "st",
                               "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
                status, hdrs, _ = await http(base + "/authorize?" + q)
                assert status == 302
                code = parse_qs(urlsplit(hdrs["Location"]).query)["code"][0]
                form = urlencode({"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
                                  "redirect_uri": CB, "client_id": "claude", "client_secret": secret}).encode()
                return await http(base + "/token", form, {"Content-Type": "application/x-www-form-urlencoded"})

            assert (await sign_in(person_token))[0] == 401
            status, hdrs, tok = await sign_in(agent_token)
            assert status == 200 and tok["access_token"] == agent_token and hdrs["Cache-Control"] == "no-store"
            bearer = {"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json"}
            call = {"name": "call", "arguments": {"worker": WID, "cap": "echo.say", "args": {"text": "hi"},
                                                  "why": WHY}}
            status, _, out = await http(base + "/mcp", rpc_body("tools/call", call), bearer)
            assert status == 200 and out["result"]["isError"] is False, out
            entry = (await hub.journal_view(AGENT, {"cap": "echo.say"}))["entries"][0]
            assert (entry["agent"], entry["person"]) == ("agent:claude", "person:bake")

            refresh = urlencode({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}).encode()
            assert (await http(base + "/token", refresh))[0] == 200
            tokens = hub.data / "tokens.json"                 # revoke = take the token out of tokens.json
            stored = json.loads(tokens.read_text())
            stored.pop(runtime.digest(agent_token))
            tokens.write_text(json.dumps(stored))
            await until(lambda: hub.authenticate(agent_token) is None)
            assert (await http(base + "/token", refresh))[0] == 400
            assert (await http(base + "/mcp", rpc_body("ping"), bearer))[0] == 401
        finally:
            await hub.stop()
    run(go())


def test_off_by_default(tmp_path):
    async def go():
        hub, base = await start(tmp_path, oauth=False)
        try:
            status, hdrs, _ = await http(base + "/mcp", rpc_body("ping"))
            assert status == 401 and hdrs["WWW-Authenticate"] == "Bearer"
            for path in ("/authorize", "/.well-known/oauth-authorization-server",
                         "/.well-known/oauth-protected-resource"):
                assert (await http(base + path))[0] == 404
            assert (await http(base + "/token", b"grant_type=x"))[0] == 404
        finally:
            await hub.stop()
    run(go())


def test_a_bad_address_stops_the_hub_starting(tmp_path):
    async def go():
        cfg = config(tmp_path)
        cfg.doors = {"mcp": {"bind": "127.0.0.1:0", "oauth": "http://rook.example"}}
        hub = runtime.Hub(cfg, transport=runtime.Loopback())
        with pytest.raises(ValueError, match="https"):
            await hub.start()
        await hub.stop()
    run(go())
