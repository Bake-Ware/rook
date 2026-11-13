"""Jobs (J1 to J8): an event match plus one cap call with fixed arguments.

When an event on the bus (events.py) matches a job that is not paused, the
hub makes the job's call as the job's owner (J7, T5). The run ends by
emitting `job.success` or `job.fail` with the result (J3), which other jobs
can match, so jobs chain. Runs in one chain share a run ID (J5). A chain that
loops back on itself is allowed; it is reported as a warning when a job is
created and when jobs are listed (J4). There is nothing else: no conditions
beyond the match, no retries, no timeouts, no steps (J6).

A match is an event name plus, optionally, values the payload must have:
every key in `payload` must be present in the event's payload with an equal value.
"""

import json
import logging
import re
import time
import uuid

from rook_protocol import ProtocolError, validate

from .errors import HubError, error
from .journal import iso

log = logging.getLogger(__name__)  # rook_hub.jobs: where a run that raised is reported

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    guid        TEXT PRIMARY KEY,
    moniker     TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    owner       TEXT NOT NULL REFERENCES users(guid),
    event       TEXT NOT NULL,        -- the match: event name ...
    payload     TEXT NOT NULL,        -- ... and payload values it must have (JSON object)
    worker      TEXT NOT NULL,        -- the call: worker GUID, or "hub" ...
    cap         TEXT NOT NULL,
    args        TEXT NOT NULL,        -- ... and its fixed arguments (JSON object)
    paused      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_event ON jobs (event);
"""

COMPLETIONS = ("job.success", "job.fail")

# Schedules (J2): "every <N>m" fires at each multiple of N minutes since the Unix
# epoch; "at HH:MM" fires once a day at that time on the hub's local clock.
_EVERY = re.compile(r"every ([1-9][0-9]*)m\Z")
_AT = re.compile(r"at ([01][0-9]|2[0-3]):([0-5][0-9])\Z")


def schedule_due(spec, start, end):
    """True if `spec` has a due time in (start, end]."""
    every = _EVERY.match(spec)
    if every:
        period = int(every.group(1)) * 60
        return end // period > start // period
    hour, minute = (int(x) for x in _AT.match(spec).groups())
    for moment in {time.localtime(start)[:3], time.localtime(end)[:3]}:
        due = time.mktime((*moment, hour, minute, 0, 0, 0, -1))
        if start < due <= end:
            return True
    return False


def _need(args, name, kind=str, default=None):
    value = args.get(name, default)
    if not isinstance(value, kind) or (kind is str and not value):
        raise HubError("bad_message", f"args.{name} is required and must be {'a string' if kind is str else 'an object'}.",
                       "Call again with the arguments in this cap's schema (see the caps tool).")
    return value


def _job(hub, job):
    row = hub._db.execute("SELECT * FROM jobs WHERE guid = ? OR moniker = ?", (job, job)).fetchone()
    if row is None:
        raise HubError("unknown_job", f"No job is called {job!r}.", "List the jobs and use a moniker or GUID from there.")
    return row


def _edges(rows):
    """job GUID -> GUIDs of the jobs its completion triggers."""
    edges = {row["guid"]: [] for row in rows}
    for row in rows:
        if row["event"] not in COMPLETIONS:
            continue
        wanted = json.loads(row["payload"])
        for source in rows:
            if wanted.get("job", source["guid"]) == source["guid"] and \
                    wanted.get("job_moniker", source["moniker"]) == source["moniker"]:
                edges[source["guid"]].append(row["guid"])
    return edges


def _loops(rows):
    """job GUID -> a warning naming the loop it is on (J4)."""
    edges, names, warnings = _edges(rows), {row["guid"]: row["moniker"] for row in rows}, {}
    for start in edges:
        path, stack, seen = None, [(start, [start])], set()
        while stack and path is None:
            node, trail = stack.pop()
            for nxt in edges[node]:
                if nxt == start:
                    path = trail + [start]
                elif nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, trail + [nxt]))
        if path:
            warnings[start] = ("This job is on a loop: " + " -> ".join(names[g] for g in path) +
                               ". Each run triggers the next and nothing ends it; pause or delete a job to stop it.")
    return warnings


def _record(hub, row, warnings):
    record = {
        "guid": row["guid"], "moniker": row["moniker"], "title": row["title"], "description": row["description"],
        "owner": hub.auth._user(row["owner"])["moniker"],
        "match": {"event": row["event"], "payload": json.loads(row["payload"])},
        "call": {"worker": row["worker"] if row["worker"] == "hub" else hub._moniker(row["worker"]),
                 "cap": row["cap"], "args": json.loads(row["args"])},
        "paused": bool(row["paused"]), "created_at": iso(row["created_at"]),
    }
    if row["guid"] in warnings:
        record["warning"] = warnings[row["guid"]]
    return record


def _all(hub):
    return hub._db.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()


# ------------------------------------------------------------- hub caps (J7)

def create(hub, args, actor):
    title, description = _need(args, "title"), args.get("description", "")
    match, call = _need(args, "match", dict), _need(args, "call", dict)
    event, payload = _need(match, "event"), match.get("payload", {})
    worker, cap, call_args = _need(call, "worker"), _need(call, "cap"), call.get("args", {})
    if not isinstance(description, str) or not isinstance(payload, dict) or not isinstance(call_args, dict):
        raise HubError("bad_message", "description must be a string, and match.payload and call.args objects.",
                       "Call again with the arguments in this cap's schema (see the caps tool).")
    if event == "schedule" and not (_EVERY.match(str(payload.get("schedule"))) or _AT.match(str(payload.get("schedule")))):
        raise HubError("bad_message", "A schedule job needs match.payload.schedule like \"every 15m\" or \"at 07:30\".",
                       "Use \"every <N>m\" for every N minutes, or \"at HH:MM\" for a time of day on the hub's clock.")
    try:
        validate({"type": "call", "id": "-", "cap": cap, "args": call_args})
    except ProtocolError as e:
        raise HubError("bad_message", str(e), "Fix the cap name or arguments.") from None
    guid = str(uuid.uuid4())
    with hub._lock, hub._db:
        user, _ = hub.auth.actor(**actor)
        device = hub._db.execute("SELECT guid FROM devices WHERE guid = ? OR moniker = ?", (worker, worker)).fetchone()
        target = worker if worker == "hub" else device["guid"] if device else hub._resolve(worker)["guid"]   # D1
        hub._db.execute(
            "INSERT INTO jobs (guid, moniker, title, description, owner, event, payload, worker, cap, args, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guid, hub._new_moniker(guid), title, description, user, event, json.dumps(payload), target, cap,
             json.dumps(call_args), hub.clock()))
        rows = _all(hub)
        warnings = _loops(rows)
        record = _record(hub, _job(hub, guid), warnings)
    return {"job": record, "warnings": [warnings[guid]] if guid in warnings else []}


def list_jobs(hub, args, actor):
    with hub._lock:
        rows = _all(hub)
        warnings = _loops(rows)
        return {"jobs": [_record(hub, row, warnings) for row in rows]}


def _set_paused(hub, args, actor, paused):
    with hub._lock, hub._db:
        hub.auth.actor(**actor)
        row = _job(hub, _need(args, "job"))
        hub._db.execute("UPDATE jobs SET paused = ? WHERE guid = ?", (int(paused), row["guid"]))
    return {"job": row["moniker"], "paused": paused}


def pause(hub, args, actor):
    return _set_paused(hub, args, actor, True)


def unpause(hub, args, actor):
    return _set_paused(hub, args, actor, False)


def delete(hub, args, actor):
    """A hard delete: the row is gone. Its past runs stay in the journal (J7)."""
    with hub._lock, hub._db:
        hub.auth.actor(**actor)
        row = _job(hub, _need(args, "job"))
        hub._db.execute("DELETE FROM jobs WHERE guid = ?", (row["guid"],))
    return {"deleted": row["moniker"]}


def emit(hub, args, actor):
    """An agent puts an event on the bus (J2)."""
    name, payload = _need(args, "name"), args.get("payload", {})
    if not isinstance(payload, dict):
        raise HubError("bad_message", "args.payload must be an object.", "Send the payload as a JSON object.")
    with hub._lock, hub._db:
        user, token = hub.auth.actor(**actor)
    hub.events.emit(name, payload, user=user, token=token)
    return {"emitted": name}


_JOB_ARG = {"type": "object", "required": ["job"], "additionalProperties": False,
            "properties": {"job": {"type": "string", "description": "The job's moniker or GUID."}}}

CAPS = {
    "job.create": (create, {
        "description": "Create a job: when an event matching `match` happens, the hub calls one cap with fixed "
                       "arguments, as you. Events: worker.online, worker.offline, schedule, webhook, token.used, "
                       "job.success, job.fail, or any name an agent or worker emits.",
        "schema": {"type": "object", "required": ["title", "match", "call"], "additionalProperties": False,
                   "properties": {
                       "title": {"type": "string"},
                       "description": {"type": "string"},
                       "match": {"type": "object", "required": ["event"], "properties": {
                           "event": {"type": "string", "description": "The event name."},
                           "payload": {"type": "object", "description":
                                       "Values the event's payload must have, such as {\"worker_moniker\": "
                                       "\"brave-quiet-otter\"} or, for event schedule, {\"schedule\": "
                                       "\"every 15m\"} or {\"schedule\": \"at 07:30\"}; for job.success and "
                                       "job.fail, {\"job_moniker\": ...} picks the job."}}},
                       "call": {"type": "object", "required": ["worker", "cap"], "properties": {
                           "worker": {"type": "string", "description": "Moniker or GUID of a worker or a device, or \"hub\"."},
                           "cap": {"type": "string"},
                           "args": {"type": "object"}}}}},
    }),
    "job.list": (list_jobs, {
        "description": "List every job with its match, call, owner and whether it is paused, and a warning on "
                       "any job that is part of a loop.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {}},
    }),
    "job.pause": (pause, {"description": "Pause a job: it does not run until un-paused.", "schema": _JOB_ARG}),
    "job.unpause": (unpause, {"description": "Un-pause a paused job.", "schema": _JOB_ARG}),
    "job.delete": (delete, {"description": "Delete a job for good. Its past runs stay in the journal.",
                            "schema": _JOB_ARG}),
    "event.emit": (emit, {
        "description": "Put an event on the hub's bus; jobs that match it run.",
        "schema": {"type": "object", "required": ["name"], "additionalProperties": False, "properties": {
            "name": {"type": "string"}, "payload": {"type": "object"}}},
    }),
}


# --------------------------------------------------------------- running (J7)

def matching(hub, event):
    """The jobs, not paused, whose match this event meets."""
    with hub._lock:
        rows = hub._db.execute("SELECT * FROM jobs WHERE event = ? AND paused = 0", (event["name"],)).fetchall()
    return [row for row in rows
            if all(event["payload"].get(k) == v for k, v in json.loads(row["payload"]).items())]


def schedules(hub):
    """The schedule specs of the jobs that are not paused."""
    with hub._lock:
        rows = hub._db.execute("SELECT payload FROM jobs WHERE event = 'schedule' AND paused = 0").fetchall()
    return sorted({json.loads(row[0]).get("schedule") for row in rows} - {None})


async def run(hub, job, event):
    """One run: the job's call as its owner, then job.success or job.fail (J3).
    A run started by a completion joins that chain's run ID; any other starts one (J5)."""
    run_id = event["payload"].get("run") if event["name"] in COMPLETIONS else None
    if not isinstance(run_id, str) or not run_id:
        run_id = uuid.uuid4().hex
    why = f"Job {job['moniker']} ({job['title']}) on event {event['name']}."
    try:
        reply = await hub.call(job["worker"], job["cap"], json.loads(job["args"]), why,
                               user=job["owner"], job=job["guid"], run=run_id)
    except Exception as e:   # the call raised instead of answering: the run still ends, as a failure (J3)
        log.exception("Job %s run %s raised", job["moniker"], run_id)
        reply = error("run_failed", f"The run stopped inside the hub: {type(e).__name__}: {e}",
                      "Read the hub's log (logger rook_hub.jobs) for the details; the job itself is unchanged.")
        with hub._lock, hub._db:
            hub._journal("job", (job["owner"], None), job["worker"], reply, job["cap"], why, job["guid"], run_id)
    outcome = "job.fail" if reply["type"] == "error" else "job.success"
    hub.events.emit(outcome, {"job": job["guid"], "job_moniker": job["moniker"], "run": run_id, "result": reply},
                    user=job["owner"])
