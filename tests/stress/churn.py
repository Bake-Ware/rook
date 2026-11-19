"""Round 5, churn: a worker that links and drops 500 times (its process killed with SIGKILL once
linked, then started again); three workers heartbeating every 5 ms for ten minutes; 50 fresh worker
keys knocking at once, approved through the door in a loop. `python tests/stress/churn.py`

The hub has no way yet to set a worker's heartbeat interval (H2 is not built), so the three fast
workers are Python workers run in this process, woken to heartbeat every 5 ms through the same event
a change of the worker's state uses (`_changed`); everything they send is a normal heartbeat."""

import asyncio
import collections
import signal
import sys
import time

import rig

sys.path[:0] = [str(rig.ROOT / "protocol/python"), str(rig.ROOT / "core/python")]
from rook_worker import Worker  # noqa: E402

DROPS, FAST_MINUTES, KNOCKERS = 500, 10, 50


def data_mb(band):
    return round(sum(f.stat().st_size for f in (band.base / "hub").rglob("*") if f.is_file()) / 1e6, 1)


async def drops(band):
    sub = f"a worker linked and dropped {DROPS} times"
    w = band.workers[0]
    band.measure("before", sub=sub)
    started, slowest = time.monotonic(), 0.0
    for i in range(DROPS):
        await w.stop(signal.SIGKILL)
        t = time.monotonic()
        await w.start()
        await w.line("Linked to")
        slowest = max(slowest, time.monotonic() - t)
        if (i + 1) % 100 == 0:
            band.measure("during", sub=sub, drops=i + 1)
    await rig.ready(band.door, band.monikers[:1])
    code, reply, _ = await band.door.call(band.monikers[0], "shell.exec", {"cmd": "echo after the drops"})
    _, listed = await band.door.tool("workers")
    band.log("result", sub=sub, wall_s=round(time.monotonic() - started, 1), slowest_relink_s=round(slowest, 2),
             call_after=code or reply["stdout"].strip(), workers_listed=len(listed["workers"]), hub_data_mb=data_mb(band))
    band.measure("after", sub=sub)


class Counted(Worker):
    def heartbeat(self):
        self.sent = getattr(self, "sent", 0) + 1
        return super().heartbeat()


async def start_inprocess(band, names, **kw):
    """Python workers in this process, each with its own state; approved through the door."""
    workers = [Counted(band.base / name, band.hub.url, name=name, macs=[], **kw) for name in names]
    tasks = [asyncio.create_task(w.run()) for w in workers]
    return workers, tasks


async def approve_all(band, keys):
    """Approve every knocking key in `keys` through the door, in a loop, until all are approved."""
    left, codes = set(keys), collections.Counter()
    while left:
        _, listed = await band.door.tool("workers")
        for k in listed["knocking"]:
            if k["key"] in left:
                code, _, _ = await band.door.call("hub", "worker.approve", {"key": k["key"]})
                codes[code or "ok"] += 1
                left.discard(k["key"])
        await asyncio.sleep(0.05)
    return codes


async def online(band, names):
    """Wait until every named worker is listed online; returns their monikers."""
    while True:
        _, listed = await band.door.tool("workers")
        found = {w["name"]: w["moniker"] for w in listed["workers"] if w["online"] and w["name"] in names}
        if len(found) == len(names):
            return [found[n] for n in names]
        await asyncio.sleep(0.1)


async def fast_heartbeats(band):
    sub = f"three workers heartbeating every 5 ms, {FAST_MINUTES} minutes"
    names = [f"fast-{i}" for i in range(3)]
    workers, tasks = await start_inprocess(band, names, knock_every=0.1)
    await approve_all(band, [w.key for w in workers])
    monikers = await online(band, names)
    await rig.ready(band.door, monikers)
    band.measure("before", sub=sub, hub_data_mb=data_mb(band))

    async def pace(w):
        while True:
            w._changed.set()
            await asyncio.sleep(0.005)
    pacers = [asyncio.create_task(pace(w)) for w in workers]
    sent0 = [w.sent for w in workers]
    started, calls, rounds = time.monotonic(), [], 0
    while time.monotonic() - started < FAST_MINUTES * 60:
        rounds += 1
        for m in monikers:          # a call to each, every 10 s: do they still answer?
            t = time.monotonic()
            code, _, _ = await band.door.call(m, "worker.ping", {})
            calls.append((code or "ok", time.monotonic() - t))
        await asyncio.sleep(10)
        if rounds % 6 == 0:     # about once a minute
            band.measure("during", sub=sub, hub_data_mb=data_mb(band),
                         heartbeats=[w.sent - s for w, s in zip(workers, sent0)])
    secs = time.monotonic() - started
    for p in pacers:
        p.cancel()
    sent = [w.sent - s for w, s in zip(workers, sent0)]
    _, listed = await band.door.tool("workers")
    band.log("result", sub=sub, seconds=round(secs), heartbeats_sent=sent,
             per_second=[round(s / secs) for s in sent], calls=dict(collections.Counter(c for c, _ in calls)),
             slowest_call_s=round(max(t for _, t in calls), 3),
             online=[w["online"] for w in listed["workers"] if w["name"] in names], hub_data_mb=data_mb(band))
    band.measure("after", sub=sub)
    for w in workers:
        w.stop()
    await asyncio.gather(*tasks, return_exceptions=True)


async def knockers(band):
    sub = f"{KNOCKERS} fresh keys knocking at once"
    names = [f"knock-{i}" for i in range(KNOCKERS)]
    band.measure("before", sub=sub)
    started = time.monotonic()
    workers, tasks = await start_inprocess(band, names, knock_every=1.0)
    codes = await approve_all(band, [w.key for w in workers])
    approved = time.monotonic() - started
    await online(band, names)
    linked = time.monotonic() - started
    band.log("result", sub=sub, approvals=dict(codes), all_approved_s=round(approved, 2), all_online_s=round(linked, 2))
    band.measure("after", sub=sub)
    for w in workers:
        w.stop()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    band.log("stopped", sub=sub, errors=[repr(r) for r in results if isinstance(r, BaseException)][:5])


async def body(band):
    await drops(band)
    await fast_heartbeats(band)
    await knockers(band)


if __name__ == "__main__":
    rig.main("churn", body, n=1)
