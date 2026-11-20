"""Ranking memories for a question: recency + importance + relevance, as in generative agents.

- Recency: decay ** hours since the memory was last read.
- Importance: the model's 1..10 when the memory was written (scratch entries count as a knob's value).
- Relevance: BM25 of the question's words against the memory's words. Local, no model, no download.

Each of the three is scaled to 0..1 across the candidates (as the paper does), weighted, and summed.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from .store import now, parse_time

WORD = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
STOP = set("""a an and are as at be been but by can could did do does for from had has have how i if in
into is it its me my no not of on or our so than that the their them then there these they this to
up was we were what when where which who why will with would you your about after all also any just
more most only other out over said some such too very""".split())


def words(text):
    """Lowercase words, with dotted and dashed names (agent.chat.send, ha/runtime parts) kept whole and
    also split, so `agent.chat.send` matches a question about `chat`."""
    out = []
    for w in WORD.findall(text.lower()):
        if w not in STOP:
            out.append(w)
        parts = re.split(r"[._-]", w)
        if len(parts) > 1:
            out.extend(p for p in parts if p and p not in STOP)
    return out


@dataclass
class Item:
    kind: str           # episode, belief, scratch
    id: int
    text: str
    last_read: str
    importance: float
    extra: dict = field(default_factory=dict)
    recency: float = 0.0
    relevance: float = 0.0
    matched: list = field(default_factory=list)
    scaled: dict = field(default_factory=dict)
    score: float = 0.0

    @property
    def tag(self):
        return {"episode": "e", "belief": "b", "scratch": "s"}[self.kind] + str(self.id)


def bm25(query, docs, k1, b):
    """BM25 scores of `docs` (lists of words) for `query` (a list of words), and the query words each matched."""
    n = len(docs)
    if not n:
        return [], []
    avg = sum(len(d) for d in docs) / n or 1.0
    df = Counter()
    for d in docs:
        df.update(set(d))
    q = set(query)
    scores, matched = [], []
    for d in docs:
        tf = Counter(d)
        s, hit = 0.0, []
        for w in q:
            if tf[w]:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(d) / avg))
                hit.append(w)
        scores.append(s)
        matched.append(sorted(hit))
    return scores, matched


def _scale(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 if hi == 0 else 1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rank(items, question, knobs, at=None):
    """Score `items` for `question` and answer them best first. Every item keeps its raw and scaled parts."""
    if not items:
        return []
    r = knobs["recall"]
    at = parse_time(at or now())
    for item in items:
        hours = max(0.0, (at - parse_time(item.last_read)).total_seconds() / 3600)
        item.recency = r["recency_decay_per_hour"] ** hours
    rel, matched = bm25(words(question), [words(i.text) for i in items], r["bm25_k1"], r["bm25_b"])
    for item, s, m in zip(items, rel, matched):
        item.relevance, item.matched = s, m
    parts = {"recency": _scale([i.recency for i in items]),
             "importance": _scale([i.importance for i in items]),
             "relevance": _scale([i.relevance for i in items])}
    weights = {"recency": r["weight_recency"], "importance": r["weight_importance"], "relevance": r["weight_relevance"]}
    for n, item in enumerate(items):
        item.scaled = {k: parts[k][n] for k in parts}
        item.score = sum(weights[k] * item.scaled[k] for k in parts)
    return sorted(items, key=lambda i: (-i.score, i.kind, i.id))


def memories(store, knobs):
    """Every long-term memory a turn could be given: all episodes and the beliefs that stand."""
    items = [Item("episode", e["id"], e["text"], e["last_read"], e["importance"],
                  {"happened_at": e["happened_at"], "session": e["session"],
                   "lines": (e["first_line"], e["last_line"])})
             for e in store.q("SELECT * FROM episodes")]
    sql = "SELECT * FROM beliefs" if knobs["recall"]["include_superseded"] else \
        "SELECT * FROM beliefs WHERE superseded_by IS NULL"
    items += [Item("belief", b["id"], b["text"], b["last_read"], b["importance"],
                   {"confidence": b["confidence"], "superseded_by": b["superseded_by"]})
              for b in store.q(sql)]
    return items


def scratch(store, knobs):
    imp = knobs["recall"]["scratch_importance"]
    return [Item("scratch", s["id"], f"{s['role']}: {s['text']}", s["at"], imp,
                 {"session": s["session"], "line": s["line"], "at": s["at"]})
            for s in store.q("SELECT * FROM scratch")]
