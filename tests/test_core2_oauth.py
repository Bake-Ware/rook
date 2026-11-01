"""The MCP door's OAuth shim (rook/core2/hub/doors/mcp_oauth.py): adding
claude.ai's connector is paste the address, sign in with your Rook user name
and password, done. /authorize is a sign-in form; /token mints the person's
token through the connector's agent. Unit tests on the shim, then the whole
flow over a real door on a hub in-process. Every test runs under a hard
timeout."""
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

from rook.core2.hub import passwords, runtime  # noqa: E402
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


class Minter:
    """The door's mint: a token per signed-in person, remembered like tokens.json."""

    def __init__(self):
        self.tokens = {"person-tok": PERSON}
        self.problem = None

    def __call__(self, person):
        if self.problem:
            raise ValueError(self.problem)
        tok = f"tok-{len(self.tokens)}"
        self.tokens[tok] = runtime.Caller(person=person, agent="agent:claude")
        return tok


def shim(clock=None, mint=None):
    mint = mint or Minter()
    s = OAuthShim("https://rook.example", "/mcp", mint.tokens.get, mint, lambda: mint.problem, clock or Clock())
    s.minter = mint
    return s


def params(**over):
    return {"response_type": "code", "client_id": "claude", "redirect_uri": CB, "code_challenge": CHALLENGE,
            "code_challenge_method": "S256", "state": "st", **over}


def authorize(s, person="person:bake", **over):
    """The sign-in form posted: ``person`` is who the door's password check found."""
    return s.handle("POST", "/authorize", "", urlencode({**params(**over), "user": "bake", "password": "x"}).encode(),
                    person=person)


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


def exchange(s, code, **over):
    return token(s, **{"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
                       "redirect_uri": CB, "client_id": "claude", **over})


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
    assert "none" in meta["token_endpoint_auth_methods_supported"]           # a public client: no secret
    assert "registration_endpoint" not in meta
    assert s.owns("GET", "/authorize") and s.owns("POST", "/authorize") and s.owns("POST", "/token")
    assert not s.owns("GET", "/token") and not s.owns("POST", "/mcp") and not s.owns("GET", "/oauth/register")


def test_authorize_shows_a_sign_in_form_carrying_the_request():
    s = shim()
    status, page, headers = s.handle("GET", "/authorize", urlencode(params(state='"><script>x</script>')), b"")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"] and "Location" not in headers
    text = page.decode()
    assert 'name="user"' in text and 'name="password"' in text and 'action="/authorize"' in text
    assert f'name="code_challenge" value="{CHALLENGE}"' in text and "claude.ai" in text
    assert "<script>x" not in text and "&lt;script&gt;" in text               # escaped
    assert s._codes == {}                                                     # nothing until signed in


def test_a_wrong_password_shows_the_form_again():
    s = shim()
    status, page, headers = authorize(s, person=None)
    assert status == 200 and b"Wrong user name or password." in page and "Location" not in headers
    assert s._codes == {}


def test_signing_in_mints_the_persons_token_through_the_connector_agent():
    s = shim()
    status, body, headers = exchange(s, code_of(authorize(s)))
    assert status == 200 and body["access_token"] == body["refresh_token"] == "tok-1"
    assert body["token_type"] == "Bearer" and headers == {"Pragma": "no-cache"}
    assert s.minter.tokens["tok-1"] == AGENT
    status, body, _ = token(s, grant_type="refresh_token", refresh_token="tok-1")
    assert status == 200 and body["access_token"] == "tok-1"


def test_a_client_secret_is_not_needed_and_ignored():
    s = shim()
    basic = "Basic " + base64.b64encode(b"claude:anything").decode()
    form = {"grant_type": "authorization_code", "code": code_of(authorize(s)), "code_verifier": VERIFIER}
    status, body, _ = token(s, basic, **form)
    assert status == 200 and body["access_token"] == "tok-1"


def test_refreshes_need_a_connector_token():
    s = shim()
    assert token(s, grant_type="refresh_token", refresh_token="person-tok")[0] == 400   # a person's own
    assert token(s, grant_type="refresh_token", refresh_token="gone")[1]["error"] == "invalid_grant"
    assert token(s, grant_type="password")[1]["error"] == "unsupported_grant_type"


def test_a_hub_without_the_connector_agent_says_so():
    s = shim()
    s.minter.problem = "This hub has no agent 'claude-web' for the connector yet"
    status, page, _ = s.handle("GET", "/authorize", urlencode(params()), b"")
    assert status == 200 and b"no agent" in page
    code = code_of(authorize(s))
    status, body, _ = exchange(s, code)
    assert status == 400 and "no agent" in body["error_description"]


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
    assert exchange(s, code_of(authorize(s, code_challenge="é" * 43)))[0] == 400     # no 500 on odd text
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
    for reply in (authorize(shim(), **over), shim().handle("GET", "/authorize", urlencode(params(**over)), b"")):
        status, body, headers = reply
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


def http(url, data=None, headers=None, method=None, raw=False):
    """(status, headers, parsed body or None), in a thread, never following a redirect.
    A page (text/html) comes back as bytes."""
    def go():
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _open(req, timeout=10) as r:
                status, hdrs, text = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, text = e.code, e.headers, e.read()
        if raw or (hdrs.get("Content-Type") or "").startswith("text/html"):
            return status, hdrs, text
        return status, hdrs, json.loads(text) if text else None
    return asyncio.to_thread(go)


async def start(tmp_path, oauth=True, agent="claude"):
    band = runtime.Loopback()
    cfg = config(tmp_path)
    cfg.doors = {"mcp": {"bind": "127.0.0.1:0",
                         **({"oauth": "http://127.0.0.1:1", "oauth_agent": agent} if oauth else {})}}
    hub = runtime.Hub(cfg, transport=band)
    await hub.start()
    w = FakeWorker(hub, band, tmp_path / "w")
    await w.announce()
    await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
    return hub, f"http://127.0.0.1:{hub.doors[0].port}"


def rpc_body(method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()


def form_post(url, fields):
    return http(url, urlencode(fields).encode(), {"Content-Type": "application/x-www-form-urlencoded"})


def test_claude_ai_flow_end_to_end(tmp_path):
    """What claude.ai does, scripted: discover, open /authorize, sign in with
    the user name and password, swap the code, call a tool, refresh."""
    async def go():
        hub, base = await start(tmp_path)
        try:
            passwords.set_password(hub.data, "bake", "open sesame", by="passwd")
            status, hdrs, _ = await http(base + "/mcp", rpc_body("ping"), {"Content-Type": "application/json"})
            assert status == 401
            meta_url = hdrs["WWW-Authenticate"].split('resource_metadata="')[1].rstrip('"')
            assert meta_url == "http://127.0.0.1:1/.well-known/oauth-protected-resource/mcp"
            status, _, prm = await http(base + urlsplit(meta_url).path)
            assert status == 200 and prm["resource"] == "http://127.0.0.1:1/mcp"
            status, _, meta = await http(base + "/.well-known/oauth-authorization-server")
            assert status == 200 and "registration_endpoint" not in meta

            status, hdrs, page = await http(base + "/authorize?" + urlencode(params()), raw=True)
            assert status == 200 and b'name="password"' in page and hdrs["Content-Type"].startswith("text/html")
            for user, pw in (("bake", "wrong"), ("nobody", "open sesame"), ("", "")):
                status, hdrs, page = await form_post(base + "/authorize", {**params(), "user": user, "password": pw})
                assert status == 200 and b"Wrong user name or password." in page and "Location" not in hdrs
            status, hdrs, _ = await form_post(base + "/authorize",
                                              {**params(), "user": "Bake ", "password": "open sesame"})
            assert status == 302
            code = parse_qs(urlsplit(hdrs["Location"]).query)["code"][0]
            status, hdrs, tok = await form_post(base + "/token", {
                "grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "redirect_uri": CB,
                "client_id": "claude"})
            assert status == 200 and hdrs["Cache-Control"] == "no-store", tok
            assert hub.authenticate(tok["access_token"]) == AGENT          # the person, through the agent
            stored = json.loads((hub.data / "tokens.json").read_text())[runtime.digest(tok["access_token"])]
            assert stored["label"] == "claude.ai connector"

            bearer = {"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json"}
            call = {"name": "call", "arguments": {"worker": WID, "cap": "echo.say", "args": {"text": "hi"},
                                                  "why": WHY}}
            status, _, out = await http(base + "/mcp", rpc_body("tools/call", call), bearer)
            assert status == 200 and out["result"]["isError"] is False, out
            entry = (await hub.journal_view(AGENT, {"cap": "echo.say"}))["entries"][0]
            assert (entry["agent"], entry["person"]) == ("agent:claude", "person:bake")

            refresh = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}
            assert (await form_post(base + "/token", refresh))[0] == 200
            tokens = hub.data / "tokens.json"                 # revoke = take the token out of tokens.json
            stored = json.loads(tokens.read_text())
            stored.pop(runtime.digest(tok["access_token"]))
            tokens.write_text(json.dumps(stored))
            await until(lambda: hub.authenticate(tok["access_token"]) is None)
            assert (await form_post(base + "/token", refresh))[0] == 400
            assert (await http(base + "/mcp", rpc_body("ping"), bearer))[0] == 401
        finally:
            await hub.stop()
    run(go())


def test_the_form_says_when_the_connector_agent_is_missing(tmp_path):
    async def go():
        hub, base = await start(tmp_path, agent="claude-web")
        try:
            status, _, page = await http(base + "/authorize?" + urlencode(params()), raw=True)
            assert status == 200 and b"no agent &#x27;claude-web&#x27;" in page
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


# -- token clients: an agent-minted token, no password typed ----------------------------

def test_a_token_client_goes_straight_through_and_needs_its_token():
    """client_id rook-<token id> skips the form; /token gives back only that token,
    and only when the client secret is it (castling-switchover-decisions,
    'Claude connects itself')."""
    s = shim()
    good = "agent-tok"
    s.minter.tokens[good] = AGENT
    tid = hashlib.sha256(good.encode()).hexdigest()[:12]
    s.token_client = lambda cid: cid == f"rook-{tid}"
    cid = f"rook-{tid}"
    # no form, straight to the callback
    code = code_of(s.handle("GET", "/authorize", urlencode(params(client_id=cid)), b""))
    status, out, _ = exchange(s, code, client_id=cid)           # no secret
    assert status == 401 and out["error"] == "invalid_client"
    code = code_of(s.handle("GET", "/authorize", urlencode(params(client_id=cid)), b""))
    status, out, _ = exchange(s, code, client_id=cid, client_secret="person-tok")   # a different token
    assert status == 401
    code = code_of(s.handle("GET", "/authorize", urlencode(params(client_id=cid)), b""))
    basic = "Basic " + base64.b64encode(f"{cid}:{good}".encode()).decode()
    status, out, _ = exchange(s, code, auth=basic, client_id=cid)
    assert status == 200 and out["access_token"] == out["refresh_token"] == good
    assert s.minter.tokens.keys() == {"person-tok", good}       # nothing new minted
    # an unknown rook- id, or any other client id, still gets the sign-in form
    for other in (f"rook-{'0' * 12}", "claude"):
        status, page, _ = s.handle("GET", "/authorize", urlencode(params(client_id=other)), b"")
        assert status == 200 and b'name="password"' in page


def test_token_create_names_a_connector_that_works_without_a_password(tmp_path):
    async def go():
        hub, base = await start(tmp_path)
        try:
            minted = await hub.call(PERSON, {"worker": "rook", "cap": "token.create",
                                            "args": {"agent": "claude-web"}, "why": WHY}, "test")
            assert minted["ok"], minted
            conn = minted["result"]["connector"]
            assert conn["client_id"] == "rook-" + minted["result"]["id"]
            assert conn["url"] == "http://127.0.0.1:1/mcp"
            q = params(client_id=conn["client_id"])
            status, hdrs, _ = await http(base + "/authorize?" + urlencode(q))
            assert status == 302, status                              # no sign-in page
            code = parse_qs(urlsplit(hdrs["Location"]).query)["code"][0]
            status, _, tok = await form_post(base + "/token", {
                "grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "redirect_uri": CB,
                "client_id": conn["client_id"], "client_secret": conn["client_secret"]})
            assert status == 200 and tok["access_token"] == conn["client_secret"], tok
            bearer = {"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json"}
            call = {"name": "call", "arguments": {"worker": WID, "cap": "echo.say", "args": {"text": "hi"},
                                                  "why": WHY}}
            status, _, out = await http(base + "/mcp", rpc_body("tools/call", call), bearer)
            assert status == 200 and out["result"]["isError"] is False, out
        finally:
            await hub.stop()
    run(go())
