"""Round 8, journal and records at size: fill the journal to 100,000 entries with calls through the
door, and make 50,000 records (through the round's own plugin, tests/stress/plugin, since records are
reached only by plugin code, K2). Then read the journal through the door and through the Data page's
API, and query the records through a plugin cap and the Data page's API. While each read runs, a
probe sends an MCP ping every 50 ms; its slowest answer shows how long the hub stopped answering
anyone else. `python tests/stress/size.py`"""

import asyncio
import json
import time

import aiohttp

import rig

JOURNAL, RECORDS, BATCH = 100_000, 50_000, 2_000


async def newest_id(door):
    _, j = await door.tool("journal", limit=1)
    return j["entries"][0]["id"] if j["entries"] else 0


async def fill_journal(band):
    w, i = band.monikers[0], 0
    started = time.monotonic()
    while (have := await newest_id(band.door)) < JOURNAL:
        n = min(BATCH, JOURNAL - have)

        async def one(k):
            if k % 10 == 0:
                return await band.door.call(w, "worker.ping", {}, why=f"Stress fill {k}")
            return await band.door.call("hub", "secret.list", {}, why=f"Stress fill {k}")
        await asyncio.gather(*(one(i + k) for k in range(n)))
        i += n
    band.log("filled", store="journal", entries=await newest_id(band.door), seconds=round(time.monotonic() - started, 1))


async def fill_records(band):
    started = time.monotonic()
    for start in range(0, RECORDS, 5_000):
        code, reply, _ = await band.door.call("hub", "stress.fill", {"start": start, "n": 5_000})
        assert code is None, reply
    band.log("filled", store="records", records=RECORDS, seconds=round(time.monotonic() - started, 1))


class Probe:
    """An MCP ping every 50 ms; the slowest answer while it ran."""

    def __init__(self, door):
        self.door, self.slowest, self.running = door, 0.0, True

    async def run(self):
        while self.running:
            t = time.monotonic()
            await self.door.rpc("ping")
            self.slowest = max(self.slowest, time.monotonic() - t)
            await asyncio.sleep(0.05)


async def measured(band, label, ask):
    probe = Probe(band.door)
    task = asyncio.create_task(probe.run())
    await asyncio.sleep(0.2)
    started = time.monotonic()
    try:
        what = await ask()
    except Exception as e:
        what = {"failed": f"{type(e).__name__}: {e}"[:300]}
    secs = time.monotonic() - started
    probe.running = False
    await task
    band.log("result", sub=label, seconds=round(secs, 3), others_waited_s=round(probe.slowest, 3), **what)


async def body(band):
    code, reply, _ = await band.door.call("hub", "plugin.install", {"path": str(rig.ROOT / "tests/stress/plugin")})
    assert code is None, reply
    band.measure("before", sub="fill")
    await fill_journal(band)
    await fill_records(band)
    band.measure("after", sub="fill", hub_db_mb=round((band.base / "hub" / "hub.db").stat().st_size / 1e6, 1))

    def door_journal(**args):
        async def ask():
            code, j = await band.door.tool("journal", **args)
            return {"code": code, "said": j[:300]} if code else {"entries": len(j["entries"]),
                                                                 "reply_mb": round(len(json.dumps(j)) / 1e6, 2)}
        return ask

    site = aiohttp.ClientSession(cookies=band.cookies, timeout=aiohttp.ClientTimeout(total=None),
                                 cookie_jar=aiohttp.CookieJar(unsafe=True))

    def site_get(path):
        async def ask():
            async with site.get(band.hub.url + path) as r:
                raw = await r.read()
            out = {"status": r.status, "reply_mb": round(len(raw) / 1e6, 2)}
            body_ = json.loads(raw)
            for key in ("entries", "records", "types"):
                if key in body_:
                    out[key] = len(body_[key])
            return out
        return ask

    def plugin_search(**args):
        async def ask():
            code, reply, _ = await band.door.call("hub", "stress.search", args)
            return {"code": code, "said": str(reply)[:300]} if code else reply
        return ask

    reads = [
        ("door journal, default (50)", door_journal()),
        ("door journal, limit 1,000", door_journal(limit=1000)),
        ("door journal, limit 100,000", door_journal(limit=100_000)),
        ("door journal, all (limit 1,000,000)", door_journal(limit=1_000_000)),
        ("door journal, text of one entry", door_journal(text="Stress fill 77777")),
        ("door journal, text found nowhere", door_journal(text="words found nowhere at all")),
        ("door journal, cap worker.ping, limit 100,000", door_journal(cap="worker.ping", limit=100_000)),
        ("door journal, worker, limit 100,000", door_journal(worker=band.monikers[0], limit=100_000)),
        ("Data page journal, default", site_get("/api/journal")),
        ("Data page journal, limit 100,000", site_get("/api/journal?limit=100000")),
        ("Data page journal, text of one entry", site_get("/api/journal?text=Stress%20fill%2077777")),
        ("Data page journal choices", site_get("/api/journal/choices")),
        ("records cap, one word (13,550 match), default 50", plugin_search(text="amber")),
        ("records cap, one word, limit 50,000", plugin_search(text="amber", limit=50_000)),
        ("records cap, all of the type, limit 50,000", plugin_search(limit=50_000)),
        ("records cap, one record's number", plugin_search(text="number43210")),
        ("Data page records, default", site_get("/api/records")),
        ("Data page records, type stress, limit 50,000", site_get("/api/records?type=stress&limit=50000")),
        ("Data page records, text amber, limit 50,000", site_get("/api/records?text=amber&limit=50000")),
        ("Data page record types", site_get("/api/record-types")),
    ]
    for label, ask in reads:
        band.measure("before", sub=label)
        await measured(band, label, ask)
        band.measure("after", sub=label)
    await site.close()


if __name__ == "__main__":
    rig.main("size", body, n=1)
