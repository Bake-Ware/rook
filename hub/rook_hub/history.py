"""Reading the heartbeat history (H5) for the Workers page's graphs (P4, P7).

The hub keeps the raw copy of every heartbeat (`heartbeats` table). This reads
it, nothing more: no roll-up, no deletion. A request names a span back from the
hub's clock and a step; the database groups the heartbeats into one bucket per
step, counted back from now, so the hub hands out at most span / step rows
however many heartbeats the span holds. Each bucket says when it starts (`at`).

- `series`: one worker's health over the span, bucket by bucket (H4). Each
  field is the bucket's average: a percent for cpu, gpu, npu and battery; the
  share of ram, disk and vram in use, as a percent; degrees for temperature;
  bytes for heap_free; the highest uptime. A field appears only if the worker
  reported it in that bucket, and is listed in `fields` if it did anywhere.
- `counts`: how many heartbeats each worker sent in each bucket, with the time
  of its latest heartbeat (H1). Devices have no heartbeat (D6) and are not here.
"""

import asyncio
import sqlite3

from .errors import HubError
from .journal import iso

BUCKETS = 120     # buckets in a span when the step is not given

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


# Buckets count back from now: bucket 0 is the last `step` seconds, so a span holds span / step of them.
SERIES = (f"SELECT CAST((:now - at) / :step AS INTEGER) AS bucket, count(*) AS count, {_columns()} "
          "FROM heartbeats WHERE worker = :worker AND at > :since AND at <= :now GROUP BY bucket ORDER BY bucket DESC")
COUNTS = ("SELECT worker, CAST((:now - at) / :step AS INTEGER) AS bucket, count(*) AS count FROM heartbeats "
          "WHERE at > :since AND at <= :now GROUP BY worker, bucket ORDER BY worker, bucket DESC")


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
    since = now - span
    rows = await asyncio.to_thread(_read, hub, SERIES, {"worker": row["guid"], "since": since, "step": step, "now": now})
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
    return {"worker": row["moniker"], "now": iso(now), "since": iso(since), "step": step,
            "fields": [f for f in FIELDS if f in fields], "totals": totals, "buckets": buckets}


async def counts(hub, span, step=None):
    """Every worker's heartbeat count per bucket over the last `span` seconds, and its latest heartbeat."""
    span, step = _span(span, step)
    with hub._lock:
        workers = hub._db.execute("SELECT guid, moniker, name, interval, last_heartbeat FROM workers "
                                  "ORDER BY approved_at").fetchall()
    now = hub.clock()
    since = now - span
    rows = await asyncio.to_thread(_read, hub, COUNTS, {"since": since, "step": step, "now": now})
    per = {}
    for r in rows:
        per.setdefault(r["worker"], []).append({"at": iso(now - (r["bucket"] + 1) * step), "count": r["count"]})
    return {"now": iso(now), "since": iso(since), "step": step,
            "workers": [{"moniker": w["moniker"], "name": w["name"], "interval": w["interval"],
                         "last_heartbeat": iso(w["last_heartbeat"]), "counts": per.get(w["guid"], [])}
                        for w in workers]}
