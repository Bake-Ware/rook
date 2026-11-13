"""The heartbeat history (H5) and reading it for the Workers page's graphs (P4, P7).

The hub keeps the raw copy of every heartbeat (`heartbeats` table) and an hourly
roll-up of it (`heartbeat_hours`): per worker and hour, how many heartbeats came,
when the latest did, and for each health field (H4) how many heartbeats carried
it, their sum, the lowest and the highest. One SQL statement (`ROLL`) makes the
roll-up from raw rows: each heartbeat adds itself as it is stored, in the same
transaction, so a restart in the middle of an hour loses nothing; and a hub that
kept heartbeats before the roll-up existed rolls them up once as it starts. Nothing
is deleted (Bake 13 Nov: "build the hourly roll-up, delete nothing").

A request names a span back from the hub's clock and a step; the database groups
the heartbeats into one bucket per step, counted back from now, so the hub hands
out at most span / step rows however many heartbeats the span holds. Each bucket
says when it starts (`at`). A span longer than a day, or a step of an hour or more,
is answered from the roll-up, in whole hours: the step is rounded up to whole
hours and the buckets count back from the end of the current hour. An average
over several hours is the sum over the hours divided by the count, so it is the
average of the heartbeats, not of the hourly averages. `source` says which answered.

- `series`: one worker's health over the span, bucket by bucket (H4). Each
  field is the bucket's average: a percent for cpu, gpu, npu and battery; the
  share of ram, disk and vram in use, as a percent; degrees for temperature;
  bytes for heap_free; the highest uptime. A field appears only if the worker
  reported it in that bucket, and is listed in `fields` if it did anywhere.
- `counts`: how many heartbeats each worker sent in each bucket, with the time
  of its latest heartbeat (H1). Devices have no heartbeat (D6) and are not here.
"""

import asyncio
import math
import sqlite3

from .errors import HubError
from .journal import iso

BUCKETS = 120     # buckets in a span when the step is not given
HOUR = 3600
RAW_SPAN = 86400  # spans up to a day, with a step under an hour, are read from the raw heartbeats

PERCENT = ("cpu", "gpu", "npu", "battery")
SIZES = ("ram", "disk", "vram")
FIELDS = ("cpu", "ram", "disk", "gpu", "vram", "npu", "battery", "temperature", "heap_free", "uptime")


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


def _columns():
    """The SELECT list that averages each health field over a bucket (H4)."""
    h = "json_extract(body, '$.health.{}')"
    out = [f"avg({h.format(f)}) AS {f}" for f in PERCENT + ("temperature", "heap_free")]
    out += [f"avg(100.0 * ({h.format(s + '.total')} - {h.format(s + '.free')}) / nullif({h.format(s + '.total')}, 0)) AS {s}"
            for s in SIZES]
    out += [f"max({h.format(s + '.total')}) AS {s}_total" for s in SIZES]
    out.append(f"max({h.format('uptime')}) AS uptime")
    return ", ".join(out)


# ------------------------------------------------------------------ the hourly roll-up (H5)

def _number(path):
    """SQL for a number in a heartbeat's health block, or NULL when it is not there as a number."""
    return f"CASE WHEN json_type(body, '$.health.{path}') IN ('integer', 'real') THEN json_extract(body, '$.health.{path}') END"


def _value(field):
    """One heartbeat's value of a health field (H4), as SQL; ram, disk and vram as the share in use, a percent."""
    if field in SIZES:
        return f"100.0 * ({_number(field + '.total')} - {_number(field + '.free')}) / nullif({_number(field + '.total')}, 0)"
    return _number(field)


SCHEMA = ("CREATE TABLE IF NOT EXISTS heartbeat_hours (\n"
          "    worker  TEXT NOT NULL REFERENCES workers(guid),\n"
          "    hour    INTEGER NOT NULL,      -- the hour's start, in hours since the epoch\n"
          "    count   INTEGER NOT NULL,      -- heartbeats that arrived in it\n"
          "    last_at REAL NOT NULL,         -- when the latest of them did\n"
          + "".join(f"    {f}_n INTEGER NOT NULL, {f}_sum REAL NOT NULL, {f}_min REAL, {f}_max REAL,\n" for f in FIELDS)
          + "".join(f"    {s}_total REAL,\n" for s in SIZES)
          + "    PRIMARY KEY (worker, hour)\n);\n")

# The roll-up of the raw heartbeats that match `{where}`, added into the hours they fall in.
ROLL = ("INSERT INTO heartbeat_hours (worker, hour, count, last_at, "
        + ", ".join(f"{f}_n, {f}_sum, {f}_min, {f}_max" for f in FIELDS) + ", "
        + ", ".join(f"{s}_total" for s in SIZES) + ") "
        f"SELECT worker, CAST(at / {HOUR} AS INTEGER), count(*), max(at), "
        + ", ".join(f"count({v}), total({v}), min({v}), max({v})" for v in map(_value, FIELDS)) + ", "
        + ", ".join(f"max({_number(s + '.total')})" for s in SIZES)
        + f" FROM heartbeats WHERE {{where}} GROUP BY worker, CAST(at / {HOUR} AS INTEGER) "
        "ON CONFLICT (worker, hour) DO UPDATE SET count = count + excluded.count, "
        "last_at = max(last_at, excluded.last_at), "
        + ", ".join(f"{f}_n = {f}_n + excluded.{f}_n, {f}_sum = {f}_sum + excluded.{f}_sum, "
                    f"{f}_min = coalesce(min({f}_min, excluded.{f}_min), {f}_min, excluded.{f}_min), "
                    f"{f}_max = coalesce(max({f}_max, excluded.{f}_max), {f}_max, excluded.{f}_max)" for f in FIELDS) + ", "
        + ", ".join(f"{s}_total = coalesce(max({s}_total, excluded.{s}_total), {s}_total, excluded.{s}_total)"
                    for s in SIZES))


def roll(hub, heartbeat):
    """Add one stored heartbeat (its row id) to its hour. The caller holds the lock and the transaction."""
    hub._db.execute(ROLL.format(where="id = ?"), (heartbeat,))


def roll_up_existing(hub):
    """Roll up the heartbeats a hub kept before it had the roll-up, once: when there are heartbeats
    and no hour yet. Called as the hub starts."""
    with hub._lock, hub._db:
        if hub._db.execute("SELECT 1 FROM heartbeat_hours LIMIT 1").fetchone() is None \
                and hub._db.execute("SELECT 1 FROM heartbeats LIMIT 1").fetchone() is not None:
            hub._db.execute(ROLL.format(where="true"))


# ------------------------------------------------------------------ reading

# Buckets count back from now: bucket 0 is the last `step` seconds, so a span holds span / step of them.
SERIES = (f"SELECT CAST((:now - at) / :step AS INTEGER) AS bucket, count(*) AS count, {_columns()} "
          "FROM heartbeats WHERE worker = :worker AND at > :since AND at <= :now GROUP BY bucket ORDER BY bucket DESC")
COUNTS = ("SELECT worker, CAST((:now - at) / :step AS INTEGER) AS bucket, count(*) AS count FROM heartbeats "
          "WHERE at > :since AND at <= :now GROUP BY worker, bucket ORDER BY worker, bucket DESC")

# The same questions from the roll-up, in whole hours: :now is the end of an hour, :step whole hours.
_HOUR_BUCKET = f"CAST((:now - (hour + 1) * {HOUR}) / :step AS INTEGER)"
_IN_SPAN = f"hour >= CAST(:since / {HOUR} AS INTEGER) AND hour < CAST(:now / {HOUR} AS INTEGER)"
SERIES_HOURLY = (f"SELECT {_HOUR_BUCKET} AS bucket, sum(count) AS count, "
                 + ", ".join(f"sum({f}_sum) / nullif(sum({f}_n), 0) AS {f}" for f in PERCENT + ("temperature", "heap_free") + SIZES)
                 + ", " + ", ".join(f"max({s}_total) AS {s}_total" for s in SIZES)
                 + f", max(uptime_max) AS uptime FROM heartbeat_hours WHERE worker = :worker AND {_IN_SPAN} "
                 "GROUP BY bucket ORDER BY bucket DESC")
COUNTS_HOURLY = (f"SELECT worker, {_HOUR_BUCKET} AS bucket, sum(count) AS count FROM heartbeat_hours "
                 f"WHERE {_IN_SPAN} GROUP BY worker, bucket ORDER BY worker, bucket DESC")


def _window(hub, span, step):
    """(source, now, end, since, step, series SQL, counts SQL) for a span and step back from the hub's
    clock `now`: the raw heartbeats for up to a day in steps under an hour, with buckets counted back
    from now; else the roll-up, in whole hours, with buckets counted back from the end of this hour."""
    now = hub.clock()
    if span <= RAW_SPAN and step < HOUR:
        return "raw", now, now, now - span, step, SERIES, COUNTS
    step = math.ceil(step / HOUR) * HOUR
    end = math.ceil(now / HOUR) * HOUR
    return "hourly", now, end, end - math.ceil(span / step) * step, step, SERIES_HOURLY, COUNTS_HOURLY


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
    source, now, end, since, step, sql, _ = _window(hub, span, step)
    rows = await asyncio.to_thread(_read, hub, sql, {"worker": row["guid"], "since": since, "step": step, "now": end})
    buckets, fields, totals = [], set(), {}
    for r in rows:
        bucket = {"at": iso(end - (r["bucket"] + 1) * step), "count": r["count"]}
        for field in FIELDS:
            if r[field] is not None:
                bucket[field] = round(r[field], 2)
                fields.add(field)
        for size in SIZES:
            if r[f"{size}_total"] is not None:
                totals[size] = r[f"{size}_total"]
        buckets.append(bucket)
    return {"worker": row["moniker"], "now": iso(now), "since": iso(since), "step": step, "source": source,
            "fields": [f for f in FIELDS if f in fields], "totals": totals, "buckets": buckets}


async def counts(hub, span, step=None):
    """Every worker's heartbeat count per bucket over the last `span` seconds, and its latest heartbeat."""
    span, step = _span(span, step)
    with hub._lock:
        workers = hub._db.execute("SELECT guid, moniker, name, interval, last_heartbeat FROM workers "
                                  "ORDER BY approved_at").fetchall()
    source, now, end, since, step, _, sql = _window(hub, span, step)
    rows = await asyncio.to_thread(_read, hub, sql, {"since": since, "step": step, "now": end})
    per = {}
    for r in rows:
        per.setdefault(r["worker"], []).append({"at": iso(end - (r["bucket"] + 1) * step), "count": r["count"]})
    return {"now": iso(now), "since": iso(since), "step": step, "source": source,
            "workers": [{"moniker": w["moniker"], "name": w["name"], "interval": w["interval"],
                         "last_heartbeat": iso(w["last_heartbeat"]), "counts": per.get(w["guid"], [])}
                        for w in workers]}
