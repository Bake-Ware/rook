"""Round 3, stream volume: one stream carrying 100,000 lines; one carrying a 50 MB single write; ten
streams at once each carrying 10,000 lines; then, past the brief, 100 such streams at once. Each is a process stream (shell.open, no terminal) read
through the door from the start to its end, at the door's default page size, as an agent reads it.
Checks order and drops. `python tests/stress/streams.py`"""

import asyncio
import collections
import time

import rig


async def follow(door, worker, args, title):
    """Open a stream and read it to its end. Returns (output, reads, entries, codes, slowest read, seconds)."""
    started = time.monotonic()
    code, reply, _ = await door.call(worker, "shell.open", {**args, "pty": False, "title": title})
    if code:
        return None, 0, 0, collections.Counter({code: 1}), 0.0, 0.0
    stream, position, parts, reads, entries, codes, slowest = reply["stream"], 0, [], 0, 0, collections.Counter(), 0.0
    while True:
        t = time.monotonic()
        code, read = await door.tool("stream", action="read", stream=stream, **{"from": position}, wait=40)
        slowest = max(slowest, time.monotonic() - t)
        reads += 1
        codes[code or "ok"] += 1
        if code:
            break
        entries += len(read["entries"])
        parts.extend(e.get("out", "") for e in read["entries"])
        position = read["next"]
        if not read["open"] and not read["entries"]:
            break
    return "".join(parts), reads, entries, codes, slowest, time.monotonic() - started


def lines_check(out, prefix, n):
    got = out.splitlines()
    want = [f"{prefix}{i}" for i in range(1, n + 1)]
    return {"lines": len(got), "in_order_and_whole": got == want,
            "missing": len(set(want) - set(got)), "extra": len(set(got) - set(want))}


async def body(band):
    w = band.monikers[0]

    band.measure("before", sub="100,000 lines")
    out, reads, entries, codes, slowest, secs = await follow(band.door, w, {"argv": ["seq", "1", "100000"]}, "stress 100k lines")
    band.log("result", sub="100,000 lines", reads=reads, entries=entries, by_code=dict(codes),
             slowest_read_s=round(slowest, 3), wall_s=round(secs, 2), **lines_check(out or "", "", 100000))
    band.measure("after", sub="100,000 lines")

    band.measure("before", sub="50 MB single write")
    write = "import sys; sys.stdout.buffer.write(b'a' * 50_000_000); sys.stdout.flush()"
    out, reads, entries, codes, slowest, secs = await follow(band.door, w, {"argv": ["python3", "-c", write]}, "stress 50 MB")
    band.log("result", sub="50 MB single write", reads=reads, entries=entries, by_code=dict(codes),
             slowest_read_s=round(slowest, 3), wall_s=round(secs, 2), bytes=len(out or ""),
             whole=(out == "a" * 50_000_000))
    band.measure("after", sub="50 MB single write")

    band.measure("before", sub="10 streams x 10,000 lines")
    started = time.monotonic()
    got = await asyncio.gather(*(follow(band.door, band.monikers[i % len(band.monikers)],
                                        {"argv": ["seq", "-f", f"s{i}-%g", "1", "10000"]}, f"stress ten {i}")
                                 for i in range(10)))
    band.log("result", sub="10 streams x 10,000 lines", wall_s=round(time.monotonic() - started, 2),
             streams=[{"reads": g[1], "by_code": dict(g[3]), "slowest_read_s": round(g[4], 3),
                       **lines_check(g[0] or "", f"s{i}-", 10000)} for i, g in enumerate(got)])
    band.measure("after", sub="10 streams x 10,000 lines")

    # Past the brief's numbers, to find where it gives.
    await many_streams(band, 100)


async def many_streams(band, n):
    sub = f"{n} streams x 10,000 lines"
    band.measure("before", sub=sub)
    started = time.monotonic()
    got = await asyncio.gather(*(follow(band.door, band.monikers[i % len(band.monikers)],
                                        {"argv": ["seq", "-f", f"s{i}-%g", "1", "10000"]}, f"stress many {i}")
                                 for i in range(n)))
    checks = [lines_check(g[0] or "", f"s{i}-", 10000) for i, g in enumerate(got)]
    band.log("result", sub=sub, wall_s=round(time.monotonic() - started, 2),
             whole=sum(c["in_order_and_whole"] for c in checks), by_code=dict(sum((g[3] for g in got), collections.Counter())),
             slowest_read_s=round(max(g[4] for g in got), 3), missing_lines=sum(c["missing"] for c in checks))
    band.measure("after", sub=sub)


if __name__ == "__main__":
    rig.main("streams", body, n=3)
