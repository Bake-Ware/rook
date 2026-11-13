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

from . import bands, caps, devices, jobs, journal
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
                # no-store: a revealed value or a new token is never kept in the browser's cache
                return web.json_response(await handler(request, token, user), headers={"Cache-Control": "no-store"})
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
    jobs_secrets_auth_routes(app, hub, logged_in)



# ------------------------------------------------- the Jobs, Secrets and Auth pages

install_page("Hub", "auth", "Auth", "/site/pages/auth.js")
install_page("Hub", "secrets", "Secrets", "/site/pages/secrets.js")
install_page("Hub", "jobs", "Jobs", "/site/pages/jobs.js")

RUNS_SHOWN = 50   # the newest chain runs the Jobs page shows; older ones stay in the journal


def _names(hub):
    """{moniker: name} for every worker, device and user, to show names beside monikers."""
    names = {w["moniker"]: w["name"] for w in hub.list_workers()}
    names.update({d["moniker"]: d["name"] for d in devices.list_devices(hub)})
    names.update({u["moniker"]: u["name"] for u in hub.auth.list_users()})
    return names


def chains(hub):
    """Every chain of jobs as a tree (J3): a root job, and under each job the jobs its completion
    starts. A job met again on its own path is a loop node and is not followed further (J4)."""
    with hub._lock:
        rows = jobs._all(hub)
        edges = jobs._edges(rows)
    by_guid = {row["guid"]: row for row in rows}
    started = {guid for following in edges.values() for guid in following}
    linked = started | {guid for guid, following in edges.items() if following}

    def node(guid, path):
        row = by_guid[guid]
        loop = guid in path
        return {"job": row["moniker"], "on": row["event"], "loop": loop,
                "children": [] if loop else [node(nxt, path | {guid}) for nxt in edges[guid]]}

    def jobs_in(tree):
        return {tree["job"]} | set().union(*(jobs_in(child) for child in tree["children"]))

    trees, placed = [], set()
    roots = [row["guid"] for row in rows if row["guid"] in linked and row["guid"] not in started]
    loops_only = [row["guid"] for row in rows if row["guid"] in linked]   # a chain that is all loop has no root
    for guid in roots + loops_only:
        if by_guid[guid]["moniker"] not in placed:
            tree = node(guid, frozenset())
            placed |= jobs_in(tree)
            trees.append(tree)
    return trees


def runs(hub, job=None):
    """Job runs from the journal, grouped by run ID (J5), newest run first; each step
    oldest first, with the call's reply (P8). With `job`, the chain runs it took part in."""
    entries = journal.read(hub, kind="job", job=job, limit=RUNS_SHOWN * 20)
    order = list(dict.fromkeys(e["run"] for e in entries))[:RUNS_SHOWN]
    with hub._lock:
        titles = {row["moniker"]: row["title"] for row in jobs._all(hub)}
    names = _names(hub)
    grouped = []
    for run in order:
        steps = entries if job is None else journal.read(hub, kind="job", run=run, limit=1000)
        steps = [e for e in steps if e["run"] == run][::-1]
        grouped.append({"run": run, "at": steps[0]["at"], "steps": [
            {"id": e["id"], "at": e["at"], "job": e.get("job"), "title": titles.get(e.get("job")),
             "worker": e["worker"], "name": names.get(e["worker"], e["worker"]), "cap": e["cap"], "why": e["why"],
             "ok": e["result"].get("type") != "error", "result": e["result"]} for e in steps]})
    return grouped


def jobs_view(hub):
    """What the Jobs page lists (P4): every job, with owner and call target named, and the chains."""
    names = _names(hub)
    listed = jobs.list_jobs(hub, {}, None)["jobs"]
    for job in listed:
        job["owner"] = {"moniker": job["owner"], "name": names.get(job["owner"], job["owner"])}
        job["call"]["name"] = names.get(job["call"]["worker"], job["call"]["worker"])
    return {"jobs": listed, "chains": chains(hub)}


def job_targets(hub):
    """What a new job can call: the hub, every worker and every device, each with its caps' schemas."""
    def caps_of(caps_list):
        return [{"name": c["name"], "description": c.get("description", ""), "schema": c.get("schema") or {}}
                for c in caps_list]
    targets = [{"worker": "hub", "name": "hub", "kind": "hub", "caps": caps_of(caps.descriptors(hub))}]
    targets += [{"worker": w["moniker"], "name": w["name"], "kind": "worker", "caps": caps_of(w["caps"])}
                for w in hub.list_workers()]
    targets += [{"worker": d["moniker"], "name": d["name"], "kind": "device", "caps": caps_of(d["caps"])}
                for d in devices.list_devices(hub)]
    return {"targets": targets}


def secrets_view(hub, token):
    """What the Secrets page lists (X3): scope, name, moniker, who made it and when; never a value.
    Also the scopes a new secret can have."""
    names = _names(hub)
    listed = hub.vault.list({}, {"user": None, "token": token})["secrets"]
    for secret in listed:
        secret["created_by"] = secret["created_by"] and {"moniker": secret["created_by"],
                                                         "name": names.get(secret["created_by"])}
    return {"secrets": listed,
            "scopes": {"workers": [{"moniker": w["moniker"], "name": w["name"]} for w in hub.list_workers()]
                       + [{"moniker": d["moniker"], "name": d["name"]} for d in devices.list_devices(hub)],
                       "bands": [{"moniker": b["moniker"], "name": b["name"]} for b in bands.list_bands(hub)]}}


def auth_view(hub, token, user):
    """What the Auth page lists (T6): users, and tokens with purpose, scope, owner, times and use."""
    users = hub.auth.list_users()
    names = {u["guid"]: u["name"] for u in users}
    tokens = hub.auth.list_tokens()
    monikers = {t["guid"]: t["moniker"] for t in tokens}
    session = hub.auth.verify(token)["guid"]
    return {
        "me": hub.auth.get_user(user)["moniker"],
        "users": [{"moniker": u["moniker"], "name": u["name"], "created_at": iso(u["created_at"]),
                   "revoked_at": iso(u["revoked_at"])} for u in users],
        "tokens": [{"moniker": t["moniker"], "purpose": t["purpose"], "scopes": t["scopes"],
                    "owner": {"moniker": t["owner_moniker"], "name": names[t["owner"]]},
                    "minted_by": monikers.get(t["minted_by"]), "minted_at": iso(t["minted_at"]),
                    "expires_at": iso(t["expires_at"]), "last_used": iso(t["last_used"]),
                    "spent_at": iso(t["spent_at"]), "revoked_at": iso(t["revoked_at"]),
                    "this_session": t["guid"] == session} for t in tokens][::-1]}   # newest first


def jobs_secrets_auth_routes(app, hub, logged_in):
    """The data and actions of the Jobs, Secrets and Auth pages, as the logged-in user.
    Actions a hub cap already does go through it as a call, so they are journaled as calls (L1)."""
    async def hub_cap(cap, args, why, token):
        reply = await hub.call("hub", cap, args, why, token=token)
        if reply["type"] == "error":
            raise HubError(reply["code"], reply["message"], reply["next"])
        return reply["result"]

    # Jobs (P4, J1, J3, J4, J5, J7, P8)
    @logged_in
    async def get_jobs(request, token, user):
        return jobs_view(hub)

    @logged_in
    async def get_runs(request, token, user):
        return {"runs": runs(hub, request.query.get("job") or None)}

    @logged_in
    async def get_targets(request, token, user):
        return job_targets(hub)

    @logged_in
    async def new_job(request, token, user):
        return await hub_cap("job.create", await _body(request), "Created on the Jobs page.", token)

    def job_action(cap, why):
        @logged_in
        async def act(request, token, user):
            return await hub_cap(cap, {"job": request.match_info["job"]}, why, token)
        return act

    # Secrets (X3)
    @logged_in
    async def get_secrets(request, token, user):
        return secrets_view(hub, token)

    @logged_in
    async def new_secret(request, token, user):
        body = await _body(request)
        args = {name: body.get(name) for name in ("scope", "name", "value")}
        return await hub_cap("secret.create", args, "Added on the Secrets page.", token)

    @logged_in
    async def reveal(request, token, user):
        return {"value": hub.vault.reveal(request.match_info["secret"], token=token)}

    # Auth (T3, T5, T6)
    @logged_in
    async def get_auth(request, token, user):
        return auth_view(hub, token, user)

    @logged_in
    async def new_token(request, token, user):
        jwt = hub.auth.mint("agent", token=token)
        return {"token": jwt, "moniker": hub.auth.verify(jwt)["moniker"]}

    @logged_in
    async def revoke_token(request, token, user):
        hub.auth.revoke_token(request.match_info["token"], token=token)
        return {"revoked": request.match_info["token"]}

    @logged_in
    async def revoke_user(request, token, user):
        hub.auth.revoke_user(request.match_info["user"], token=token)
        return {"revoked": request.match_info["user"]}

    @logged_in
    async def change_password(request, token, user):
        password = (await _body(request)).get("password")
        if not isinstance(password, str) or not password:
            raise HubError("bad_message", "password is required.", "Give the new password.")
        hub.auth.change_password(user, password, token=token)
        return {"changed": True}

    app.router.add_get("/api/jobs", get_jobs)
    app.router.add_get("/api/jobs/runs", get_runs)
    app.router.add_get("/api/jobs/targets", get_targets)
    app.router.add_post("/api/jobs", new_job)
    app.router.add_post("/api/jobs/{job}/pause", job_action("job.pause", "Paused on the Jobs page."))
    app.router.add_post("/api/jobs/{job}/unpause", job_action("job.unpause", "Un-paused on the Jobs page."))
    app.router.add_post("/api/jobs/{job}/delete", job_action("job.delete", "Deleted on the Jobs page."))
    app.router.add_get("/api/secrets", get_secrets)
    app.router.add_post("/api/secrets", new_secret)
    app.router.add_post("/api/secrets/{secret}/reveal", reveal)
    app.router.add_get("/api/auth", get_auth)
    app.router.add_post("/api/tokens", new_token)
    app.router.add_post("/api/tokens/{token}/revoke", revoke_token)
    app.router.add_post("/api/users/{user}/revoke", revoke_user)
    app.router.add_post("/api/password", change_password)
