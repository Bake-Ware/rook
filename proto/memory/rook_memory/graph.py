"""Reading the graph: what runs every turn on the hub, with Python's sqlite3 and nothing else.

All the thinking happens when memory is written: the dream names the things beliefs are about, the
other ways each thing is said (aliases), and the edges between them. Reading is a lookup:
1. the message's words against thing names and aliases (SQLite full-text search, Porter stemming):
   a thing is hit when all the content words of one of its names are in the message;
2. from the things hit, walk out over edges (about, part of, related to) up to `hops` steps and
   collect the beliefs on the way; nearer is closer (`walk_decay` per step);
3. beliefs, episodes and scratch entries whose own words (and the dream's other wordings) cover
   enough of the message are found directly, as keyword search does;
4. opposites of what was found come along, marked (recall.py does that part).
No model and no embeddings anywhere in it.
"""

from . import search


def _index_things(store):
    db = store.db
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS thing_fts USING fts5(thing UNINDEXED, alias UNINDEXED, body,"
               " tokenize = 'porter unicode61')")
    db.execute("CREATE TABLE IF NOT EXISTS thing_fts_seen (alias INTEGER PRIMARY KEY, thing INTEGER, body TEXT, rid INTEGER)")
    seen = {r[0]: (r[2], r[3]) for r in db.execute("SELECT alias, thing, body, rid FROM thing_fts_seen")}
    # an alias id of -thing stands for the thing's own name
    rows = [(-t["id"], t["id"], t["name"]) for t in store.all("SELECT id, name FROM things")]
    rows += [(a["id"], a["thing"], a["alias"]) for a in store.all("SELECT id, thing, alias FROM aliases")]
    for aid, tid, body in rows:
        old = seen.get(aid)
        if old and old[0] == body:
            continue
        if old:
            db.execute("DELETE FROM thing_fts WHERE rowid = ?", (old[1],))
        rid = db.execute("INSERT INTO thing_fts (thing, alias, body) VALUES (?, ?, ?)", (tid, aid, body)).lastrowid
        db.execute("INSERT OR REPLACE INTO thing_fts_seen (alias, thing, body, rid) VALUES (?, ?, ?, ?)", (aid, tid, body, rid))
    db.commit()


def match_things(store, text):
    """{thing id: (1.0, the name or alias that matched)} for things fully named in the message."""
    _index_things(store)
    terms = search.words(text)
    counts, bodies = {}, {}
    for t in terms:
        for tid, aid, body in store.db.execute("SELECT thing, alias, body FROM thing_fts WHERE thing_fts MATCH ?",
                                               (f'"{t}"',)):
            counts[aid] = counts.get(aid, 0) + 1
            bodies[aid] = (tid, body)
    hit = {}
    for aid, n in counts.items():
        tid, body = bodies[aid]
        need = len(search.words(body))
        if need and n >= need:
            hit.setdefault(tid, (1.0, body))
    return hit


def walk(store, things, hops, decay):
    """{belief id: (closeness, how it was reached)} from the things hit, out to `hops` steps."""
    found = {}
    frontier = {tid: (score, f"'{name}'") for tid, (score, name) in things.items()}
    seen = set(frontier)
    for step in range(hops + 1):
        nxt = {}
        for tid, (score, how) in frontier.items():
            for e in store.all("SELECT * FROM edges WHERE (dst_kind = 'thing' AND dst = ?) OR (src_kind = 'thing' AND src = ?)",
                               tid, tid):
                if e["rel"] == "about" and e["src_kind"] == "belief":
                    s = score * decay ** step
                    if s > found.get(e["src"], (0, ""))[0]:
                        found[e["src"]] = (s, how + (f", {step} step{'s' if step != 1 else ''} out" if step else ""))
                elif e["rel"] in ("part_of", "related") and e["src_kind"] == "thing" and e["dst_kind"] == "thing":
                    other = e["dst"] if e["src"] == tid else e["src"]
                    if other not in seen:
                        seen.add(other)
                        name = store.one("SELECT name FROM things WHERE id = ?", other)["name"]
                        nxt[other] = (score, f"{how} -> {e['rel']} '{name}'")
        frontier = nxt
        if not frontier:
            break
    return found


def query(store, knobs, text, rows_by_kind):
    """{(kind, id): closeness} and {(kind, id): how it was reached}."""
    from .knobs import search as search_knobs
    s = search_knobs(knobs)
    close = {(k, r["id"]): 0.0 for k, rows in rows_by_kind.items() for r in rows}
    via = {}
    for key, c in search.keyword(store, text, rows_by_kind).items():
        close[key] = c
        via[key] = f"its own words ({c:.0%} of the message's)"
    things = match_things(store, text)
    for bid, (c, how) in walk(store, things, s.get("hops", 1), s.get("walk_decay", 0.6)).items():
        key = ("belief", bid)
        if key in close and c > close[key]:
            close[key] = c
            via[key] = f"about {how}"
    return close, via, things


def neighbourhood(store, kind, ref):
    """Lines describing one thing's or belief's neighbourhood: names, aliases, edges, links."""
    out = []
    if kind == "thing":
        t = store.one("SELECT * FROM things WHERE id = ?", ref)
        if not t:
            return [f"no thing t{ref}"]
        out.append(f"[t{ref}] {t['name']}  (made {str(t['made_at'])[:10]} by dream {t['dream'] or '-'})")
        for a in store.all("SELECT * FROM aliases WHERE thing = ? ORDER BY id", ref):
            out.append(f"    also said: \"{a['alias']}\"  ({a['how']}, {str(a['at'])[:10]}, dream {a['dream'] or '-'},"
                       f" from {a['entries']})")
    else:
        b = store.one("SELECT * FROM beliefs WHERE id = ?", ref)
        if not b:
            return [f"no belief b{ref}"]
        out.append(f"[b{ref}] {b['text']}  (about {b['about']})")
    for e in store.edges_of(kind, ref):
        mine = e["src_kind"] == kind and e["src"] == ref
        ok, oid = (e["dst_kind"], e["dst"]) if mine else (e["src_kind"], e["src"])
        if ok == "thing":
            name = store.one("SELECT name FROM things WHERE id = ?", oid)["name"]
            label = f"[t{oid}] {name}"
        else:
            label = f"[b{oid}] {store.one('SELECT text FROM beliefs WHERE id = ?', oid)['text']}"
        arrow = f"{e['rel']} ->" if mine else f"<- {e['rel']}"
        out.append(f"    {arrow} {label}  ({e['how']}, dream {e['dream'] or '-'}, from {e['entries']})")
    if kind == "belief":
        for l in store.links(ref):
            out.append(f"    rests on [e{l['episode']}] {'for' if l['stance'] > 0 else 'against'} {l['strength']:.2f}"
                       f" ({l['said_by']}, {l['kind']}): {l['episode_text'][:100]}")
    return out
