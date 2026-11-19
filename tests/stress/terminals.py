"""Round 2, terminal storm: 25 terminals open at once on one worker, each typed into and read every
100 ms for five minutes; then 100 terminals opened and closed in a loop, ten times; then, past the
brief, 1,000 terminals open at once.
`python tests/stress/terminals.py`"""

import asyncio
import collections
import re
import time

import rig

MINUTES = 5
STORM, CYCLES = 100, 10


class Terminal:
    def __init__(self, door, worker, i):
        self.door, self.worker, self.i = door, worker, i
        self.position, self.said, self.typed = 0, "", 0
        self.codes, self.slowest = collections.Counter(), 0.0

    async def open(self):
        code, reply, _ = await self.door.call(self.worker, "shell.open", {
            "argv": ["sh"], "cols": 80, "rows": 24, "title": f"stress terminal {self.i}"})
        self.codes[code or "ok"] += 1
        self.stream = None if code else reply["stream"]
        return self.stream

    async def timed(self, **arguments):
        started = time.monotonic()
        code, reply = await self.door.tool("stream", stream=self.stream, **arguments)
        self.slowest = max(self.slowest, time.monotonic() - started)
        self.codes[code or "ok"] += 1
        return code, reply

    async def read(self, wait=0):
        code, reply = await self.timed(action="read", **{"from": self.position}, wait=wait)
        if not code:
            self.said += "".join(e.get("out", "") for e in reply["entries"])
            self.position = reply["next"]
        return code, reply

    async def type_and_read(self, until):
        """Every 100 ms: type one numbered echo, and read what the terminal said."""
        while time.monotonic() < until:
            tick = time.monotonic()
            await self.timed(action="write", text=f"echo x$(({self.typed}+0))\n")
            self.typed += 1
            await self.read()
            await asyncio.sleep(max(0.0, tick + 0.1 - time.monotonic()))

    def seen(self):
        """The numbers the terminal echoed back, in the order it said them."""
        return [int(n) for n in re.findall(r"^x(\d+)\r?$", self.said, re.M)]

    async def drain(self):
        """Read until the last echo typed has come back (no deadline)."""
        while self.typed and (not self.seen() or self.seen()[-1] != self.typed - 1):
            code, reply = await self.read(wait=5)
            if code or not reply["open"]:
                return

    async def close(self):
        return await self.timed(action="close", why="Stress test: the terminal's work is done.")


async def storm(band):
    worker = band.monikers[0]
    band.measure("before", sub="25 terminals, 5 minutes")
    terms = [Terminal(band.door, worker, i) for i in range(25)]
    opened = time.monotonic()
    await asyncio.gather(*(t.open() for t in terms))
    open_s = time.monotonic() - opened
    terms = [t for t in terms if t.stream]
    until = time.monotonic() + MINUTES * 60
    sampler = asyncio.create_task(sample(band, "25 terminals, 5 minutes"))
    await asyncio.gather(*(t.type_and_read(until) for t in terms))
    await asyncio.gather(*(t.drain() for t in terms))
    sampler.cancel()
    lost = {t.i: t.typed - len(set(t.seen())) for t in terms if t.typed != len(set(t.seen()))}
    disorder = [t.i for t in terms if t.seen() != sorted(t.seen())]
    codes = sum((t.codes for t in terms), collections.Counter())
    await asyncio.gather(*(t.close() for t in terms))
    band.log("result", sub="25 terminals, 5 minutes", opened=len(terms), open_s=round(open_s, 2),
             typed=sum(t.typed for t in terms), echoed=sum(len(set(t.seen())) for t in terms), lost=lost,
             out_of_order=disorder, by_code=dict(codes), slowest_s=round(max(t.slowest for t in terms), 3))
    band.measure("after", sub="25 terminals, 5 minutes")


async def sample(band, sub):
    while True:
        await asyncio.sleep(60)
        band.measure("during", sub=sub)


async def churn(band):
    worker, sub = band.monikers[0], f"{STORM} terminals opened and closed, {CYCLES} times"
    band.measure("before", sub=sub)
    codes, slowest, started = collections.Counter(), 0.0, time.monotonic()
    for cycle in range(CYCLES):
        terms = [Terminal(band.door, worker, i) for i in range(STORM)]
        await asyncio.gather(*(t.open() for t in terms))
        await asyncio.gather(*(t.close() for t in terms if t.stream))
        for t in terms:
            codes.update(t.codes)
            slowest = max(slowest, t.slowest)
        band.measure("cycle", sub=sub, cycle=cycle + 1)
    _, listed = await band.door.tool("stream", action="list", open=True)
    band.log("result", sub=sub, by_code=dict(codes), slowest_s=round(slowest, 3),
             wall_s=round(time.monotonic() - started, 2), still_open=len(listed["streams"]))
    await asyncio.sleep(5)    # let the worker reap the shells it was told to end
    band.measure("after", sub=sub)


async def many(band, n):
    """Past the brief's numbers: `n` terminals opened at once, one echo typed into each, read back, closed."""
    sub = f"{n} terminals at once"
    band.measure("before", sub=sub)
    started = time.monotonic()
    terms = [Terminal(band.door, band.monikers[0], i) for i in range(n)]
    await asyncio.gather(*(t.open() for t in terms))
    opened = [t for t in terms if t.stream]
    band.measure("open", sub=sub, opened=len(opened))

    async def echo(t):
        await t.timed(action="write", text="echo x$((0+0))\n")
        t.typed = 1
        await t.drain()
    await asyncio.gather(*(echo(t) for t in opened))
    await asyncio.gather(*(t.close() for t in opened))
    codes = sum((t.codes for t in terms), collections.Counter())
    band.log("result", sub=sub, opened=len(opened), echoed=sum(1 for t in opened if t.seen() == [0]),
             by_code=dict(codes), slowest_s=round(max(t.slowest for t in terms), 3),
             wall_s=round(time.monotonic() - started, 2))
    await asyncio.sleep(5)
    band.measure("after", sub=sub)


async def body(band):
    await storm(band)
    await churn(band)
    await many(band, 1000)     # past the brief's numbers, to find where it gives


if __name__ == "__main__":
    rig.main("terminals", body, n=1)
