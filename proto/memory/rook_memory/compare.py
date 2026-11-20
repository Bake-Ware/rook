"""Comparing embedding models on the same memory: misses, noise and time per search.

For each model (a [models] table in knobs.toml) and each question in madeup/questions.toml, recall
runs against this store as a turn would. A recalled item is relevant if its text has all the words
of one of the question's groups. A miss: the store holds relevant items but none was recalled.
Noise: recalled items that are not relevant. Time: one search (embedding the question and scoring
everything held), after the model is loaded and what is held is embedded once.
"""

import copy
import time
import tomllib

from . import embed, recall


def _relevant(text, groups):
    t = text.lower()
    return any(all(w in t for w in g) for g in groups)


def models(store, knobs, names, questions_path, out=print):
    with open(questions_path, "rb") as f:
        questions = tomllib.load(f)["question"]
    at = store.one("SELECT MAX(at) AS at FROM entries")["at"]
    every = store.all("SELECT text FROM beliefs") + store.all("SELECT text FROM episodes") + store.scratch()
    for name in names:
        k = copy.deepcopy(knobs)
        k["search"]["model"] = name
        started = time.monotonic()
        emb = embed.embedder(store.folder, k)
        loaded = time.monotonic() - started
        started = time.monotonic()
        recall.recall(store, k, "warm up", at, emb=emb)   # embeds everything held, once
        embedded = time.monotonic() - started
        out(f"\n== {name} ({k['models'][name]['repo']}): load {loaded:.1f} s, embedding what is held {embedded:.1f} s")
        misses = noise = recalled = 0
        found = {False: [0, 0], True: [0, 0]}      # said in other words or not: [found, asked]
        times = []
        for q in questions:
            started = time.monotonic()
            r = recall.recall(store, k, q["text"], at, emb=emb)
            times.append(time.monotonic() - started)
            items = r.beliefs + r.episodes + r.scratch
            good = [i for i in items if _relevant(i.text, q["relevant"])]
            bad = [i for i in items if not _relevant(i.text, q["relevant"])]
            exists = any(_relevant(row["text"], q["relevant"]) for row in every) if q["relevant"] else False
            miss = exists and not good
            misses += miss
            if exists:
                found[bool(q.get("other_words"))][1] += 1
                found[bool(q.get("other_words"))][0] += bool(good)
            noise += len(bad)
            recalled += len(items)
            out(f"  {'MISS ' if miss else '     '}{len(good):2} relevant, {len(bad):2} noise  (best {r.best:.2f})  {q['text']}")
            for i in bad[:3]:
                out(f"         noise {i.tag} {i.closeness:.2f} {i.text[:80]}")
        out(f"  {name}: {misses} misses, {noise} noise of {recalled} recalled, "
            f"{1000 * sum(times) / len(times):.0f} ms per search on average")
        out(f"  SUMMARY {name}: found {found[False][0]}/{found[False][1]} asked in the stored words, "
            f"{found[True][0]}/{found[True][1]} asked in other words; {noise} noise of {recalled} recalled; "
            f"{sum(1 for q in questions if not q['relevant'])} questions with nothing to find")


def bench(knobs, name, sizes, folder, out=print):
    """For the free-VM question: load time, one search against N stored items through the real recall
    path, embedding a dream's worth of new texts (20), and peak memory. The stored items are episodes
    with made-up sentences (drawn from the made-up set's words, so keyword search has a real index to
    search) and stand-in vectors (ranking cost does not depend on what the vectors say); the question
    and the 20 new texts go through the model for real. Prints one JSON line."""
    import json
    import pathlib
    import random
    import resource
    import numpy as np
    from . import search
    from .store import Store

    k = copy.deepcopy(knobs)
    k["search"]["model"] = name
    kind = search.kind(k)
    result = {"model": name, "kind": kind}
    started = time.monotonic()
    emb = embed.embedder(folder, k)
    if emb is not None:
        emb(["warm"])                               # the first call pages the weights in
    result["load_s"] = round(time.monotonic() - started, 2)
    question = "Where does the photo library backup go, and when does it run?"
    texts = ["Bake said the photo library backup goes to cachyrig.", "The steward moved the weekly report to Monday.",
             "Bake said Starscream is decommissioned and never coming back.", "The backup agent moved the job to 03:00.",
             "Bake said he likes dark mode on everything.", "The inventory agent uses nmap for the weekly sweep.",
             "Bake dropped the wake word; push to talk only.", "The guest wifi password changes every month.",
             "The old camera clips are in the archive folder on cachyrig.", "Bake asked for a 40 minute bread timer."] * 2
    started = time.monotonic()
    if emb is not None:
        emb(texts)
    result["embed_20_s"] = round(time.monotonic() - started, 2)
    dim = emb(["x"]).shape[1] if emb is not None else 0
    vocab = sorted({w for t in texts for w in search.words(t)} | {f"word{i}" for i in range(3000)})
    rnd = random.Random(1)
    rng = np.random.default_rng(1)
    for n in sizes:
        d = pathlib.Path(folder) / f"bench-{name}-{n}"
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("memory.sqlite3*"):
            f.unlink()
        store = Store(d)
        sid = store.session("bench", "bench", "bench")["id"]
        store.add_entry(sid, "2026-01-01T00:00:00Z", "bake", "chat", "x")
        store.run("UPDATE entries SET condensed_at = 'x'")
        store.db.executemany("INSERT INTO episodes (session, text, said_by, channel, kind, entries, happened_at, made_at)"
                             " VALUES (?, ?, 'bake', 'chat', 'statement', '[]', '2026-01-01T00:00:00+00:00', 'x')",
                             [(sid, " ".join(rnd.choice(vocab) for _ in range(12))) for _ in range(n)])
        store.db.commit()
        if dim:
            vecs = rng.standard_normal((n, dim)).astype(np.float32)
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
            ids = [r["id"] for r in store.all("SELECT id FROM episodes ORDER BY id")]
            store.put_vectors("episode", recall.model_key(k), [(i, v.tobytes()) for i, v in zip(ids, vecs)])
        if kind in ("keyword", "hybrid"):           # build the full-text index once, as a running store would have
            started = time.monotonic()
            recall.recall(store, k, "index", "2026-09-01T00:00:00+00:00", emb=emb)
            result[f"index_{n}_s"] = round(time.monotonic() - started, 2)
        times = []
        for _ in range(3):
            started = time.monotonic()
            recall.recall(store, k, question, "2026-09-01T00:00:00+00:00", emb=emb)
            times.append(time.monotonic() - started)
        result[f"search_{n}_s"] = round(min(times), 3)
        store.close()
        for f in d.glob("memory.sqlite3*"):
            f.unlink()
        d.rmdir()
    result["peak_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    out(json.dumps(result))
    return result


def opposites(store, knobs, out=print):
    """Every pair of beliefs marked as opposites, with each one's standing with the knob off and on."""
    from . import standing
    off, on = copy.deepcopy(knobs), copy.deepcopy(knobs)
    off["beliefs"]["opposites"], on["beliefs"]["opposites"] = False, True
    pairs = store.all("SELECT * FROM opposites ORDER BY a")
    out(f"{len(pairs)} pair(s) marked as opposites")
    for p in pairs:
        out(f"\n  marked {str(p['at'])[:10]} by {p['by']}")
        for bid in (p["a"], p["b"]):
            b = store.one("SELECT * FROM beliefs WHERE id = ?", bid)
            out(f"  [b{bid}] {b['text']}")
            out(f"      off: {standing.summary(standing.of(store, off, bid))}")
            out(f"      on:  {standing.summary(standing.of(store, on, bid))}")


def _belief_hits(store, knobs, text, at, relevant, upto):
    r = recall.recall(store, knobs, text, at)
    good = [b for b in r.beliefs if b.id <= upto and _relevant(b.text, relevant)]
    return bool(good), r


def heal_test(store, knobs, path, start, per_day=8, others=(), out=print):
    """Misses and healing. Each question is asked cold (recall only, no model), then live as Bake (the
    house agent may search in its own words; a dream follows each day), then cold again. A question
    counts as found when recall turns up a belief held before the test with the question's words.
    The other searches named are asked cold before and after too (they do not heal themselves, but
    the dreams may add to what they search)."""
    import datetime
    import json
    from . import dream as dream_mod, turn
    from .store import parse_time

    with open(path, "rb") as f:
        questions = tomllib.load(f)["question"]
    upto = store.one("SELECT MAX(id) AS n FROM beliefs")["n"] or 0
    t0 = parse_time(start)

    def ask_all(label, model):
        k = copy.deepcopy(knobs)
        k["search"]["model"] = model
        at = (t0 + datetime.timedelta(days=60)).isoformat()
        return [_belief_hits(store, k, q["text"], at, q["relevant"], upto)[0] for q in questions]

    before = {m: ask_all("cold", m) for m in [knobs["search"]["model"], *others]}
    sess = turn.session(store, "heal test")
    for n, q in enumerate(questions):
        day = t0 + datetime.timedelta(days=n // per_day, minutes=3 * (n % per_day))
        out(f"[{day.isoformat()[:16]}] bake: {q['text']}")
        answer, r = turn.say(store, knobs, sess, q["text"], knobs["people"]["person"], day.isoformat(), log=out)
        out(f"    recalled: {', '.join(i.tag for i in r.beliefs) or 'no beliefs'}\n  agent: {(answer or '')[:300]}")
    dream_mod.run(store, knobs, sess["id"], trigger="idle", log=out)
    after = {m: ask_all("healed", m) for m in before}

    main = knobs["search"]["model"]
    rows = store.all("SELECT * FROM misses WHERE session = ? ORDER BY id", sess["id"])
    by_query = {}
    for m in rows:
        by_query.setdefault(m["query"], m)
    out("\n== heal test: found cold / after the dreams")
    for i, q in enumerate(questions):
        m = by_query.get(q["text"])
        heal = ""
        if m:
            heals = store.all("SELECT kind, words FROM heals WHERE miss = ?", m["id"])
            found = json.loads(m["found"] or "[]")
            heal = f"  [{m['status']}" + (f"; agent's search found {', '.join(found)}" if found else "") + \
                   (f"; healed with {'; '.join(h['kind'] + ' ' + h['words'] for h in heals)}" if heals else "") + "]"
        out(f"  {'yes' if before[main][i] else 'no ':3} -> {'yes' if after[main][i] else 'no ':3}  {q['text']}{heal}")
    for m, cold in before.items():
        out(f"  SUMMARY {m}: found {sum(cold)}/{len(questions)} cold, {sum(after[m])}/{len(questions)} after the dreams")
    return before, after


def bench_graph(knobs, sizes, folder, out=print):
    """The graph's read path at N beliefs, with N/5 things (two aliases each), about 1.5 about-edges a
    belief and N/10 thing-to-thing edges, all filler sentences from the made-up set's words. Uses
    Python's sqlite3 and nothing else; says whether numpy was ever imported."""
    import json
    import pathlib
    import random
    import resource
    import sys
    from . import search
    from .store import Store

    k = copy.deepcopy(knobs)
    k["search"]["model"] = "graph"
    result = {"model": "graph", "kind": "graph"}
    words = sorted({w for w in search.words(" ".join([
        "photo library backup cachyrig starscream decommissioned wake word push talk kitchen speaker dashboard dark mode",
        "light tablet guest wifi vault vlan network nas storage digest morning steward report monday logs zstd nmap",
        "sweep inventory agent camera clips archive sprinklers garden timer bread porch sun night"]))} |
                   {f"word{i}" for i in range(3000)})
    rnd = random.Random(1)
    question = "Is that old server we retired in the winter available again?"
    for n in sizes:
        d = pathlib.Path(folder) / f"bench-graph-{n}"
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("memory.sqlite3*"):
            f.unlink()
        store = Store(d)
        db = store.db
        db.executemany("INSERT INTO beliefs (text, about, made_at) VALUES (?, 'world', '2026-01-01T00:00:00+00:00')",
                       [(" ".join(rnd.choice(words) for _ in range(10)),) for _ in range(n)])
        db.executemany("INSERT INTO things (name, made_at) VALUES (?, '2026-01-01')",
                       [(" ".join(rnd.choice(words) for _ in range(2)),) for _ in range(n // 5)])
        db.executemany("INSERT INTO aliases (thing, alias, at, how) VALUES (?, ?, '2026-01-01', 'dream')",
                       [(t, " ".join(rnd.choice(words) for _ in range(2))) for t in range(1, n // 5 + 1) for _ in range(2)])
        db.executemany("INSERT INTO edges (src_kind, src, rel, dst_kind, dst, at) VALUES ('belief', ?, 'about', 'thing', ?, 'x')",
                       [(rnd.randint(1, n), rnd.randint(1, n // 5)) for _ in range(n * 3 // 2)])
        db.executemany("INSERT INTO edges (src_kind, src, rel, dst_kind, dst, at) VALUES ('thing', ?, 'related', 'thing', ?, 'x')",
                       [(rnd.randint(1, n // 5), rnd.randint(1, n // 5)) for _ in range(n // 10)])
        # the real Starscream, so the question has something to find
        t = store.add_thing("Starscream", "2026-01-01")
        store.add_alias(t, "the old server we retired", "2026-01-01")
        b = store.add_belief("Starscream is decommissioned and never coming back", "world")
        store.add_edge("belief", b, "about", "thing", t, "2026-01-01")
        db.commit()
        started = time.monotonic()
        recall.recall(store, k, "index", "2026-09-01T00:00:00+00:00")       # builds both full-text indexes
        result[f"index_{n}_s"] = round(time.monotonic() - started, 2)
        times, hit = [], False
        for _ in range(3):
            started = time.monotonic()
            r = recall.recall(store, k, question, "2026-09-01T00:00:00+00:00")
            times.append(time.monotonic() - started)
            hit = any(x.id == b for x in r.beliefs)
        result[f"search_{n}_s"] = round(min(times), 3)
        result[f"found_{n}"] = hit
        store.close()
        for f in d.glob("memory.sqlite3*"):
            f.unlink()
        d.rmdir()
    result["peak_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    result["numpy_imported"] = "numpy" in sys.modules
    out(json.dumps(result))
    return result
