"""The heartbeat history (H5, as Bake narrowed it after the stress test) and reading it
for the Workers page's graphs (P4, P7).

A stored heartbeat is its health numbers (`heartbeats` table): which worker, when it
came, and a body that keeps only the heartbeat's `health` block (H4) and what the hub
itself reads from a heartbeat, `config_version` (CF2) and `max_message` (D4), where
the worker sent them (`KEPT`). The caps, facts, band and the rest are kept once, as
the worker's current state (`workers.state`), not again in every row. Bake, after the
stress test: "We'll store whatever the last caps was ... and then store just the
metrics needed for the health charts."

Raw rows live 12 hours. `roll_up` turns every raw heartbeat older than that into
one-minute buckets per worker (`heartbeat_minutes`): for each health field the lowest
and the highest value in that minute, and how many heartbeats the minute held; then
it deletes those raw rows, in the same transaction. Bake: "Let's keep the resolution
around one minute and then take min-max on that one minute sample." "Deleting those
is not a big deal because it's ephemeral." It is the hub cap `heartbeat.roll-up`.
Heartbeats are the only thing the hub deletes, and only this way; minute buckets are
kept. A hub that kept heartbeats in the old shape (every field of every heartbeat, and
an hourly roll-up beside them) is brought to this shape once, as it starts (`convert`).

A request names a span back from the hub's clock and a step; the database groups the
heartbeats into one bucket per step, counted back from now, so the hub hands out at
most span / step rows however many heartbeats the span holds. Each bucket says when
it starts (`at`). Raw rows and minute buckets are read together: a raw heartbeat is
one sample of each field it carried; a minute bucket is `count` samples at the middle
of its lowest and highest value, placed at the middle of the minute, so it falls in
the bucket its heartbeats fell in whenever the buckets start on a minute. So the last
12 hours come from raw rows, as before, and anything older from the minute buckets.

- `series`: one worker's health over the span, bucket by bucket (H4). Each field is
  the average of the bucket's samples: a percent for cpu, gpu, npu and battery; the
  share of ram, disk and vram in use, as a percent; degrees for temperature; bytes
  for heap_free; the highest uptime. A field appears only if the worker reported it
  in that bucket, and is listed in `fields` if it did anywhere.
- `counts`: how many heartbeats each worker sent in each bucket, with the time of its
  latest heartbeat (H1). Devices have no heartbeat (D6) and are not here.
"""

import asyncio
import json
import sqlite3

from .errors import HubError
from .journal import iso

BUCKETS = 120     # buckets in a span when the step is not given
MINUTE = 60       # a roll-up bucket: "keep the resolution around one minute" (Bake)
RAW_KEPT = 12 * 3600   # raw heartbeats older than this are rolled up into minutes (Bake: "12 hours")

KEPT = ("health", "config_version", "max_message")   # what a stored heartbeat keeps of the message

PERCENT = ("cpu", "gpu", "npu", "battery")
SIZES = ("ram", "disk", "vram")
FIELDS = ("cpu", "ram", "disk", "gpu", "vram", "npu", "battery", "temperature", "heap_free", "uptime")


def stored(heartbeat):
    """What the hub stores of one heartbeat message (a dict): its health numbers and KEPT."""
    return {name: heartbeat[name] for name in KEPT if name in heartbeat}


def _span(span, step):
    """(span, step) in seconds, checked; the step defaults to the span over BUCKETS."""
    try:
        span = float(span)
        step = float(step) if step not in (None, "") else max(1.0, span / BUCKETS)
    except (TypeError, ValueError):
        raise HubError("bad_message", "span and step must be numbers of seconds.",
                       "Give span like 21600 for six hours, and step like 180, or leave step out.") from None
    if not span > 0 or not step > 0:
        raise HubError("bad_message", "span and step must be more than 0 seconds.",
                       "Give span like 21600 for six hours, and step like 180, or leave step out.")
    return span, step


def _number(path):
    """SQL for a number in a heartbeat's health block, or NULL when it is not there as a number."""
    return f"CASE WHEN json_type(body, '$.health.{path}') IN ('integer', 'real') THEN json_extract(body, '$.health.{path}') END"


def _value(field):
    """One heartbeat's value of a health field (H4), as SQL; ram, disk and vram as the share in use, a percent."""
    if field in SIZES:
        return f"100.0 * ({_number(field + '.total')} - {_number(field + '.free')}) / nullif({_number(field + '.total')}, 0)"
    return _number(field)


# ------------------------------------------------------------------ the minute roll-up

SCHEMA = ("CREATE TABLE IF NOT EXISTS heartbeat_minutes (\n"
          "    worker  TEXT NOT NULL REFERENCES workers(guid),\n"
          "    minute  INTEGER NOT NULL,      -- the minute's start, in minutes since the epoch\n"
          "    count   INTEGER NOT NULL,      -- heartbeats that arrived in it\n"
          + "".join(f"    {f}_min REAL, {f}_max REAL,\n" for f in FIELDS)
          + "".join(f"    {s}_total REAL,\n" for s in SIZES)
          + "    PRIMARY KEY (worker, minute)\n);\n"
          "CREATE INDEX IF NOT EXISTS heartbeat_minutes_minute ON heartbeat_minutes (minute);\n")

# The raw heartbeats from before :before, added into the minutes they fall in. A minute
# that was partly rolled up before (the 12 hours ended inside it) takes the rest in.
ROLL = ("INSERT INTO heartbeat_minutes (worker, minute, count, "
        + ", ".join(f"{f}_min, {f}_max" for f in FIELDS) + ", " + ", ".join(f"{s}_total" for s in SIZES) + ") "
        f"SELECT worker, CAST(at / {MINUTE} AS INTEGER), count(*), "
        + ", ".join(f"min({v}), max({v})" for v in map(_value, FIELDS)) + ", "
        + ", ".join(f"max({_number(s + '.total')})" for s in SIZES)
        + f" FROM heartbeats WHERE at < :before GROUP BY worker, CAST(at / {MINUTE} AS INTEGER) "
        "ON CONFLICT (worker, minute) DO UPDATE SET count = count + excluded.count, "
        + ", ".join(f"{f}_min = coalesce(min({f}_min, excluded.{f}_min), {f}_min, excluded.{f}_min), "
                    f"{f}_max = coalesce(max({f}_max, excluded.{f}_max), {f}_max, excluded.{f}_max)" for f in FIELDS) + ", "
        + ", ".join(f"{s}_total = coalesce(max({s}_total, excluded.{s}_total), {s}_total, excluded.{s}_total)"
                    for s in SIZES))


def roll_up(hub):
    """Roll every raw heartbeat older than 12 hours into its minute, and delete it: one transaction."""
    with hub._lock, hub._db:
        before = hub.clock() - RAW_KEPT
        hub._db.execute(ROLL, {"before": before})
        rolled = hub._db.execute("DELETE FROM heartbeats WHERE at < ?", (before,)).rowcount
        minutes = hub._db.execute("SELECT count(*) FROM heartbeat_minutes").fetchone()[0]
    return {"rolled_up": rolled, "before": iso(before), "minutes": minutes}


def convert(hub):
    """Bring a hub's heartbeats from the old shape to this one, once, as it starts: roll up what
    is older than 12 hours, keep only KEPT of the raw rows left, and drop the hourly roll-up,
    which the minute buckets now answer for. A hub in the new shape is left as it is."""
    with hub._lock:
        hours = hub._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'heartbeat_hours'").fetchone()
        whole = hub._db.execute("SELECT 1 FROM heartbeats WHERE json_type(body, '$.caps') IS NOT NULL LIMIT 1").fetchone()
    if hours is None and whole is None:
        return
    roll_up(hub)
    with hub._lock, hub._db:
        rows = hub._db.execute("SELECT id, body FROM heartbeats WHERE json_type(body, '$.caps') IS NOT NULL").fetchall()
        hub._db.executemany("UPDATE heartbeats SET body = ? WHERE id = ?",
                            [(json.dumps(stored(json.loads(row["body"]))), row["id"]) for row in rows])
        hub._db.execute("DROP TABLE IF EXISTS heartbeat_hours")


CAPS = {
    "heartbeat.roll-up": (lambda hub, args, actor: roll_up(hub), {
        "description": "Roll every raw heartbeat older than 12 hours into one-minute buckets (each health "
                       "field's lowest and highest, and how many heartbeats) and delete those raw rows.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {}},
    }),
}


# ------------------------------------------------------------------ reading

def _samples(columns_raw, columns_minutes, worker):
    """Raw rows and minute buckets in the span (at > :since and at <= :now), as one set of samples."""
    only = " AND worker = :worker" if worker else ""
    return (f"SELECT worker, at, 1 AS count{columns_raw} FROM heartbeats WHERE at > :since AND at <= :now{only} "
            f"UNION ALL SELECT worker, minute * {MINUTE} + {MINUTE // 2} AS at, count{columns_minutes} "
            f"FROM heartbeat_minutes WHERE minute > (:since - {MINUTE // 2}) / {MINUTE}.0 "
            f"AND minute <= (:now - {MINUTE // 2}) / {MINUTE}.0{only}")


_RAW = "".join(f", {_value(f)} AS {f}_lo, {_value(f)} AS {f}_hi" for f in FIELDS) \
    + "".join(f", {_number(s + '.total')} AS {s}_total" for s in SIZES)
_MINUTES = "".join(f", {f}_min, {f}_max" for f in FIELDS) + "".join(f", {s}_total" for s in SIZES)
_BUCKET = "CAST((:now - at) / :step AS INTEGER)"

# Buckets count back from now: bucket 0 is the last `step` seconds, so a span holds span / step of them.
SERIES = (f"SELECT {_BUCKET} AS bucket, sum(count) AS count, "
          + ", ".join(f"sum(CASE WHEN {f}_lo IS NOT NULL THEN count * ({f}_lo + {f}_hi) / 2.0 END) "
                      f"/ sum(CASE WHEN {f}_lo IS NOT NULL THEN count END) AS {f}" for f in FIELDS if f != "uptime")
          + ", max(uptime_hi) AS uptime, " + ", ".join(f"max({s}_total) AS {s}_total" for s in SIZES)
          + f" FROM ({_samples(_RAW, _MINUTES, True)}) GROUP BY bucket ORDER BY bucket DESC")
COUNTS = (f"SELECT worker, {_BUCKET} AS bucket, sum(count) AS count FROM ({_samples('', '', False)}) "
          "GROUP BY worker, bucket ORDER BY worker, bucket DESC")


def _read(hub, sql, params):
    """Run one read on a read-only connection of its own, in a thread. The hub's database is in WAL
    mode, so this reads beside the hub's writes without its lock, and a long read never holds up
    the event loop that serves workers and agents."""
    db = sqlite3.connect(hub.state_dir.resolve().as_uri() + "/hub.db?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


async def series(hub, worker, span, step=None):
    """One worker's health over the last `span` seconds, in buckets of `step` seconds."""
    span, step = _span(span, step)
    with hub._lock:
        row = hub._resolve(worker)
    now = hub.clock()
    rows = await asyncio.to_thread(_read, hub, SERIES, {"worker": row["guid"], "since": now - span, "step": step,
                                                        "now": now})
    buckets, fields, totals = [], set(), {}
    for r in rows:
        bucket = {"at": iso(now - (r["bucket"] + 1) * step), "count": r["count"]}
        for field in FIELDS:
            if r[field] is not None:
                bucket[field] = round(r[field], 2)
                fields.add(field)
        for size in SIZES:
            if r[f"{size}_total"] is not None:
                totals[size] = r[f"{size}_total"]
        buckets.append(bucket)
    return {"worker": row["moniker"], "now": iso(now), "since": iso(now - span), "step": step,
            "fields": [f for f in FIELDS if f in fields], "totals": totals, "buckets": buckets}


async def counts(hub, span, step=None):
    """Every worker's heartbeat count per bucket over the last `span` seconds, and its latest heartbeat."""
    span, step = _span(span, step)
    with hub._lock:
        workers = hub._db.execute("SELECT guid, moniker, name, interval, last_heartbeat FROM workers "
                                  "ORDER BY approved_at").fetchall()
    now = hub.clock()
    rows = await asyncio.to_thread(_read, hub, COUNTS, {"since": now - span, "step": step, "now": now})
    per = {}
    for r in rows:
        per.setdefault(r["worker"], []).append({"at": iso(now - (r["bucket"] + 1) * step), "count": r["count"]})
    return {"now": iso(now), "since": iso(now - span), "step": step,
            "workers": [{"moniker": w["moniker"], "name": w["name"], "interval": w["interval"],
                         "last_heartbeat": iso(w["last_heartbeat"]), "counts": per.get(w["guid"], [])}
                        for w in workers]}
