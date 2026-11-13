"""The site (P1): Vue 3 with no build step, served by the hub on its one HTTP server.

A person logs in with a name and a password (T4). A good login gives the browser
a session: a hub token (T1) of purpose `session`, owned by the user, kept in a
cookie the page's scripts cannot read, so the person never sees it (T3).
Logging out revokes it. Without a session every page shows the login, and
every data request is refused with what to do.

Pages are installed into segments of the navigation (P2). Core's own Workers
page is installed by `install_page`, the same way a plugin's page will be (P1).
The data requests are only what the Workers page needs, as the logged-in user.
Approving a worker goes through the `worker.approve` hub cap, so it is a call
in the journal (L1).
"""

import pathlib

from aiohttp import web

from . import bands
from .errors import HubError
from .journal import iso

FILES = pathlib.Path(__file__).parent / "site"
COOKIE = "rook_session"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (           -- saved views in a user's Pinned strip (P6)
    user     TEXT NOT NULL REFERENCES users(guid),
    position INTEGER NOT NULL,
    label    TEXT NOT NULL,
    href     TEXT NOT NULL,                 -- the page's path and its view: filter, tab or selection
    PRIMARY KEY (user, position)
);
"""

# The navigation: segments, each with the pages installed into it (P2).
SEGMENTS = {}


def install_page(segment, page, label, script):
    """Put a page in a segment. The segment appears once something is installed in it."""
    SEGMENTS.setdefault(segment, []).append({"id": page, "label": label, "path": f"/{page}", "script": script})


install_page("Hub", "workers", "Workers", "/site/pages/workers.js")


def _refused(error):
    """An error reply; 401 when the request has no good session."""
    unauthorised = error.message["code"] == "no_session" or error.message["code"].startswith("token_")
    return web.json_response(error.message, status=401 if unauthorised else 400)


async def _body(request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HubError("bad_message", "The request body must be a JSON object.", "Send JSON, as the site does.")
    return body


def _text(body, name):
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HubError("bad_message", f"{name} is required.", f"Give {name}.")
    return value.strip()


def workers_view(hub):
    """Everything the Workers page shows (P4, P7, B2, B5, R4, W6)."""
    members, owners = bands.membership(hub), {}
    open_streams = {}
    for stream in hub.streams.list(open=True):
        open_streams.setdefault(stream["worker"], []).append(
            {"moniker": stream["moniker"], "title": stream["title"], "description": stream["description"]})

    def owner(guid):
        if guid not in owners:
            user = hub.auth.get_user(guid)
            owners[guid] = {"name": user["name"], "moniker": user["moniker"]}
        return owners[guid]

    def worker(w):
        return {"guid": w["guid"], "moniker": w["moniker"], "name": w["name"], "online": w["online"],
                "owner": owner(w["owner"]), "last_heartbeat": iso(w["last_heartbeat"]),
                "facts": w["facts"] or {}, "health": w["health"] or {},
                "caps": [{"name": c["name"], "description": c["description"], "returns": c["returns"]}
                         for c in w["caps"]],
                "streams": open_streams.get(w["moniker"], [])}

    workers = hub.list_workers()
    groups = [{"band": {"guid": b["guid"], "moniker": b["moniker"], "name": b["name"], "gateways": b["gateways"]},
               "workers": [worker(w) for w in workers if (members.get(w["guid"]) or {}).get("guid") == b["guid"]]}
              for b in bands.list_bands(hub)]
    groups.append({"band": None, "workers": [worker(w) for w in workers if members.get(w["guid"]) is None]})
    pending = [{"key": k["key"], "name": k["name"], "macs": k["macs"], "first_seen": iso(k["first_at"]),
                "caps": [{"name": c["name"], "description": c["description"]} for c in k["caps"]]}
               for k in hub.list_pending()]
    return {"stats": {"online": sum(w["online"] for w in workers), "pending": len(pending)},
            "groups": groups, "pending": pending, "unrecognised": bands.unrecognised(hub)}


def add_routes(app, hub):
    def session(request):
        """(session token, user GUID) of the request, recording the use (T6); else HubError."""
        token = request.cookies.get(COOKIE)
        if not token:
            raise HubError("no_session", "You are not logged in.", "Log in with your name and password.")
        with hub._lock, hub._db:
            user, _ = hub.auth.use(token, "session")
        return token, user

    def logged_in(handler):
        async def answer(request):
            try:
                token, user = session(request)
                return web.json_response(await handler(request, token, user))
            except HubError as e:
                return _refused(e)
        return answer

    async def page(request):
        return web.FileResponse(FILES / "index.html")

    async def login(request):
        try:
            body = await _body(request)
            user = hub.auth.login(_text(body, "name"), body.get("password") or "")
            token = hub.auth.mint("session", user=user["guid"])
        except HubError as e:
            return web.json_response(e.message, status=401 if e.message["code"] != "bad_message" else 400)
        reply = web.json_response({"user": {"name": user["name"], "moniker": user["moniker"]}})
        reply.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path="/")
        return reply

    async def logout(request):
        reply = web.json_response({"logged_out": True})
        reply.del_cookie(COOKIE, path="/")
        try:
            token, _ = session(request)
            hub.auth.revoke_token(hub.auth.verify(token)["guid"], token=token)
        except HubError:
            pass   # no session to end
        return reply

    @logged_in
    async def me(request, token, user):
        person = hub.auth.get_user(user)
        return {"user": {"name": person["name"], "moniker": person["moniker"]},
                "nav": [{"segment": name, "pages": pages} for name, pages in SEGMENTS.items()]}

    @logged_in
    async def workers(request, token, user):
        return workers_view(hub)

    @logged_in
    async def approve(request, token, user):
        key = _text(await _body(request), "key")
        reply = await hub.call("hub", "worker.approve", {"key": key}, "Approved on the Workers page.", token=token)
        if reply["type"] == "error":
            raise HubError(reply["code"], reply["message"], reply["next"])
        return reply["result"]

    @logged_in
    async def name_band(request, token, user):
        body = await _body(request)
        return bands.create(hub, _text(body, "name"), _text(body, "gateway_mac"), token=token)

    @logged_in
    async def add_gateway(request, token, user):
        body = await _body(request)
        return bands.add_gateway(hub, request.match_info["band"], _text(body, "gateway_mac"), token=token)

    @logged_in
    async def rename_band(request, token, user):
        return bands.rename(hub, request.match_info["band"], _text(await _body(request), "name"), token=token)

    def pins_of(user):
        with hub._lock:
            return {"pins": [{"label": r["label"], "href": r["href"]} for r in hub._db.execute(
                "SELECT label, href FROM pins WHERE user = ? ORDER BY position", (user,))]}

    @logged_in
    async def get_pins(request, token, user):
        return pins_of(user)

    @logged_in
    async def put_pins(request, token, user):
        """Replace the user's pins with this list, in this order (P6)."""
        pins = (await _body(request)).get("pins")
        if not isinstance(pins, list) or not all(
                isinstance(p, dict) and isinstance(p.get("label"), str) and p["label"]
                and isinstance(p.get("href"), str) and p["href"].startswith("/") and not p["href"].startswith("//")
                for p in pins):
            raise HubError("bad_message", "pins must be a list of {label, href}, each href a path on this hub.",
                           "Send the whole Pinned strip, in order.")
        with hub._lock, hub._db:
            hub._db.execute("DELETE FROM pins WHERE user = ?", (user,))
            hub._db.executemany("INSERT INTO pins (user, position, label, href) VALUES (?, ?, ?, ?)",
                                [(user, i, p["label"], p["href"]) for i, p in enumerate(pins)])
        return pins_of(user)

    app.router.add_get("/", page)
    for pages in SEGMENTS.values():
        for p in pages:
            app.router.add_get(p["path"], page)
    app.router.add_static("/site/", FILES)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/session", me)
    app.router.add_get("/api/workers", workers)
    app.router.add_post("/api/approve", approve)
    app.router.add_post("/api/bands", name_band)
    app.router.add_post("/api/bands/{band}/gateways", add_gateway)
    app.router.add_post("/api/bands/{band}/name", rename_band)
    app.router.add_get("/api/pins", get_pins)
    app.router.add_put("/api/pins", put_pins)

