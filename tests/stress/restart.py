"""Round 6, hub restart under load: with a call flood (50 calls a second, mixed short commands, over
three workers) and 25 terminals being typed into every 100 ms, stop the hub cleanly (SIGTERM) and
start it again on the same port; then kill -9 it and start it again. Records what each client saw,
what the journal holds, and what came back by itself. `python tests/stress/restart.py`"""

import asyncio
import collections
import signal
import time

import rig
from flood import COMMANDS

RATE, PHASE = 50, 30      # calls a second; seconds of load before, between and after the restarts


class Calls:
    def __init__(self, band):
        self.band, self.seen, self.tasks, self.running = band, [], [], True

    async def one(self, i):
        started = time.time()
        try:
            code, _, followed = await self.band.door.call(self.band.monikers[i % 3], "shell.exec", COMMANDS[i % len(COMMANDS)])
            code = code or ("ok, followed" if followed else "ok")
        except Exception as e:
            code = f"no_answer:{type(e).__name__}"
        self.seen.append((started, time.time(), code))

    async def generate(self):
        i, start = 0, time.monotonic()
        while self.running:
            self.tasks.append(asyncio.create_task(self.one(i)))
            i += 1
            await asyncio.sleep(max(0.0, start + i / RATE - time.monotonic()))


class Typist:
    """A terminal typed into and read every 100 ms; every reply's code is kept with its time."""

    def __init__(self, band, i):
        self.band, self.i, self.events, self.position, self.running = band, i, [], 0, True

    async def run(self):
        code, reply, _ = await self.band.door.call(self.band.monikers[0], "shell.open",
                                                   {"argv": ["sh"], "title": f"restart terminal {self.i}"})
        self.stream = reply["stream"]
        n = 0
        while self.running:
            for args in ({"action": "write", "text": f"echo r{n}\n"}, {"action": "read", "from": self.position}):
                try:
                    code, reply = await self.band.door.tool("stream", stream=self.stream, **args)
                    if not code and args["action"] == "read":
                        self.position = reply["next"]
                        if not reply["open"]:
                            code = "stream_closed"
                except Exception as e:
                    code = f"no_answer:{type(e).__name__}"
                self.events.append((time.time(), args["action"], code or "ok"))
            n += 1
            await asyncio.sleep(0.1)


def linked_lines(worker):
    return sum("Linked to" in line for line in worker.lines)


def window(seen, start, end):
    """Codes of the calls started in [start, end)."""
    return dict(collections.Counter(code for s, _, code in seen if start <= s < end))


async def restart(band, how, marks):
    signum = signal.SIGTERM if how == "clean" else signal.SIGKILL
    band.measure("before", sub=f"hub stopped {how}")
    linked = [linked_lines(w) for w in band.workers]
    t0 = time.time()
    code = await band.hub.stop(signum)
    stopped = time.time()
    await band.hub.start()
    started = time.time()
    band.door.url = band.hub.url
    await rig.ready(band.door, band.monikers)      # the hub lists them online, with their caps
    listed = time.time()
    while any(linked_lines(w) <= n for w, n in zip(band.workers, linked)):   # each says it linked again
        await asyncio.sleep(0.05)
    back = time.time()
    marks.append({"how": how, "stop_sent": t0, "exited": stopped, "exit_code": code, "listening": started,
                  "workers_back": back})
    band.log("restart", how=how, exit_code=code, stop_s=round(stopped - t0, 2), start_s=round(started - stopped, 2),
             listed_online_s=round(listed - started, 2), workers_linked_s=round(back - started, 2),
             workers_said=[[l for l in w.lines if "lost" in l or "Linked" in l or "Cannot" in l][-3:] for w in band.workers])
    band.measure("after", sub=f"hub stopped {how}")


async def body(band):
    t_begin = time.time()
    calls, typists = Calls(band), [Typist(band, i) for i in range(25)]
    gen = asyncio.create_task(calls.generate())
    typing = [asyncio.create_task(t.run()) for t in typists]
    marks = []
    await asyncio.sleep(PHASE)
    await restart(band, "clean", marks)
    await asyncio.sleep(PHASE)
    await restart(band, "kill -9", marks)
    await asyncio.sleep(PHASE)
    calls.running = False
    for t in typists:
        t.running = False
    await gen
    band.log("waiting", calls_left=sum(1 for t in calls.tasks if not t.done()))
    await asyncio.gather(*calls.tasks)        # every call, however long it takes
    await asyncio.gather(*typing, return_exceptions=True)
    t_end = time.time()

    edges = [t_begin] + [x for m in marks for x in (m["stop_sent"], m["workers_back"])] + [t_end]
    names = ["before", "clean: stopping to workers back", "between", "kill -9: killed to workers back", "after"]
    band.log("result", sub="calls", total=len(calls.seen),
             by_window={names[i]: window(calls.seen, edges[i], edges[i + 1]) for i in range(5)},
             slowest_s=round(max(e - s for s, e, _ in calls.seen), 2))
    term = collections.Counter()
    for t in typists:
        for when, action, code in t.events:
            phase = next(names[i] for i in range(5) if edges[i] <= when < edges[i + 1] or i == 4)
            term[(phase, action, code)] += 1
    band.log("result", sub="terminals", by_phase=[[*k, v] for k, v in sorted(term.items())],
             worked_at_end=sum(1 for t in typists if t.events and t.events[-1][2] == "ok"))
    # The journal, as an agent reads it: everything since the round began.
    code, journal = await band.door.tool("journal", since=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_begin)),
                                         limit=1_000_000)
    entries = journal["entries"] if not code else []
    kinds = collections.Counter((e.get("kind"), (e.get("result") or {}).get("type"), (e.get("result") or {}).get("code"))
                                for e in entries)
    band.log("result", sub="journal", entries=len(entries), by_kind=[[*k, v] for k, v in kinds.most_common()],
             error=code and journal)
    _, streams = await band.door.tool("stream", action="list", open=True)
    band.log("result", sub="streams open at the end", open=len(streams["streams"]))


if __name__ == "__main__":
    rig.main("restart", body, n=3)
