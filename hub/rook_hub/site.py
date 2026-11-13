"""The site (P1): Vue 3 with no build step, served by the hub on its one HTTP server.

A person logs in with a name and a password (T4). A good login gives the browser
a session: a hub token (T1) of purpose `session`, owned by the user, kept in a
cookie the page's scripts cannot read, so the person never sees it (T3).
Logging out revokes it. Without a session every page shows the login, and
every data request is refused with what to do.

Pages are installed into segments of the navigation (P2). Core's own Workers
page is installed by `install_page`, the same way a plugin's page will be (P1).
The data requests are only what the pages need, as the logged-in user.
Approving a worker goes through the `worker.approve` hub cap, and a redact
through `journal.redact`, so each is a call in the journal (L1).
"""

import json
import pathlib

from aiohttp import web

from . import bands, caps, devices, history, jobs, journal, plugins, records
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
# the Jobs, Secrets and Auth pages (their data and actions are the last block of this file)
install_page("Hub", "auth", "Auth", "/site/pages/auth.js")
install_page("Hub", "secrets", "Secrets", "/site/pages/secrets.js")
install_page("Hub", "jobs", "Jobs", "/site/pages/jobs.js")
install_page("Hub", "data", "Data", "/site/pages/data.js")


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
    """Everything the Workers page shows (P4, P7, B2, B5, R4, W6), with the devices
    in the band they are on (D1, D4, D6) and each worker's opt-out (D8)."""
    members, owners, out = bands.membership(hub), {}, devices.opted_out(hub)
    open_streams = {}
    for stream in hub.streams.list(open=True):
        open_streams.setdefault(stream["worker"], []).append(stream_view(hub, stream))

    def owner(guid):
        if guid not in owners:
            user = hub.auth.get_user(guid)
            owners[guid] = {"name": user["name"], "moniker": user["moniker"]}
        return owners[guid]

    def cap_list(announced):
        return [{"name": c["name"], "description": c["description"], "returns": c["returns"]} for c in announced]

    def worker(w):
        return {"guid": w["guid"], "moniker": w["moniker"], "name": w["name"], "online": w["online"],
                "owner": owner(w["owner"]), "last_heartbeat": iso(w["last_heartbeat"]),
                "facts": w["facts"] or {}, "health": w["health"] or {}, "caps": cap_list(w["caps"]),
                "streams": open_streams.get(w["moniker"], []), "serves_devices": w["guid"] not in out}

    def device(d):
        return {"guid": d["guid"], "moniker": d["moniker"], "name": d["name"], "device": True,
                "driver": d["driver"], "owner": owner(d["owner"]), "route": d["route"], "interval": d["interval"],
                "reachable": d["reachable"], "status": d["status"], "polled_at": d["polled_at"],
                "caps": cap_list(d["caps"])}

    workers, all_devices = hub.list_workers(), devices.list_devices(hub)
    groups = [{"band": {"guid": b["guid"], "moniker": b["moniker"], "name": b["name"], "gateways": b["gateways"]},
               "workers": [worker(w) for w in workers if (members.get(w["guid"]) or {}).get("guid") == b["guid"]],
               "devices": [device(d) for d in all_devices if (d["band"] or {}).get("guid") == b["guid"]]}
              for b in bands.list_bands(hub)]
    groups.append({"band": None, "workers": [worker(w) for w in workers if members.get(w["guid"]) is None],
                   "devices": [device(d) for d in all_devices if d["band"] is None]})
    pending = [{"key": k["key"], "name": k["name"], "macs": k["macs"], "first_seen": iso(k["first_at"]),
                "caps": [{"name": c["name"], "description": c["description"]} for c in k["caps"]]}
               for k in hub.list_pending()]
    return {"stats": {"online": sum(w["online"] for w in workers), "pending": len(pending),
                      "devices": len(all_devices)},
            "groups": groups, "pending": pending, "unrecognised": bands.unrecognised(hub)}


def stream_view(hub, stream):
    """A stream as a worker card shows it (S3, S5): title, description, who opened it and when,
    and, once closed, who closed it and when."""
    def person(moniker):
        if moniker in (None, "worker"):
            return moniker
        user = hub.auth.get_user(moniker)
        return {"name": user["name"], "moniker": user["moniker"]}
    return {"moniker": stream["moniker"], "title": stream["title"], "description": stream["description"],
            "worker": stream["worker"], "cap": stream["cap"], "open": stream["open"],
            "opened_by": person(stream["opened_by"]), "opened_at": stream["opened_at"],
            "closed_by": person(stream["closed_by"]), "closed_at": stream["closed_at"]}


# ----------------------------------------------------------- the Data page

KINDS = ("call", "auth", "band", "event", "job", "stream")   # what the journal holds (L1)
JOURNAL_FILTERS = ("kind", "worker", "cap", "user", "text", "since", "until", "job", "run")


STREAM_READ_LIMIT = 1000     # entries per terminal read; the terminal reads on until it has them all


def _query_text(request, name):
    value = request.query.get(name, "").strip()
    if not value:
        raise HubError("bad_message", f"{name} is required.", f"Give {name}.")
    return value


def _limit(query, default=50):
    """The `limit` of a query string: how many to show. Any whole number from 1 up."""
    try:
        limit = int(query.get("limit", default))
    except ValueError:
        limit = 0
    if limit < 1:
        raise HubError("bad_message", "limit must be a whole number, 1 or more.", "Give how many to show, like 50.")
    return limit


def journal_choices(hub):
    """What the Journal tab's filters offer: the kinds, users, workers and devices, caps and jobs (L2)."""
    workers, all_devices = hub.list_workers(), devices.list_devices(hub)
    announced = {c["name"] for c in caps.descriptors(hub)}
    announced.update(c["name"] for w in [*workers, *all_devices] for c in w["caps"])
    return {"kinds": list(KINDS),
            "users": [{"name": u["name"], "moniker": u["moniker"]} for u in hub.auth.list_users()],
            "workers": [{"name": "hub", "moniker": "hub", "device": False}]
                       + [{"name": w["name"], "moniker": w["moniker"], "device": False} for w in workers]
                       + [{"name": d["name"], "moniker": d["moniker"], "device": True} for d in all_devices],
            "caps": sorted(announced),
            "jobs": [{"title": j["title"], "moniker": j["moniker"]} for j in jobs.list_jobs(hub, {}, None)["jobs"]]}


def _person(hub, guid, names):
    """{name, moniker} of a user GUID, or None."""
    if guid is None:
        return None
    if guid not in names:
        user = hub.auth.get_user(guid)
        names[guid] = {"name": user["name"], "moniker": user["moniker"]}
    return names[guid]


def record_view(hub, record, names):
    """A record as the Records tab shows it (K1): owner and creator by name, the token an agent used."""
    with hub._lock:
        token = hub._db.execute("SELECT moniker FROM monikers WHERE guid = ?",
                                (record["creator_token"],)).fetchone() if record["creator_token"] else None
    return {"guid": record["guid"], "moniker": record["moniker"], "type": record["type"], "title": record["title"],
            "description": record["description"], "owner": _person(hub, record["owner"], names),
            "creator": _person(hub, record["creator"], names), "creator_token": token[0] if token else None,
            "body": record["body"], "created_at": iso(record["created_at"]), "updated_at": iso(record["updated_at"])}


def record_types(hub):
    """Every record type, the plugin that owns it, and how many records it has (K2)."""
    with hub._lock:
        counts = dict(hub._db.execute("SELECT type, count(*) FROM records GROUP BY type").fetchall())
    return [dict(t, count=counts.get(t["type"], 0)) for t in records.list_types(hub)]


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

    # ------------------------------------------- heartbeat history for the Workers page's graphs (P4, P7, H5)

    @logged_in
    async def heartbeat_counts(request, token, user):
        """Every worker's heartbeats per step over the last `span` seconds: the heartbeats graph (P7)."""
        return await history.counts(hub, request.query.get("span", 60), request.query.get("step"))

    @logged_in
    async def heartbeat_series(request, token, user):
        """One worker's health per step over the last `span` seconds: its health charts (P4, H4)."""
        return await history.series(hub, request.match_info["worker"], request.query.get("span", 86400),
                                    request.query.get("step"))

    app.router.add_get("/api/heartbeats", heartbeat_counts)
    app.router.add_get("/api/heartbeats/{worker}", heartbeat_series)

    # ----------------------------------------------- streams as a terminal on the worker card (P8, S2-S5, L1)

    @logged_in
    async def worker_streams(request, token, user):
        """A worker's streams, newest first; `open=false` for the closed ones (S5)."""
        worker = hub.get_worker(_query_text(request, "worker"))["moniker"]
        wanted = {"true": True, "false": False}.get(request.query.get("open"))
        return {"streams": [stream_view(hub, s) for s in hub.streams.list(wanted) if s["worker"] == worker]}

    @logged_in
    async def one_stream(request, token, user):
        return {"stream": stream_view(hub, hub.streams.get(request.match_info["stream"]))}

    @logged_in
    async def read_stream(request, token, user):
        """The recording's bytes from `from` on, as recorded; waits up to `wait` seconds for more (S2)."""
        try:
            position, wait = int(request.query.get("from", 0)), float(request.query.get("wait", 0))
        except ValueError:
            raise HubError("bad_message", "from must be a whole number and wait a number of seconds.",
                           "Read from the position the last read gave as next.") from None
        return await hub.streams.read(request.match_info["stream"], position, wait, STREAM_READ_LIMIT, raw=True)

    @logged_in
    async def stream_input(request, token, user):
        """What the person typed, to the worker, recorded as theirs (S2)."""
        text = (await _body(request)).get("text")
        if not isinstance(text, str) or not text:
            raise HubError("bad_message", "text is required.", "Send what was typed as text.")
        return await hub.streams.write(request.match_info["stream"], text, token=token)

    @logged_in
    async def close_stream(request, token, user):
        """Close on purpose, with a reason, journaled under the user (S4, L1)."""
        why = _text(await _body(request), "why")
        return await hub.streams.close(request.match_info["stream"], why, token=token)

    app.router.add_get("/api/streams", worker_streams)
    app.router.add_get("/api/streams/{stream}", one_stream)
    app.router.add_get("/api/streams/{stream}/read", read_stream)
    app.router.add_post("/api/streams/{stream}/input", stream_input)
    app.router.add_post("/api/streams/{stream}/close", close_stream)

    # ------------------------------------------- the Data page, and device opt-out (P4, L1, L2, L5, K1-K4, D8)

    @logged_in
    async def read_journal(request, token, user):
        filters = {name: request.query[name] for name in JOURNAL_FILTERS if request.query.get(name)}
        return {"entries": journal.read(hub, **filters, limit=_limit(request.query))}

    @logged_in
    async def choices(request, token, user):
        return journal_choices(hub)

    @logged_in
    async def redact(request, token, user):
        """journal.redact as a call by the user; the value is never kept or echoed (L5)."""
        body = await _body(request)
        if not isinstance(body.get("value"), str) or not body["value"]:
            raise HubError("bad_message", "value is required.", "Give the exact text to take out of the journal.")
        reply = await hub.call("hub", "journal.redact", {"value": body["value"]}, "Redacted on the Data page.",
                               token=token)
        if reply["type"] == "error":
            raise HubError(reply["code"], reply["message"], reply["next"])
        return reply["result"]

    @logged_in
    async def list_records(request, token, user):
        names = {}
        found = records.search(hub, type=request.query.get("type") or None, text=request.query.get("text") or None,
                               limit=_limit(request.query))
        return {"records": [record_view(hub, r, names) for r in found]}

    @logged_in
    async def types(request, token, user):
        return {"types": record_types(hub)}

    @logged_in
    async def unlinked(request, token, user):
        names = {}
        return {"records": [record_view(hub, r, names) for r in records.unlinked_blobs(hub)]}

    @logged_in
    async def one_record(request, token, user):
        names, name = {}, request.match_info["record"]
        linked = records.links(hub, name)
        return {"record": record_view(hub, records.get(hub, name), names),
                "links": {way: [record_view(hub, r, names) for r in linked[way]] for way in ("to", "from")}}

    async def blob_bytes(request):
        """A blob's bytes, as a download: never shown as a page of this site."""
        try:
            session(request)
            name = request.match_info["record"]
            record, data = records.get(hub, name), records.read_blob(hub, name)
        except HubError as e:
            return _refused(e)
        filename = json.dumps(record["title"] or record["moniker"], ensure_ascii=True)
        return web.Response(body=data, headers={
            "Content-Type": record["body"]["mime"], "Content-Disposition": f"attachment; filename={filename}",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

    @logged_in
    async def serves_devices(request, token, user):
        serves = (await _body(request)).get("serves")
        if not isinstance(serves, bool):
            raise HubError("bad_message", "serves must be true or false.", "Send {\"serves\": true} or false.")
        return {"serves_devices": devices.set_serves_devices(hub, request.match_info["worker"], serves, token=token)}

    app.router.add_get("/api/journal", read_journal)
    app.router.add_get("/api/journal/choices", choices)
    app.router.add_post("/api/redact", redact)
    app.router.add_get("/api/records", list_records)
    app.router.add_get("/api/record-types", types)
    app.router.add_get("/api/unlinked-blobs", unlinked)
    app.router.add_get("/api/records/{record}", one_record)
    app.router.add_get("/api/records/{record}/bytes", blob_bytes)
    app.router.add_post("/api/workers/{worker}/serves-devices", serves_devices)

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
    settings_routes(app, hub, logged_in)


# ------------------------------------------------- the Jobs, Secrets and Auth pages

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
             "ok": e["result"].get("type") != "error", "result": e["result"],
             "stream": e["result"].get("stream") if e["result"].get("type") == "stream_open" else None}  # P8
            for e in steps]})
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


async def _hub_cap(hub, cap, args, why, token):
    """Call one of the hub's caps as the logged-in user, journaled as a call (L1); its result, or HubError."""
    reply = await hub.call("hub", cap, args, why, token=token)
    if reply["type"] == "error":
        raise HubError(reply["code"], reply["message"], reply["next"])
    return reply["result"]


def jobs_secrets_auth_routes(app, hub, logged_in):
    """The data and actions of the Jobs, Secrets and Auth pages, as the logged-in user.
    Actions a hub cap already does go through it as a call, so they are journaled as calls (L1)."""
    async def hub_cap(cap, args, why, token):
        return await _hub_cap(hub, cap, args, why, token)

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
        # The person mints it directly (T5: "a user mints directly"), not through the session,
        # so the token hangs on the person and not on this browser's login.
        jwt = hub.auth.mint("agent", user=user)
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


# ------------------------------------------------- the Settings page and its Plugins section

# The Settings page is the last page of the Hub segment (P4). Its sections are installed the
# way pages are installed into segments (P2), so a plugin's settings section can be added
# the same way later (PL1). Core installs one: Plugins.
install_page("Hub", "settings", "Settings", "/site/pages/settings.js")
SETTINGS = []


def install_settings_section(section, label, script):
    """Put a section on the Settings page. The page shows the sections installed, in order."""
    SETTINGS.append({"id": section, "label": label, "script": script})


install_settings_section("plugins", "Plugins", "/site/sections/plugins.js")


def settings_routes(app, hub, logged_in):
    """The Settings page's sections, and its Plugins section's data and install (P4, PL3, PL4)."""
    @logged_in
    async def get_settings(request, token, user):
        return {"sections": SETTINGS}

    @logged_in
    async def get_plugins(request, token, user):
        return {"plugins": hub.plugins.list()}

    @logged_in
    async def check_plugin(request, token, user):
        """What installing from `path` would do, before the person confirms: the folder's plugin,
        and the installed one it would replace at once (PL3). Or the refusal, with what to fix."""
        path = _text(await _body(request), "path")
        manifest = plugins.read_manifest(pathlib.Path(path).expanduser().resolve())
        try:
            installed = hub.plugins.get(manifest["name"])
        except HubError:
            installed = None
        return {"plugin": {"name": manifest["name"], "version": manifest["version"]}, "replaces": installed}

    @logged_in
    async def install_plugin(request, token, user):
        path = _text(await _body(request), "path")
        return await _hub_cap(hub, "plugin.install", {"path": path}, "Installed on the Settings page.", token)

    app.router.add_get("/api/settings", get_settings)
    app.router.add_get("/api/plugins", get_plugins)
    app.router.add_post("/api/plugins/check", check_plugin)
    app.router.add_post("/api/plugins", install_plugin)
