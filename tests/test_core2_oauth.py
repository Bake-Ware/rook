"""OAuth 2.1 on the 0.5 MCP door (rook/core2/hub/oauth.py, gate R13): a fake
remote MCP client (the shape of claude.ai's connector) discovers the door,
registers, sends a person through sign-in and consent on the hub site, trades
the code with PKCE, calls tools as the chosen agent for the person, refreshes
and is revoked. Plus the unhappy paths and the store on its own.

A hub runs in-process on a loopback band with the fake worker from
test_core2_hub_runtime. Every test runs under a hard timeout; HTTP requests
run in a thread with their own socket timeout."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.request
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from rook.core2.hub import runtime  # noqa: E402
from rook.core2.hub.oauth import ACCESS_SECS, REFRESH_IDLE, OAuth, OAuthError, check_options  # noqa: E402
from test_core2_hub_runtime import WHY, WID, FakeWorker, config, run, until  # noqa: E402

ISSUER, RESOURCE = "http://localhost:8740", "http://localhost:8741/mcp"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def request(url, method="GET", body=None, form=None, headers=None):
    """(status, headers, bytes) in a thread; redirects are returned, not followed."""
    def go():
        h, data = dict(headers or {}), None
        if form is not None:
            data, h["Content-Type"] = urlencode(form).encode(), "application/x-www-form-urlencoded"
        elif body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with _opener.open(r, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
    return asyncio.to_thread(go)


async def jreq(url, method="GET", **kw):
    status, headers, data = await request(url, method, **kw)
    return status, headers, json.loads(data) if data else None


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class Hub:
    """A hub with the site (http) and the MCP door, OAuth on."""

    def __init__(self, hub, worker):
        self.hub, self.worker = hub, worker
        ports = {d.name: d.port for d in hub.doors}
        self.site, self.mcp = f"http://127.0.0.1:{ports['http']}", f"http://127.0.0.1:{ports['mcp']}"


@pytest.fixture
def oauth_hub(tmp_path):
    hubs = []

    async def start(oauth=True, **over):
        band = runtime.Loopback()
        cfg = config(tmp_path, **over)
        cfg.agents = {"claude": {"grants": ["*"], "max_risk": "exec"}, "reader": {"grants": ["echo.say"]}}
        cfg.doors = {"http": {"bind": "127.0.0.1:0"}, "mcp": {"bind": "127.0.0.1:0"}}
        if oauth:
            cfg.oauth = {"issuer": ISSUER, "resource": RESOURCE}
        runtime.check_config(cfg)
        hub = runtime.Hub(cfg, transport=band)
        await hub.start()
        hubs.append(hub)
        w = FakeWorker(hub, band, tmp_path / f"w{len(hubs)}")
        await w.announce()
        await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
        return Hub(hub, w)
    return start


class Browser:
    """The person's browser on the hub site: keeps the sign-in cookie."""

    def __init__(self, site):
        self.site, self.cookie = site, ""

    async def get(self, path, headers=None):
        h = {"Cookie": self.cookie} if self.cookie else {}
        return await request(self.site + path, headers={**h, **(headers or {})})

    async def post(self, path, form, headers=None):
        h = {"Cookie": self.cookie} if self.cookie else {}
        return await request(self.site + path, "POST", form=form, headers={**h, **(headers or {})})

    async def sign_in(self, token, next_path=""):
        status, headers, _ = await self.post("/login", {"token": token, "next": next_path})
        assert status == 303, status
        self.cookie = headers["Set-Cookie"].split(";")[0]
        return headers["Location"]


class FakeClient:
    """A remote MCP client: discovery, registration, the code flow, tokens."""

    def __init__(self, h: Hub, method="none", redirect=CALLBACK):
        self.h, self.method, self.redirect = h, method, redirect
        self.client_id = self.secret = None
        self.verifier, self.challenge = pkce()
        self.state = secrets.token_urlsafe(8)

    async def register(self):
        status, _, out = await jreq(self.h.site + "/oauth/register", "POST", body={
            "redirect_uris": [self.redirect], "client_name": "Claude <b>", "token_endpoint_auth_method": self.method,
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert status == 201, out
        self.client_id, self.secret = out["client_id"], out.get("client_secret")
        return out

    def authorize_path(self, **over):
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect,
             "code_challenge": self.challenge, "code_challenge_method": "S256", "state": self.state,
             "scope": "rook", "resource": RESOURCE, **over}
        return "/oauth/authorize?" + urlencode({k: v for k, v in q.items() if v is not None})

    async def token(self, **form):
        if self.method == "client_secret_post":
            form.update(client_secret=self.secret)
        headers = {}
        if self.method == "client_secret_basic":
            headers["Authorization"] = "Basic " + base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
        else:
            form.setdefault("client_id", self.client_id)
        return await jreq(self.h.site + "/oauth/token", "POST", form=form, headers=headers)

    async def exchange(self, code, **over):
        return await self.token(**{"grant_type": "authorization_code", "code": code, "code_verifier": self.verifier,
                                   "redirect_uri": self.redirect, "resource": RESOURCE, **over})


def consent_fields(html: str) -> dict:
    return {k: unescape(re.search(rf'name="{k}" value="([^"]*)"', html).group(1)) for k in ("consent", "csrf")}


async def allow(browser: Browser, client: FakeClient, agent="claude", decision="allow"):
    """The person's part: the consent page, then their answer. -> the redirect's query."""
    status, headers, page = await browser.get(client.authorize_path())
    assert status == 200, page
    status, headers, _ = await browser.post("/oauth/authorize", {**consent_fields(page.decode()), "agent": agent,
                                                                 "decision": decision})
    assert status == 303
    loc = urlsplit(headers["Location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == client.redirect
    return {k: v[0] for k, v in parse_qs(loc.query).items()}


async def mcp_call(h: Hub, token, text="hi"):
    """(status, tool result or None)."""
    status, _, out = await jreq(h.mcp + "/mcp", "POST", body={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "call", "arguments": {"worker": "kaiju", "cap": "echo.say", "args": {"text": text},
                                                 "why": WHY}}}, headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        return status, None
    return status, json.loads(out["result"]["content"][0]["text"])


async def connected(h: Hub, browser=None, agent="claude", method="none"):
    """Register, sign bake in, allow, exchange: (client, tokens)."""
    client = FakeClient(h, method)
    await client.register()
    browser = browser or Browser(h.site)
    if not browser.cookie:
        await browser.sign_in(h.hub.add_token(runtime.Caller(person="person:bake")))
    q = await allow(browser, client, agent)
    status, _, tokens = await client.exchange(q["code"])
    assert status == 200, tokens
    return client, tokens


# -- the whole flow ---------------------------------------------------------------

def test_full_flow(oauth_hub):
    async def go():
        h = await oauth_hub()
        hub = h.hub
        # 1. discovery: the door's 401 names its metadata, which names the hub site
        status, headers, _ = await jreq(h.mcp + "/mcp", "POST", body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert status == 401
        assert headers["WWW-Authenticate"] == \
            'Bearer resource_metadata="http://localhost:8741/.well-known/oauth-protected-resource/mcp"'
        for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
            status, _, prm = await jreq(h.mcp + path)
            assert status == 200 and prm["resource"] == RESOURCE and prm["authorization_servers"] == [ISSUER]
        status, _, meta = await jreq(h.site + "/.well-known/oauth-authorization-server")
        assert status == 200 and meta["issuer"] == ISSUER
        assert meta["authorization_endpoint"] == ISSUER + "/oauth/authorize"
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert "none" in meta["token_endpoint_auth_methods_supported"]
        assert (await jreq(h.mcp + "/.well-known/oauth-authorization-server"))[2] == meta    # older clients look here

        # 2. registration
        client = FakeClient(h)
        reg = await client.register()
        assert reg["client_id"].startswith("rc_") and "client_secret" not in reg
        assert reg["client_name"] == "Claude <b>"

        # 3. the person: arriving from claude.ai the Strict cookie stays behind, so the page reloads itself
        browser = Browser(h.site)
        path = client.authorize_path()
        status, headers, page = await browser.get(path, {"Sec-Fetch-Site": "cross-site"})
        assert status == 200 and b'http-equiv="refresh" content="0; url=/oauth/authorize?' in page
        status, _, page = await browser.get(path + "&rook_again=1", {"Sec-Fetch-Site": "cross-site"})
        assert status == 200 and b"Continue</a>" in page and b"refresh" not in page     # never a loop
        status, headers, _ = await browser.get(path, {"Sec-Fetch-Site": "same-origin"})
        assert status == 303 and headers["Location"].startswith("/login?next=%2Foauth%2Fauthorize%3F")
        nxt = parse_qs(urlsplit(headers["Location"]).query)["next"][0]
        assert nxt == path
        bake = hub.add_token(runtime.Caller(person="person:bake"))
        assert await browser.sign_in(bake, nxt) == path
        status, headers, page = await browser.get(path)
        html = page.decode()
        assert status == 200 and "Claude &lt;b&gt;" in html and "<b>claude.ai</b>" in html      # escaped; where it goes
        assert 'value="claude"' in html and 'value="reader"' in html
        assert "form-action 'self' https://claude.ai" in headers["Content-Security-Policy"]
        assert headers["X-Frame-Options"] == "DENY"
        status, headers, _ = await browser.post("/oauth/authorize", {**consent_fields(html), "agent": "claude",
                                                                     "decision": "allow"})
        assert status == 303
        loc = urlsplit(headers["Location"])
        q = {k: v[0] for k, v in parse_qs(loc.query).items()}
        assert f"https://{loc.netloc}{loc.path}" == CALLBACK
        assert q["state"] == client.state and q["iss"] == ISSUER and q["code"]

        # 4. the code, with PKCE
        status, _, tokens = await client.exchange(q["code"])
        assert status == 200, tokens
        assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == ACCESS_SECS
        assert tokens["access_token"].startswith("rook_at_") and tokens["refresh_token"].startswith("rook_rt_")
        grant = hub.journal.query(kind="connector")[0]
        assert (grant["person"], grant["agent"], grant["name"]) == ("person:bake", "agent:claude", "Claude <b>")

        # 5. the door: the agent acting for the person, through dispatch
        status, out = await mcp_call(h, tokens["access_token"])
        assert status == 200 and out["result"] == {"said": "hi", "person": "person:bake", "agent": "agent:claude"}
        t = h.worker.verified[-1]
        assert (t["person"], t["agent"]) == ("person:bake", "agent:claude")
        entry = hub.journal.query(call=out["id"])[0]
        assert (entry["person"], entry["agent"], entry["door"] if "door" in entry else "mcp") == \
            ("person:bake", "agent:claude", "mcp")
        # an access token is for the MCP door only, not the HTTP door or the site
        status, _, _ = await jreq(h.site + "/workers", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert status == 401
        # bearer tokens keep working on the door
        bearer = hub.add_token(runtime.Caller(person="person:bake", agent="agent:claude"))
        assert (await mcp_call(h, bearer))[0] == 200

        # 6. refresh: rotated; the old access token lives out its hour
        status, _, fresh = await client.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert status == 200 and fresh["refresh_token"] != tokens["refresh_token"]
        assert (await mcp_call(h, fresh["access_token"]))[0] == 200
        assert (await mcp_call(h, tokens["access_token"]))[0] == 200

        # 7. the dashboard lists it and revokes it: every token stops
        listed = await hub.call(runtime.Caller(person="person:bake"), {
            "worker": "rook", "cap": "connector.list", "why": WHY}, "http")
        [c] = listed["result"]["connectors"]
        assert (c["name"], c["agent"], c["person"]) == ("Claude <b>", "agent:claude", "person:bake")
        out = await hub.call(runtime.Caller(person="person:bake"), {
            "worker": "rook", "cap": "connector.revoke", "args": {"id": c["id"]}, "why": WHY}, "http")
        assert out["ok"], out
        for tok in (tokens["access_token"], fresh["access_token"]):
            assert (await mcp_call(h, tok))[0] == 401
        status, _, err = await client.token(grant_type="refresh_token", refresh_token=fresh["refresh_token"])
        assert status == 400 and err["error"] == "invalid_grant"
        await hub.stop()
    run(go(), timeout=60)


def test_survives_restart(oauth_hub, tmp_path):
    async def go():
        h = await oauth_hub()
        client, tokens = await connected(h)
        await h.hub.stop()
        h2 = await oauth_hub()
        assert (await mcp_call(h2, tokens["access_token"]))[0] == 200
        client.h = h2
        status, _, fresh = await client.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert status == 200, fresh
        assert oct((tmp_path / "data" / "oauth.json").stat().st_mode & 0o777) == "0o600"
        stored = (tmp_path / "data" / "oauth.json").read_text()
        assert tokens["access_token"] not in stored and fresh["refresh_token"] not in stored
        await h2.hub.stop()
    run(go(), timeout=60)


def test_rights_intersect_and_identity_checks(oauth_hub):
    async def go():
        h = await oauth_hub()
        hub = h.hub
        # kid (child preset) allows the all-powerful agent: only what a child may do
        kid = Browser(h.site)
        await kid.sign_in(hub.add_token(runtime.Caller(person="person:kid")))
        _, tokens = await connected(h, kid)
        status, out = await mcp_call(h, tokens["access_token"])
        assert status == 200 and not out["ok"] and out["error"].startswith("denied")
        # bake chooses the narrow agent: echo.say only
        _, narrow = await connected(h, agent="reader")
        assert (await mcp_call(h, narrow["access_token"]))[1]["ok"]
        out = (await jreq(h.mcp + "/mcp", "POST", body={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "call", "arguments": {
                "worker": "kaiju", "cap": "echo.relay", "args": {"text": "x"}, "why": WHY}}},
            headers={"Authorization": f"Bearer {narrow['access_token']}"}))[2]
        assert json.loads(out["result"]["content"][0]["text"])["error"].startswith("denied")
        # the agent taken out of the config: its connector's tokens are nobody, and it can't refresh
        del hub.identities.agents["reader"]
        assert (await mcp_call(h, narrow["access_token"]))[0] == 401
        reader = next(c for c in hub.oauth.connectors() if c["agent"] == "agent:reader")
        status, _, err = await jreq(h.site + "/oauth/token", "POST", form={
            "grant_type": "refresh_token", "refresh_token": narrow["refresh_token"], "client_id": reader["client"]})
        assert status == 400 and err["error"] == "invalid_grant"
        assert hub.oauth.get(reader["id"]) is None
        # closing the mcp door refuses connectors like every agent
        _, toks = await connected(h)
        await hub.set_door(runtime.Caller(person="person:bake"), "mcp", False)
        assert (await mcp_call(h, toks["access_token"]))[0] == 503
        await hub.set_door(runtime.Caller(person="person:bake"), "mcp", True)
        # connector.list: people see their own, owners all; kid can't revoke bake's
        mine = await hub.call(runtime.Caller(person="person:kid"), {
            "worker": "rook", "cap": "connector.list", "why": WHY}, "http")
        assert mine["ok"] is False or all(c["person"] == "person:kid" for c in mine["result"]["connectors"])
        everyone = (await hub.call(runtime.Caller(person="person:bake"), {
            "worker": "rook", "cap": "connector.list", "why": WHY}, "http"))["result"]["connectors"]
        assert {c["person"] for c in everyone} == {"person:bake", "person:kid"}
        bakes = next(c["id"] for c in everyone if c["person"] == "person:bake")
        assert hub.oauth.revoke(bakes, "test") is not None
        await hub.stop()
    run(go(), timeout=60)


def test_kid_cannot_revoke_anothers(oauth_hub):
    async def go():
        h = await oauth_hub()
        hub = h.hub
        await connected(h)
        [c] = hub.oauth.connectors()
        hub.identities.presets["child"]["grants"].append("connector.*")
        hub.identities.presets["child"]["effects"] = ["persists"]
        hub.identities.presets["child"].pop("approve_new", None)
        out = await hub.call(runtime.Caller(person="person:kid"), {
            "worker": "rook", "cap": "connector.revoke", "args": {"id": c["id"]}, "why": WHY}, "http")
        assert not out["ok"] and "no connector" in out["error"], out
        assert hub.oauth.get(c["id"]) is not None
        out = await hub.call(runtime.Caller(person="person:kid"), {
            "worker": "rook", "cap": "connector.list", "why": WHY}, "http")
        assert out["ok"] and out["result"]["connectors"] == []
        await hub.stop()
    run(go(), timeout=60)


# -- refusals -----------------------------------------------------------------------

def test_codes_and_refresh_tokens_are_single_use(oauth_hub):
    async def go():
        h = await oauth_hub()
        client = FakeClient(h)
        await client.register()
        browser = Browser(h.site)
        await browser.sign_in(h.hub.add_token(runtime.Caller(person="person:bake")))
        q = await allow(browser, client)
        status, _, tokens = await client.exchange(q["code"])
        assert status == 200
        # the same code again: refused, and the connector it made ends (it leaked)
        status, _, err = await client.exchange(q["code"])
        assert status == 400 and err["error"] == "invalid_grant"
        assert (await mcp_call(h, tokens["access_token"]))[0] == 401

        _, tokens = await connected(h, browser)
        c2 = h.hub.oauth.connectors()[0]["client"]
        refresher = FakeClient(h)
        refresher.client_id = c2
        status, _, fresh = await refresher.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert status == 200
        # the rotated-out refresh token comes back: someone has a copy, the connector ends
        status, _, err = await refresher.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert status == 400 and err["error"] == "invalid_grant"
        assert (await mcp_call(h, fresh["access_token"]))[0] == 401
        assert h.hub.oauth.connectors() == []
        await h.hub.stop()
    run(go(), timeout=60)


def test_bad_requests(oauth_hub):
    async def go():
        h = await oauth_hub()
        client = FakeClient(h)
        await client.register()
        browser = Browser(h.site)
        await browser.sign_in(h.hub.add_token(runtime.Caller(person="person:bake")))

        # an unknown client or an unregistered redirect: shown to the person, never redirected
        for path in (client.authorize_path(client_id="rc_nobody"),
                     client.authorize_path(redirect_uri="https://evil.example/cb")):
            status, headers, page = await browser.get(path)
            assert status == 400 and "Location" not in headers and b"This app can" in page
        # the rest go back to the client with an error
        for over, error in (({"code_challenge": None}, "invalid_request"),
                            ({"code_challenge_method": "plain"}, "invalid_request"),
                            ({"response_type": "token"}, "unsupported_response_type"),
                            ({"resource": "https://other.example/mcp"}, "invalid_target")):
            status, headers, _ = await browser.get(client.authorize_path(**over))
            assert status == 303, over
            q = parse_qs(urlsplit(headers["Location"]).query)
            assert q["error"] == [error] and q["state"] == [client.state], over

        # the person says no
        q = await allow(browser, client, decision="deny")
        assert q["error"] == "access_denied" and "code" not in q

        # the consent: needs its csrf, its own session, a real agent, once
        status, _, page = await browser.get(client.authorize_path())
        fields = consent_fields(page.decode())
        status, _, _ = await browser.post("/oauth/authorize", {**fields, "csrf": "x", "agent": "claude",
                                                               "decision": "allow"})
        assert status == 403
        status, _, _ = await browser.post("/oauth/authorize", {**fields, "agent": "nobody", "decision": "allow"})
        assert status == 400
        other = Browser(h.site)
        await other.sign_in(h.hub.add_token(runtime.Caller(person="person:kid")))
        _, _, page2 = await other.get(client.authorize_path())
        status, _, _ = await other.post("/oauth/authorize", {**fields, "csrf": consent_fields(page2.decode())["csrf"],
                                                             "agent": "claude", "decision": "allow"})
        assert status == 400                     # bake's consent id, kid's session
        status, _, _ = await browser.post("/oauth/authorize", {**fields, "agent": "claude", "decision": "allow"},
                                          {"Sec-Fetch-Site": "cross-site"})
        assert status == 403
        status, _, _ = await browser.post("/oauth/authorize", {**fields, "agent": "claude", "decision": "allow"})
        assert status == 303
        status, _, _ = await browser.post("/oauth/authorize", {**fields, "agent": "claude", "decision": "allow"})
        assert status == 400                     # used

        # PKCE: a wrong verifier, a missing one
        q = await allow(browser, client)
        status, _, err = await client.exchange(q["code"], code_verifier=pkce()[0])
        assert status == 400 and err["error"] == "invalid_grant"
        status, _, err = await client.exchange(q["code"], code_verifier="")
        assert status == 400 and err["error"] == "invalid_grant"
        q = await allow(browser, client)
        status, _, err = await client.exchange(q["code"], redirect_uri="https://claude.ai/other")
        assert status == 400 and err["error"] == "invalid_grant"
        # another client can't use this client's code
        thief = FakeClient(h)
        await thief.register()
        thief.verifier = client.verifier
        status, _, err = await thief.exchange(q["code"])
        assert status == 400 and err["error"] == "invalid_grant"
        status, _, err = await client.token(grant_type="password")
        assert status == 400 and err["error"] == "unsupported_grant_type"
        status, _, err = await jreq(h.site + "/oauth/token", "POST", body={"grant_type": "authorization_code"})
        assert status == 400 and err["error"] == "invalid_request"          # JSON, not a form

        # registration
        for body in ({"redirect_uris": ["http://evil.example/cb"]}, {"redirect_uris": []},
                     {"redirect_uris": ["https://ok.example/cb#frag"]}, {"redirect_uris": ["javascript:alert(1)"]}):
            status, _, err = await jreq(h.site + "/oauth/register", "POST", body=body)
            assert status == 400 and err["error"] == "invalid_redirect_uri", body
        status, _, err = await jreq(h.site + "/oauth/register", "POST", body={
            "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "private_key_jwt"})
        assert status == 400 and err["error"] == "invalid_client_metadata"
        status, _, ok = await jreq(h.site + "/oauth/register", "POST", body={"redirect_uris": ["http://127.0.0.1:9/cb"]})
        assert status == 201 and ok["token_endpoint_auth_method"] == "client_secret_basic" and ok["client_secret"]

        # sign-in only follows next back into an authorization
        b2 = Browser(h.site)
        assert await b2.sign_in(h.hub.add_token(runtime.Caller(person="person:bake")), "https://evil.example/") == "/"
        assert await b2.sign_in(h.hub.add_token(runtime.Caller(person="person:bake")),
                                "/oauth/authorize?x\r\nSet-Cookie: a=b") == "/"
        # an access token is not a sign-in token
        _, tokens = await connected(h, browser)
        status, headers, _ = await request(h.site + "/login", "POST", form={"token": tokens["access_token"]})
        assert status == 303 and headers["Location"] == "/login?failed=1"
        await h.hub.stop()
    run(go(), timeout=90)


def test_confidential_clients(oauth_hub):
    async def go():
        h = await oauth_hub()
        for method in ("client_secret_basic", "client_secret_post"):
            client, tokens = await connected(h, method=method)
            assert client.secret and (await mcp_call(h, tokens["access_token"]))[0] == 200
            good = client.secret
            client.secret = "wrong"
            status, headers, err = await client.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
            assert status == 401 and err["error"] == "invalid_client"
            assert headers["WWW-Authenticate"].startswith("Basic")
            client.secret = good
            # RFC 7009: the client revokes its refresh token, the connector ends
            form = {"token": tokens["refresh_token"]}
            hdrs = {}
            if method == "client_secret_basic":
                hdrs["Authorization"] = "Basic " + base64.b64encode(f"{client.client_id}:{good}".encode()).decode()
            else:
                form.update(client_id=client.client_id, client_secret=good)
            status, _, _ = await request(h.site + "/oauth/revoke", "POST", form=form, headers=hdrs)
            assert status == 200
            assert (await mcp_call(h, tokens["access_token"]))[0] == 401
        await h.hub.stop()
    run(go(), timeout=60)


def test_off_without_config(oauth_hub):
    async def go():
        h = await oauth_hub(oauth=False)
        status, headers, _ = await jreq(h.mcp + "/mcp", "POST", body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
        for url in (h.site + "/.well-known/oauth-authorization-server", h.site + "/oauth/authorize",
                    h.mcp + "/.well-known/oauth-protected-resource"):
            assert (await request(url))[0] == 404, url
        out = await h.hub.call(runtime.Caller(person="person:bake"), {
            "worker": "rook", "cap": "connector.list", "why": WHY}, "http")
        assert out["result"] == {"enabled": False, "connectors": []}
        await h.hub.stop()
    run(go(), timeout=60)


# -- the store on its own --------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def _grant(o: OAuth) -> tuple[dict, dict]:
    reg = o.register({"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    c = o.begin({"client_id": reg["client_id"], "response_type": "code", "code_challenge": challenge,
                 "code_challenge_method": "S256"}, "sess")
    code = parse_qs(urlsplit(o.decide(c["id"], "sess", "person:bake", "agent:claude")).query)["code"][0]
    reply, grant = o.token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                            "client_id": reg["client_id"]}, None)
    return reg, reply


def test_store_expiry(tmp_path):
    clock = Clock()
    o = OAuth(tmp_path, ISSUER, RESOURCE, clock=clock)
    reg, reply = _grant(o)
    assert o.authenticate(reply["access_token"])[:2] == ("person:bake", "agent:claude")
    assert o.authenticate(reply["refresh_token"]) is None           # a refresh token is not an access token
    clock.now += ACCESS_SECS
    assert o.authenticate(reply["access_token"]) is None            # an hour, then refresh
    clock.now += REFRESH_IDLE - ACCESS_SECS - 1
    reply, _ = o.token({"grant_type": "refresh_token", "refresh_token": reply["refresh_token"],
                        "client_id": reg["client_id"]}, None)      # refreshed in time: 30 more days
    clock.now += REFRESH_IDLE
    with pytest.raises(OAuthError) as e:
        o.token({"grant_type": "refresh_token", "refresh_token": reply["refresh_token"],
                 "client_id": reg["client_id"]}, None)
    assert e.value.error == "invalid_grant" and o.connectors() == []
    # an expired code, an expired consent
    verifier, challenge = pkce()
    c = o.begin({"client_id": reg["client_id"], "response_type": "code", "code_challenge": challenge,
                 "code_challenge_method": "S256"}, "sess")
    clock.now += 601
    with pytest.raises(OAuthError):
        o.decide(c["id"], "sess", "person:bake", "agent:claude")


def test_store_client_room(tmp_path, monkeypatch):
    from rook.core2.hub import oauth as mod
    monkeypatch.setattr(mod, "MAX_CLIENTS", 3)
    o = OAuth(tmp_path, ISSUER, RESOURCE, clock=Clock())
    kept, _ = _grant(o)                            # a client someone allowed stays
    for _ in range(5):
        o.register({"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
    assert len(o.clients) == 3 and kept["client_id"] in o.clients
    o2 = OAuth(tmp_path, ISSUER, RESOURCE)
    assert set(o2.clients) == set(o.clients) and len(o2.connectors()) == 1


def test_config_checked(tmp_path):
    for bad in ({"issuer": "http://hub.example", "resource": RESOURCE},
                {"issuer": ISSUER},
                {"issuer": ISSUER, "resource": RESOURCE, "extra": 1},
                {"issuer": ISSUER + "?x=1", "resource": RESOURCE},
                {"issuer": ISSUER + "/rook", "resource": RESOURCE}):
        with pytest.raises(ValueError):
            check_options(bad)
    check_options({"issuer": "https://hub.example", "resource": "https://hub.example/mcp"})
