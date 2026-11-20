"""How close a memory is to a question, by whichever search the knobs choose ([models] in knobs.toml).

- "bert" and "static": cosine closeness of embeddings (see embed.py).
- "keyword": SQLite full-text search (FTS5, Porter stemming), no embedding at all. Closeness is the
  share of the question's content words the memory contains. A belief is indexed with the other
  ways of saying it that the dream wrote down with it, so a question in other words can still meet
  one of them; and a live turn may search again in its own words.
- "graph": things, aliases and edges the dream wrote, read with SQLite alone (see graph.py).
- "hybrid": keyword and a static table together. Each has its own floor; closeness is the better of
  the two, each as a multiple of its floor, so a memory is recalled when either finds it (the
  floor of the hybrid is 1.0).

Two texts' closeness (pin matching, the dream's same check, newer words in the scratch pad) is the
embedding cosine, or for keyword the share of content words they have in common (Dice).
"""

import json
import re

from . import embed
from .knobs import search as search_knobs

STOP = set("""a an the and or but if then so of to in on at by for with from about into over as is are was were be been
being am do does did done have has had having i me my mine you your yours he him his she her it its we us our they them
their this that these those there here what which who whom whose when where why how can could will would shall should may
might must just still now again also any some all no not yes please tell let lets say said go goes going get gets got
days day these way ways thing things very really""".split())


def kind(knobs):
    return search_knobs(knobs).get("kind", "bert")


def words(text):
    out = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in STOP or len(w) < 2:
            continue
        out.append(w)
    return list(dict.fromkeys(out))


def _stem(w):
    for suffix in ("ing", "ed", "es", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[:-len(suffix)]
    return w


def dice(a, b):
    A, B = {_stem(w) for w in words(a)}, {_stem(w) for w in words(b)}
    return 2 * len(A & B) / (len(A) + len(B)) if A and B else 0.0


def body(kind_, row):
    """The text a memory is found by: a belief's own words and the other ways of saying it."""
    text = row["text"]
    if kind_ == "belief" and "wordings" in row.keys() and row["wordings"]:
        text += " | " + " | ".join(json.loads(row["wordings"]))
    return text


# --- embeddings ---

def vectors(store, knobs, kind_, rows, emb):
    import numpy as np
    s = search_knobs(knobs)
    key = f"{s['repo'] if s.get('kind') != 'hybrid' else s['static']}|{s.get('pooling')}"
    have = store.vectors(kind_, key)
    missing = [r for r in rows if r["id"] not in have]
    if missing:
        made = emb([r["text"] for r in missing])
        pairs = [(r["id"], made[i].astype("float32").tobytes()) for i, r in enumerate(missing)]
        store.put_vectors(kind_, key, pairs)
        have.update(dict(pairs))
    return {r["id"]: np.frombuffer(have[r["id"]], dtype=np.float32) for r in rows}


# --- keyword (SQLite FTS5) ---

def _index(store, rows_by_kind):
    """Keeps the full-text index in step with the memories: new ones are added, changed ones (a pin
    rewords a belief) are replaced by their row id."""
    cols = [r[1] for r in store.db.execute("PRAGMA table_info(memory_fts_seen)")]
    if cols and "rid" not in cols:          # an index from before row ids were kept: rebuild it
        store.db.execute("DROP TABLE memory_fts_seen")
        store.db.execute("DROP TABLE IF EXISTS memory_fts")
    store.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(kind UNINDEXED, ref UNINDEXED, body,"
                     " tokenize = 'porter unicode61')")
    store.db.execute("CREATE TABLE IF NOT EXISTS memory_fts_seen (kind TEXT, ref INTEGER, body TEXT, rid INTEGER,"
                     " PRIMARY KEY (kind, ref))")
    seen = {(r[0], r[1]): (r[2], r[3]) for r in store.db.execute("SELECT kind, ref, body, rid FROM memory_fts_seen")}
    for k, rows in rows_by_kind.items():
        for r in rows:
            b = body(k, r)
            old = seen.get((k, r["id"]))
            if old and old[0] == b:
                continue
            if old:
                store.db.execute("DELETE FROM memory_fts WHERE rowid = ?", (old[1],))
            rid = store.db.execute("INSERT INTO memory_fts (kind, ref, body) VALUES (?, ?, ?)", (k, r["id"], b)).lastrowid
            store.db.execute("INSERT OR REPLACE INTO memory_fts_seen (kind, ref, body, rid) VALUES (?, ?, ?, ?)",
                             (k, r["id"], b, rid))
    store.db.commit()


def keyword(store, query, rows_by_kind):
    """{(kind, id): share of the question's content words found}, through FTS5."""
    _index(store, rows_by_kind)
    terms = words(query)
    wanted = {(k, r["id"]) for k, rows in rows_by_kind.items() for r in rows}
    hits = {}
    for t in terms:
        for k, ref in store.db.execute("SELECT kind, ref FROM memory_fts WHERE memory_fts MATCH ?", (f'"{t}"',)):
            if (k, ref) in wanted:
                hits[(k, ref)] = hits.get((k, ref), 0) + 1
    return {key: n / len(terms) for key, n in hits.items()} if terms else {}


# --- the two questions every search answers ---

last_via = {}          # (kind, id): how the last query reached it (graph search only)


def query(store, knobs, text, rows_by_kind, emb="load"):
    """{(kind, id): closeness} for every row given."""
    s = search_knobs(knobs)
    k = s.get("kind", "bert")
    last_via.clear()
    if k == "graph":
        from . import graph
        close, via, _things = graph.query(store, knobs, text, rows_by_kind)
        last_via.update(via)
        return close
    out = {(kk, r["id"]): 0.0 for kk, rows in rows_by_kind.items() for r in rows}
    if k in ("keyword", "hybrid"):
        kw = keyword(store, text, rows_by_kind)
    if k in ("bert", "static", "hybrid"):
        emb = embed.embedder(store.folder, knobs) if emb == "load" else emb
        q = emb([s.get("query_prefix", "") + text])[0]
        cos = {}
        for kk, rows in rows_by_kind.items():
            if rows:
                vec = vectors(store, knobs, kk, rows, emb)
                cos.update({(kk, i): float(v @ q) for i, v in vec.items()})
    if k == "keyword":
        out.update(kw)
    elif k == "hybrid":
        for key in out:
            out[key] = max(kw.get(key, 0.0) / s["keyword_floor"], cos.get(key, 0.0) / s["static_floor"])
    else:
        out.update(cos)
    return out


def pair(store, knobs, texts, rows, kind_="belief", emb="load"):
    """[[closeness of texts[i] to rows[j]]]: embedding cosine, or word overlap for keyword search."""
    if kind(knobs) in ("keyword", "graph"):
        return [[dice(t, body(kind_, r)) for r in rows] for t in texts]
    emb = embed.embedder(store.folder, knobs) if emb == "load" else emb
    if not rows or not texts:
        return [[0.0] * len(rows) for _ in texts]
    vec = vectors(store, knobs, kind_, rows, emb)
    tv = emb(texts)
    return [[float(vec[r["id"]] @ tv[i]) for r in rows] for i in range(len(texts))]


def pair_rows(store, knobs, a_kind, a_rows, b_kind, b_rows, emb="load"):
    """[[closeness of a_rows[i] to b_rows[j]]] for stored rows (newer words: entries against beliefs)."""
    if kind(knobs) in ("keyword", "graph"):
        return [[dice(body(a_kind, a), body(b_kind, b)) for b in b_rows] for a in a_rows]
    emb = embed.embedder(store.folder, knobs) if emb == "load" else emb
    if not a_rows or not b_rows:
        return [[0.0] * len(b_rows) for _ in a_rows]
    va, vb = vectors(store, knobs, a_kind, a_rows, emb), vectors(store, knobs, b_kind, b_rows, emb)
    return [[float(va[a["id"]] @ vb[b["id"]]) for b in b_rows] for a in a_rows]
