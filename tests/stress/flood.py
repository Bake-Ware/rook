"""Round 1, call flood: 200 shell.exec calls at once, then 1,000 at once, then 5,000 spread over a
minute; then, past the brief's numbers, 5,000 and 20,000 at once; mixed short commands, spread over three workers. Counts answers, errors by code, wall time
and the slowest call. `python tests/stress/flood.py`"""

import asyncio
import collections
import time

import rig

COMMANDS = [{"cmd": "echo flood"}, {"cmd": "true"}, {"cmd": "date +%s.%N"}, {"cmd": "seq 1 100"},
            {"argv": ["uname", "-a"]}, {"cmd": "ls /"}, {"cmd": "sleep 0.1; echo slept"},
            {"cmd": "printf '%s\\n' a b c"}, {"cmd": "exit 3"}, {"cmd": "echo to stderr >&2"}]


async def one(door, worker, args, results):
    started = time.monotonic()
    try:
        code, reply, followed = await door.call(worker, "shell.exec", args)
    except Exception as e:      # the request itself failed: no answer from the door
        code, followed = f"no_answer:{type(e).__name__}: {e}"[:120], False
    results.append((code, time.monotonic() - started, followed))


async def flood(band, label, n, spread=0.0):
    """`n` calls, started all at once (spread 0) or evenly over `spread` seconds. Waits for every
    one, however long that takes, and logs what came back."""
    band.measure("before", sub=label)
    results, tasks, started = [], [], time.monotonic()
    for i in range(n):
        if spread:
            await asyncio.sleep(max(0.0, started + i * spread / n - time.monotonic()))
        worker = band.monikers[i % len(band.monikers)]
        tasks.append(asyncio.create_task(one(band.door, worker, COMMANDS[i % len(COMMANDS)], results)))
    await asyncio.gather(*tasks)
    wall = time.monotonic() - started
    codes = collections.Counter(code or "ok" for code, _, _ in results)
    times = sorted(t for _, t, _ in results)
    band.log("result", sub=label, calls=n, answered=sum(1 for c, _, _ in results if not (c or "").startswith("no_answer")),
             by_code=dict(codes), followed=sum(1 for *_, f in results if f), wall_s=round(wall, 2),
             slowest_s=round(times[-1], 3), median_s=round(times[len(times) // 2], 3),
             p99_s=round(times[int(len(times) * 0.99)], 3))
    band.measure("after", sub=label)


async def body(band):
    await flood(band, "200 at once", 200)
    await flood(band, "1,000 at once", 1000)
    await flood(band, "5,000 over a minute", 5000, spread=60.0)
    # Past the brief's numbers, to find where it gives.
    await flood(band, "5,000 at once", 5000)
    await flood(band, "20,000 at once", 20000)


if __name__ == "__main__":
    rig.main("flood", body, n=3)
