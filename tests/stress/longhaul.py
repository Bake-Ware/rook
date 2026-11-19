"""Round 7, long haul: a steady mixed load for one hour: 20 calls a second, five terminals typed into
and read every 100 ms, and one stream (a process printing a line every 100 ms) followed to the end.
The hub's and the worker's RSS, file descriptors, threads and CPU every minute, with the calls of
that minute. `python tests/stress/longhaul.py [--minutes N]` (60 unless given)."""

import asyncio
import collections
import re
import sys
import time

import rig
from flood import COMMANDS
from terminals import Terminal

MINUTES = int(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 60
if "--minutes" in sys.argv:
    del sys.argv[sys.argv.index("--minutes"):sys.argv.index("--minutes") + 2]
RATE = 20


async def body(band):
    w = band.monikers[0]
    until = time.monotonic() + MINUTES * 60
    minute = collections.Counter()
    slowest = [0.0]
    pending = []

    async def one(i):
        t = time.monotonic()
        try:
            code, _, _ = await band.door.call(w, "shell.exec", COMMANDS[i % len(COMMANDS)])
        except Exception as e:
            code = f"no_answer:{type(e).__name__}"
        minute[code or "ok"] += 1
        slowest[0] = max(slowest[0], time.monotonic() - t)

    async def calls():
        i, start = 0, time.monotonic()
        while time.monotonic() < until:
            pending.append(asyncio.create_task(one(i)))
            i += 1
            await asyncio.sleep(max(0.0, start + i / RATE - time.monotonic()))

    async def stream():
        code, reply, _ = await band.door.call(w, "shell.open", {
            "pty": False, "title": "long haul stream",
            "cmd": "i=0; while :; do echo $i; i=$((i+1)); sleep 0.1; done"})
        name, position, said, codes = reply["stream"], 0, [], collections.Counter()
        while time.monotonic() < until:
            code, read = await band.door.tool("stream", action="read", stream=name, **{"from": position}, wait=40)
            codes[code or "ok"] += 1
            if not code:
                said.extend(e.get("out", "") for e in read["entries"])
                position = read["next"]
        await band.door.tool("stream", action="close", stream=name, why="Stress test: the long haul is over.")
        numbers = [int(n) for n in re.findall(r"^(\d+)$", "".join(said), re.M)]
        return {"lines": len(numbers), "whole_and_in_order": numbers == list(range(len(numbers))), "reads": dict(codes)}

    terms = [Terminal(band.door, w, i) for i in range(5)]
    for t in terms:
        await t.open()
    band.measure("before", sub="long haul", hub_data_mb=data_mb(band))
    started = time.monotonic()
    jobs = [asyncio.create_task(calls()), asyncio.create_task(stream()),
            *(asyncio.create_task(t.type_and_read(until)) for t in terms)]
    n = 0
    while time.monotonic() < until:
        await asyncio.sleep(max(0.0, started + (n + 1) * 60 - time.monotonic()))
        n += 1
        band.measure("minute", sub="long haul", minute=n, calls=dict(minute), slowest_s=round(slowest[0], 3),
                     hub_data_mb=data_mb(band))
        minute.clear()
        slowest[0] = 0.0
    _, streamed, *_ = await asyncio.gather(*jobs)
    await asyncio.gather(*pending)
    await asyncio.gather(*(t.drain() for t in terms))
    band.log("result", sub="long haul", minutes=MINUTES, calls_last_minute=dict(minute), stream=streamed,
             terminals=[{"typed": t.typed, "echoed": len(set(t.seen())), "codes": dict(t.codes),
                         "slowest_s": round(t.slowest, 3)} for t in terms])
    for t in terms:
        await t.close()
    band.measure("after", sub="long haul", hub_data_mb=data_mb(band))


def data_mb(band):
    return round(sum(f.stat().st_size for f in (band.base / "hub").rglob("*") if f.is_file()) / 1e6, 1)


if __name__ == "__main__":
    rig.main("longhaul", body, n=1)
