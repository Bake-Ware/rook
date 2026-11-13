"""The site (P1, P2, P6, T1, T3, T4) and the Workers page's data (P4, P7, B2, B5, R2, R4, W6),
driven through real HTTP on localhost, as the browser drives it.

The sample hub: two bands (Home, Office), workers on each, a roaming phone, a
laptop on a gateway no band holds, a worker that never connected, a pending
knock, and an open stream. Workers are in-memory links, as in test_bands."""

import asyncio

import aiohttp
import yarl
from nacl.signing import SigningKey

from helpers import Clock, eventually
from memory_link import link_pair
from rook_hub import Hub, bands, journal
from rook_protocol import decode, encode

HOME, OFFICE, CAFE, HOTEL = "02:aa:00:00:00:01", "02:bb:00:00:00:02", "02:cc:00:00:00:03", "02:dd:00:00:00:04"
CAPS = [{"name": "shell.exec", "description": "Run one command and return its output.", "schema": {},
         "returns": "result"},
        {"name": "shell.open", "description": "Open a terminal as a stream.", "schema": {}, "returns": "stream"}]
HEALTH = {"cpu": 12.5, "ram": {"total": 8 << 30, "free": 5 << 30}, "uptime": 3600}
COOKIE = "rook_session"


class Box:
    """An approved worker on an in-memory link. It heartbeats the gateway it is given
    and answers shell.open with a stream."""

    def __init__(self, hub, name, owner):
        self.hub, self.name = hub, name
        self.key = SigningKey.generate().verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": name, "key": self.key, "macs": [], "caps": []})
        record = hub.approve(self.key, user=owner)
        self.guid, self.moniker = record["guid"], record["moniker"]

    async def connect(self, gateway):
        hub_end, self.link = link_pair(self.key)
        self.serving = asyncio.create_task(self.hub.serve_link(hub_end))
        self.answering = asyncio.create_task(self._answer())
        await self.link.send(encode({
            "type": "heartbeat", "protocol": 1, "identity": {"name": self.name, "key": self.key},
            "facts": {"os": "linux", "arch": "x86_64", "hostname": self.name, "form": "python",
                      "version": "0.1.0", "macs": []},
            "caps": CAPS, "band": {"gateway_mac": gateway}, "health": HEALTH}))
        await eventually(lambda: self.hub.get_worker(self.guid)["online"])

    async def _answer(self):
        while True:
            try:
                message = decode(await self.link.recv())
            except EOFError:
                return
            if message["type"] == "call":
                await self.link.send(encode({"type": "stream_open", "id": message["id"], "stream": "t1",
                                             "title": "nightly build", "description": "following the build log"}))

    async def stop(self):
        self.link.close()
        await self.serving
        await self.answering


def browser():
    """A client that keeps cookies as a browser does, also for 127.0.0.1."""
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


async def ask(client, method, url, body=None):
    """(status, JSON reply) of one request."""
    async with client.request(method, url, json=body) as response:
        return response.status, await response.json()


async def login(client, base, name, password):
    async with client.post(f"{base}/api/login", json={"name": name, "password": password}) as response:
        return response.status, await response.json(), response.headers.getall("Set-Cookie", [])


def by_band(view):
    """{band name or 'roaming': [worker names]} from GET /api/workers."""
    return {(g["band"] or {}).get("name", "roaming"): [w["name"] for w in g["workers"]] for g in view["groups"]}


def test_site(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    bob = hub.auth.create_user("Bob", "battery staple")
    boxes = {name: Box(hub, name, owner["guid"]) for name, owner in
             (("desk", alice), ("nas", bob), ("shop", alice), ("phone", alice), ("laptop", alice),
              ("tablet", alice), ("spare", bob))}
    home = bands.create(hub, "Home", HOME, user=alice["guid"])
    office = bands.create(hub, "Office", OFFICE, user=alice["guid"])
    for name, gateway in (("desk", HOME), ("nas", HOME), ("shop", OFFICE), ("phone", None), ("laptop", CAFE),
                          ("tablet", HOTEL)):
        await boxes[name].connect(gateway)        # "spare" never connects: offline, and roaming
    knock_key = SigningKey.generate().verify_key.encode().hex()
    hub.knock({"type": "knock", "protocol": 1, "name": "porch-light", "key": knock_key, "macs": ["24:6f:28:aa:bb:cc"],
               "caps": [{"name": "gpio.write", "description": "Set a pin high or low.", "schema": {},
                         "returns": "result"}]})
    opened = await hub.call(boxes["desk"].guid, "shell.open", {}, "follow the build", user=alice["guid"])
    assert opened["type"] == "stream_open"
    try:
        async with browser() as client:
            # 1. The hub serves the site's files itself (P1): no CDN, no build step.
            for path in ("/", "/workers"):
                async with client.get(base + path) as response:
                    page = await response.text()
                    assert response.status == 200 and "/site/vendor/vue.global.prod.js" in page
                    assert "/site/shell.js" in page and "http" not in page.split("<body>")[1]
            for path in ("shell.js", "pages/workers.js", "style.css", "vendor/vue.global.prod.js",
                         "vendor/rook-scene.js", "assets/rook-logo.png", "assets/favicon.png"):
                async with client.get(f"{base}/site/{path}") as response:
                    assert response.status == 200, path

            # 2. Without a session every data request is refused, saying what to do (T4, C3).
            requests = [("GET", "/api/session", None), ("GET", "/api/workers", None), ("GET", "/api/pins", None),
                        ("PUT", "/api/pins", {"pins": []}), ("POST", "/api/approve", {"key": knock_key}),
                        ("POST", "/api/bands", {"name": "Cafe", "gateway_mac": CAFE}),
                        ("POST", f"/api/bands/{office['moniker']}/gateways", {"gateway_mac": HOTEL}),
                        ("POST", f"/api/bands/{home['moniker']}/name", {"name": "House"})]
            for method, path, body in requests:
                status, reply = await ask(client, method, base + path, body)
                assert status == 401 and reply["code"] == "no_session", (method, path)
                assert reply["next"] == "Log in with your name and password."
            assert hub.list_pending() and len(bands.list_bands(hub)) == 2   # nothing happened

            # An agent's token is not a session, and a session is not an agent's token (T1).
            agent = hub.auth.mint("agent", user=alice["guid"])
            async with browser() as other:
                other.cookie_jar.update_cookies({COOKIE: agent})
                status, reply = await ask(other, "GET", base + "/api/workers")
                assert status == 401 and reply["code"] == "token_scope"

            # 3. A wrong password is refused, sets no cookie, and is journaled (T4, L1).
            status, reply, cookies = await login(client, base, "alice", "wrong")
            assert status == 401 and reply["code"] == "login_failed" and reply["next"] and not cookies
            assert journal.read(hub, kind="auth", user=alice["guid"])[0]["result"] == {"login": "wrong_password"}
            status, reply = await ask(client, "GET", base + "/api/workers")
            assert status == 401

            # 4. A good login sets a session cookie the page's scripts cannot read (T3).
            #    The session is a hub token owned by the user (T1).
            status, reply, cookies = await login(client, base, "alice", "correct horse")
            assert status == 200 and reply == {"user": {"name": "Alice", "moniker": alice["moniker"]}}
            [cookie] = cookies
            assert cookie.startswith(COOKIE + "=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
            assert "token" not in str(reply)
            session = client.cookie_jar.filter_cookies(yarl.URL(base))[COOKIE].value
            token = hub.auth.verify(session)
            assert token["purpose"] == "session" and token["owner"] == alice["guid"]
            assert journal.read(hub, kind="auth", user=alice["guid"])[1]["result"] == {"login": "ok"}

            # A session cookie does not open the MCP door, which takes agent tokens (T4).
            async with client.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   headers={"Authorization": f"Bearer {session}"}) as response:
                assert response.status == 403

            # 5. The shell: who is logged in, and the navigation (P2). Core installs one
            #    segment, Hub, holding the pages that exist.
            status, reply = await ask(client, "GET", base + "/api/session")
            assert status == 200 and reply["user"]["name"] == "Alice"
            assert reply["nav"] == [{"segment": "Hub", "pages": [
                {"id": page, "label": page.title(), "path": f"/{page}", "script": f"/site/pages/{page}.js"}
                for page in ("workers", "auth", "secrets", "jobs", "data")]}]

            # 6. The Workers page's data (P4, P7, B2, B5, R4, W6).
            status, view = await ask(client, "GET", base + "/api/workers")
            assert status == 200
            assert view["stats"] == {"online": 6, "pending": 1, "devices": 0}
            assert by_band(view) == {"Home": ["desk", "nas"], "Office": ["shop"],
                                     "roaming": ["phone", "laptop", "tablet", "spare"]}
            workers = {w["name"]: w for g in view["groups"] for w in g["workers"]}
            assert [g["band"]["gateways"] for g in view["groups"] if g["band"]] == [[HOME], [OFFICE]]
            assert workers["nas"]["owner"] == {"name": "Bob", "moniker": bob["moniker"]}
            assert workers["desk"]["owner"]["name"] == "Alice"
            desk, spare = workers["desk"], workers["spare"]
            assert desk["online"] and desk["moniker"] == boxes["desk"].moniker and desk["last_heartbeat"]
            assert not spare["online"] and spare["last_heartbeat"] is None and spare["caps"] == []
            assert desk["facts"]["hostname"] == "desk" and desk["health"] == HEALTH
            assert desk["caps"] == [{"name": c["name"], "description": c["description"], "returns": c["returns"]}
                                    for c in CAPS]
            assert desk["streams"] == [{"moniker": hub.streams.get(opened["stream"])["moniker"],
                                        "title": "nightly build", "description": "following the build log"}]
            assert workers["nas"]["streams"] == []
            [pending] = view["pending"]
            assert pending["name"] == "porch-light" and pending["key"] == knock_key
            assert pending["caps"] == [{"name": "gpio.write", "description": "Set a pin high or low."}]
            assert pending["macs"] == ["24:6f:28:aa:bb:cc"]
            assert view["unrecognised"] == [
                {"gateway_mac": CAFE, "workers": [{"guid": boxes["laptop"].guid, "moniker": boxes["laptop"].moniker,
                                                   "name": "laptop"}]},
                {"gateway_mac": HOTEL, "workers": [{"guid": boxes["tablet"].guid, "moniker": boxes["tablet"].moniker,
                                                    "name": "tablet"}]}]

            # 7. Approve through the site: a worker.approve call, journaled under the user and
            #    the session (R2, L1). The new worker is the user's (W6).
            status, reply = await ask(client, "POST", base + "/api/approve", {"key": knock_key})
            assert status == 200 and reply["approved"]["name"] == "porch-light" and reply["owner"] == alice["moniker"]
            [call] = journal.read(hub, kind="call", cap="worker.approve")
            assert (call["user"], call["token"], call["why"]) == (alice["moniker"], token["moniker"],
                                                                  "Approved on the Workers page.")
            assert journal.read(hub, kind="auth", text="approved")[0]["token"] == token["moniker"]
            status, view = await ask(client, "GET", base + "/api/workers")
            assert view["pending"] == [] and view["stats"]["pending"] == 0
            assert "porch-light" in by_band(view)["roaming"]
            status, reply = await ask(client, "POST", base + "/api/approve", {"key": knock_key})
            assert status == 400 and reply["code"] == "unknown_key" and reply["next"]

            # 8. Name an unrecognised gateway as a new band, add another to a band, rename a band (B2, P4).
            status, cafe = await ask(client, "POST", base + "/api/bands", {"name": "Cafe", "gateway_mac": CAFE})
            assert status == 200 and cafe["name"] == "Cafe" and cafe["gateways"] == [CAFE]
            status, reply = await ask(client, "POST", f"{base}/api/bands/{office['moniker']}/gateways",
                                      {"gateway_mac": HOTEL})
            assert status == 200 and reply["gateways"] == [OFFICE, HOTEL]
            status, reply = await ask(client, "POST", f"{base}/api/bands/{home['moniker']}/name", {"name": "House"})
            assert status == 200 and reply["name"] == "House"
            status, view = await ask(client, "GET", base + "/api/workers")
            assert by_band(view) == {"House": ["desk", "nas"], "Office": ["shop", "tablet"], "Cafe": ["laptop"],
                                     "roaming": ["phone", "spare", "porch-light"]}
            assert view["unrecognised"] == []
            band_entries = journal.read(hub, kind="band", user=alice["guid"])
            assert [(e["token"], list(e["result"])[0]) for e in band_entries[:3]] == [
                (token["moniker"], "band_renamed"), (token["moniker"], "gateway_added"),
                (token["moniker"], "band_created")]
            status, reply = await ask(client, "POST", base + "/api/bands", {"name": "Again", "gateway_mac": CAFE})
            assert status == 400 and reply["code"] == "gateway_taken"
            status, reply = await ask(client, "POST", base + "/api/bands", {"gateway_mac": CAFE})
            assert status == 400 and reply["code"] == "bad_message" and reply["next"]
            async with client.post(base + "/api/bands", data="not json") as response:
                assert response.status == 400 and (await response.json())["code"] == "bad_message"

            # 9. Pins: saved views, kept at the hub for the user, reorderable (P6).
            assert await ask(client, "GET", base + "/api/pins") == (200, {"pins": []})
            pins = [{"label": "Workers", "href": "/workers"},
                    {"label": "Workers: desk", "href": f"/workers?worker={boxes['desk'].moniker}"},
                    {"label": "Workers: gpu", "href": "/workers?q=gpu"}]
            assert await ask(client, "PUT", base + "/api/pins", {"pins": pins}) == (200, {"pins": pins})
            reordered = [pins[2], pins[0], pins[1]]
            assert await ask(client, "PUT", base + "/api/pins", {"pins": reordered}) == (200, {"pins": reordered})
            kept = [pins[2], pins[1]]
            assert await ask(client, "PUT", base + "/api/pins", {"pins": kept}) == (200, {"pins": kept})
            for bad in ([{"label": "x", "href": "https://elsewhere.example/"}], [{"label": "", "href": "/workers"}],
                        [{"label": "x", "href": "//elsewhere.example/"}], "nope"):
                status, reply = await ask(client, "PUT", base + "/api/pins", {"pins": bad})
                assert status == 400 and reply["code"] == "bad_message"
            assert await ask(client, "GET", base + "/api/pins") == (200, {"pins": kept})

            # Pins belong to the user: another browser logged in as Alice sees them, Bob does not.
            async with browser() as second, browser() as bobs:
                assert (await login(second, base, "Alice", "correct horse"))[0] == 200
                assert await ask(second, "GET", base + "/api/pins") == (200, {"pins": kept})
                assert (await login(bobs, base, "Bob", "battery staple"))[0] == 200
                assert await ask(bobs, "GET", base + "/api/pins") == (200, {"pins": []})

            # 10. Logout ends the session: the token is revoked, the cookie cleared (T4).
            async with client.post(base + "/api/logout") as response:
                assert response.status == 200
            assert COOKIE not in client.cookie_jar.filter_cookies(yarl.URL(base))
            assert [t["revoked_at"] is not None for t in hub.auth.list_tokens() if t["guid"] == token["guid"]] == [True]
            assert journal.read(hub, kind="auth", user=alice["guid"])[0]["result"] == {"revoked_token": token["guid"]}
            async with browser() as stale:
                stale.cookie_jar.update_cookies({COOKIE: session})
                status, reply = await ask(stale, "GET", base + "/api/workers")
                assert status == 401 and reply["code"] == "token_revoked"
            status, reply = await ask(client, "GET", base + "/api/pins")
            assert status == 401 and reply["code"] == "no_session"

            # Pins are still there after a new login.
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            assert await ask(client, "GET", base + "/api/pins") == (200, {"pins": kept})
    finally:
        for box in boxes.values():
            if hasattr(box, "link"):
                await box.stop()
        await hub.stop_serving()
        hub.close()
