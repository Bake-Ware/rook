"""Memory's read path at size (brief MEM5): recall on the hub, with Python's sqlite3 and nothing else.

    systemd-run --user --scope -p CPUQuota=12.5% -p MemoryMax=600M -p MemorySwapMax=0 \
        .venv/bin/python tests/stress/memory.py 500 5000 50000

(the AMD micro's limits: 1/8 of a core, 600 MB). For each size N it makes a memory.db in a new folder
under /tmp of filler as round four's bench made it (proto/memory/rook_memory/compare.py, bench_graph):
N beliefs of ten filler words, each with one link to an episode, N/5 things with two aliases each,
1.5 about-edges a belief and N/10 thing-to-thing edges, plus the real Starscream, its alias "the old
server we retired" and the belief about it. Then it times recall (hub_memory.recall, exactly what a turn
runs) for a question that names it in other words, and one that names nothing (a thin recall, with its
hint). One JSON line: the times (the first call, and the best of five), whether the question found
Starscream, peak memory, and whether numpy was ever imported. `--hub N` instead starts a whole hub (not
serving) on a memory of N beliefs and times agent.memory.search through it, with the hub's peak memory.
"""

import importlib.util
import json
import pathlib
import random
import resource
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "hub"), str(ROOT / "protocol" / "python")]
spec = importlib.util.spec_from_file_location("hub_memory", ROOT / "hub/rook_hub/defaults/agents/hub_memory.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

WORDS = sorted(set(m.words(
    "photo library backup cachyrig decommissioned wake word push talk kitchen speaker dashboard dark mode "
    "light tablet guest wifi vault vlan network nas storage digest morning steward report monday logs zstd nmap "
    "sweep inventory agent camera clips archive sprinklers garden timer bread porch sun night")) | {f"word{i}" for i in range(3000)})
NAMED = "Is that old server we retired in the winter available again?"
THIN = "Did the porch light timer get fixed?"


def build(folder, n, rnd):
    s = m.Store(folder / "memory.db")
    db, now = s.db, time.time()
    sentence = lambda k: " ".join(rnd.choice(WORDS) for _ in range(k))
    with s.write():
        db.executemany("INSERT INTO beliefs (id, text, about, made_at, touched, summary) VALUES (?, ?, 'world', ?, ?, ?)",
                       [(i, sentence(10), now, now, "holds, tentative (+0.33; 1 occasion)") for i in range(1, n + 1)])
        db.execute("INSERT INTO belief_fts (rowid, body) SELECT id, text FROM beliefs")
        db.executemany("INSERT INTO episodes (id, text, said_by, channel, kind, entries, happened_at, made_at)"
                       " VALUES (?, ?, 'person:Alice', 'site', 'statement', '[]', ?, ?)",
                       [(i, sentence(12), now, now) for i in range(1, n + 1)])
        db.executemany("INSERT INTO links (belief, episode, stance, strength, occasion, flips, at) VALUES (?, ?, 1, 0.6,"
                       " '2026-01-01', 1, ?)", [(i, i, now) for i in range(1, n + 1)])
        things = max(1, n // 5)
        db.executemany("INSERT INTO things (id, name, made_at) VALUES (?, ?, ?)",
                       [(t, sentence(2), now) for t in range(1, things + 1)])
        names = [(t, name, len(m.words(name)), "name", now) for t, name in db.execute("SELECT id, name FROM things")]
        names += [(t, sentence(2), 2, "alias", now) for t in range(1, things + 1) for _ in range(2)]
        db.executemany("INSERT OR IGNORE INTO names (thing, name, words, how, at) VALUES (?, ?, ?, ?, ?)", names)
        db.execute("INSERT INTO name_fts (rowid, name) SELECT id, name FROM names")
        db.executemany("INSERT OR IGNORE INTO edges (src_kind, src, rel, dst_kind, dst, at) VALUES ('belief', ?, 'about',"
                       " 'thing', ?, ?)", [(rnd.randint(1, n), rnd.randint(1, things), now) for _ in range(n * 3 // 2)])
        db.executemany("INSERT OR IGNORE INTO edges (src_kind, src, rel, dst_kind, dst, at) VALUES ('thing', ?, 'related',"
                       " 'thing', ?, ?)", [(rnd.randint(1, things), rnd.randint(1, things), now) for _ in range(n // 10)])
        t = s.add_thing("Starscream", now)
        s.add_name(t, "the old server we retired", now)
        b = s.add_belief("Starscream is decommissioned and never coming back", "world", now)
        ep = s.run("INSERT INTO episodes (text, said_by, channel, kind, entries, happened_at, made_at) VALUES"
                   " ('Alice said Starscream is gone', 'person:Alice', 'site', 'correction', '[]', ?, ?)", now, now)
        s.add_link(b, ep, 1, 1.0, "2026-01-01", now)
        s.add_edge("belief", b, "about", "thing", t, now)
    return s, b


def timed(fn):
    best, out = None, None
    for _ in range(5):
        started = time.perf_counter()
        out = fn()
        took = time.perf_counter() - started
        best = took if best is None else min(best, took)
    return best, out


def main(sizes):
    rnd = random.Random(1)
    result = {}
    for n in sizes:
        folder = pathlib.Path(tempfile.mkdtemp(prefix=f"rook-memory-{n}-"))
        try:
            started = time.perf_counter()
            s, b = build(folder, n, rnd)
            result[f"build_{n}_s"] = round(time.perf_counter() - started, 1)
            started = time.perf_counter()
            m.recall(s, NAMED, time.time(), 1, 6)
            result[f"first_{n}_ms"] = round((time.perf_counter() - started) * 1000, 1)
            took, r = timed(lambda: m.recall(s, NAMED, time.time(), 1, 6))
            result[f"recall_{n}_ms"] = round(took * 1000, 1)
            result[f"found_{n}"] = r["beliefs"][0]["id"] == b if r["beliefs"] else False
            took, block = timed(lambda: m.for_turn(s, m.recall(s, NAMED, time.time(), 1, 6)))
            result[f"recall_and_block_{n}_ms"] = round(took * 1000, 1)
            took, r = timed(lambda: m.recall(s, THIN, time.time(), 1, 6))
            result[f"thin_{n}_ms"] = round(took * 1000, 1)
            result[f"thin_{n}_hint"] = len(r["hint"])
            result[f"db_{n}_mb"] = round((folder / "memory.db").stat().st_size / 1e6, 1)
            s.db.close()
        finally:
            shutil.rmtree(folder)
    result["peak_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    result["numpy_imported"] = "numpy" in sys.modules
    print(json.dumps(result))


def hub(n):
    """A whole hub (not serving) holding a memory of N beliefs: its peak memory, and agent.memory.search
    through the hub's own call path, as a turn's agent calls it."""
    import asyncio
    from rook_hub import Hub
    rnd = random.Random(1)
    folder = pathlib.Path(tempfile.mkdtemp(prefix=f"rook-memory-hub-{n}-"))
    try:
        data = folder / "hub" / "plugin-data" / "agents"
        data.mkdir(parents=True)
        s, b = build(data, n, rnd)
        s.db.close()
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024

        async def go():
            h = Hub(folder / "hub", vault_key=bytes(32))
            user = h.auth.create_user("Alice", "a password", first=True)
            times = []
            for _ in range(5):
                started = time.perf_counter()
                got = await h.call("hub", "agent.memory.search", {"query": NAMED}, "The memory bench.", user=user["guid"])
                times.append(time.perf_counter() - started)
            h.close()
            return got, times
        got, times = asyncio.run(go())
        print(json.dumps({"beliefs": n, "found": got["result"]["beliefs"][0]["id"] == f"b{b}",
                          "search_first_ms": round(times[0] * 1000, 1), "search_best_ms": round(min(times) * 1000, 1),
                          "peak_mb_building": before, "peak_mb_hub": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
                          "numpy_imported": "numpy" in sys.modules}))
    finally:
        shutil.rmtree(folder)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--hub"]:
        hub(int(sys.argv[2]))
    else:
        main([int(a) for a in sys.argv[1:]] or [500, 5000, 50000])
