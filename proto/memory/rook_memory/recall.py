"""Recall: what memory hands a turn for a message, and why.

Three places are searched, by meaning (local embeddings, cosine closeness):
- the scratch pad: what was said word for word and no dream has covered yet. Newer words win;
- episodes: facts the dreams distilled, each with who it comes from;
- beliefs: each with its standing, worked out from its links (see standing.py).

Anything less close than `miss_below` is not recalled at all. When nothing clears it, the turn is
told to say "I don't remember", and nothing is written down about the miss.
"""

from dataclasses import dataclass, field

from . import search, standing
from .knobs import search as search_knobs
from .store import parse_time


@dataclass
class Item:
    kind: str           # entry, episode, belief
    id: int
    text: str
    who: str
    when: str
    closeness: float = 0.0
    recency: float = 0.0
    certainty: float = 0.0
    score: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def tag(self):
        return {"entry": "s", "episode": "e", "belief": "b"}[self.kind] + str(self.id)


@dataclass
class Recall:
    query: str
    at: str
    scratch: list
    episodes: list
    beliefs: list
    best: float

    @property
    def missed(self):
        return not (self.scratch or self.episodes or self.beliefs)

    def items(self):
        return self.scratch + self.episodes + self.beliefs


def model_key(knobs):
    s = search_knobs(knobs)
    return f"{s['repo'] if s.get('kind') != 'hybrid' else s.get('static')}|{s.get('pooling')}"


def _vectors(store, knobs, kind, rows, emb):
    return search.vectors(store, knobs, kind, rows, emb)


def _recency(knobs, last, at):
    hours = max(0.0, (parse_time(at) - parse_time(last)).total_seconds() / 3600)
    return knobs["search"]["recency_decay_per_hour"] ** hours


def recall(store, knobs, query, at, exclude_entries=(), emb="load"):
    """emb: an embedder already loaded, or "load" (the knobs' one; none for keyword search)."""
    s = search_knobs(knobs)
    out = {"entry": [], "episode": [], "belief": []}
    entries = [r for r in store.scratch() if r["id"] not in set(exclude_entries) and r["at"] <= at]
    episodes = [r for r in store.all("SELECT * FROM episodes ORDER BY id") if r["happened_at"] <= at]
    beliefs = store.all("SELECT * FROM beliefs ORDER BY id")
    close = search.query(store, knobs, query, {"entry": entries, "episode": episodes, "belief": beliefs},
                         emb=emb if emb is not None else "load")
    best = max(close.values(), default=0.0)
    floor = min(s["miss_below"], s["pinned_miss_below"])

    for r in entries:
        it = Item("entry", r["id"], r["text"], r["speaker"], r["at"], extra={"channel": r["channel"], "session": r["session"]})
        it.closeness = close[("entry", r["id"])]
        it.recency = _recency(knobs, r["last_used"] or r["at"], at)
        out["entry"].append(it)
    for r in episodes:
        it = Item("episode", r["id"], r["text"], r["said_by"], r["happened_at"],
                  extra={"kind": r["kind"], "channel": r["channel"], "session": r["session"], "entries": r["entries"]})
        it.closeness = close[("episode", r["id"])]
        it.recency = _recency(knobs, r["last_used"] or r["happened_at"], at)
        out["episode"].append(it)
    for r in beliefs:
        c = close[("belief", r["id"])]
        if c < floor:          # too far to be recalled even if pinned: no need to work out its standing
            continue
        st = standing.of(store, knobs, r["id"])
        links = standing.links_of(store, knobs, r["id"])
        it = Item("belief", r["id"], r["text"], r["about"], r["made_at"],
                  extra={"standing": st, "links": links, "about": r["about"], "row": r})
        it.closeness = c
        it.recency = _recency(knobs, r["last_used"] or (links[-1]["at"] if links else r["made_at"]), at)
        it.certainty = abs(st["value"])
        out["belief"].append(it)

    for kind, items in out.items():
        for it in items:
            it.score = (s["weight_closeness"] * it.closeness + s["weight_recency"] * it.recency
                        + s["weight_certainty"] * it.certainty)
        keep = sorted((i for i in items if i.closeness >= s["miss_below"]
                       or (i.kind == "belief" and i.extra["standing"]["pinned"] and i.closeness >= s["pinned_miss_below"])),
                      key=lambda i: -i.score)
        out[kind] = keep[:s[{"entry": "turn_scratch", "episode": "turn_episodes", "belief": "turn_beliefs"}[kind]]]

    # Newer words win: a scratch pad entry close to a belief and newer than its latest link.
    if out["belief"] and out["entry"]:
        rows_e = [store.one("SELECT * FROM entries WHERE id = ?", e.id) for e in out["entry"]]
        rows_b = [b.extra["row"] for b in out["belief"]]
        grid = search.pair_rows(store, knobs, "entry", rows_e, "belief", rows_b, emb=emb if emb is not None else "load")
        for j, b in enumerate(out["belief"]):
            latest = b.extra["links"][-1]["at"] if b.extra["links"] else b.when
            for i, e in enumerate(out["entry"]):
                if e.when > latest and grid[i][j] >= s["newer_words"]:
                    b.extra.setdefault("newer_words", []).append(e.tag)
    return Recall(query, at, out["entry"], out["episode"], out["belief"], best)


def _day(t):
    return str(t)[:16].replace("T", " ")


def belief_line(it, links_shown=3):
    st = it.extra["standing"]
    head = f"[{it.tag}] {standing.summary(st)}"
    if st["pinned"]:
        head += f", pinned {_day(st['pinned_at'])[:10]} from his words \"{(st.get('pinned_words') or '')[:120]}\""
    if it.extra.get("newer_words"):
        head += f"; newer words in {', '.join(it.extra['newer_words'])} go first"
    lines = [head, f"     {it.text}"]
    for l in it.extra["links"][-links_shown:]:
        mark = "+" if l["stance"] > 0 else "-"
        lines.append(f"     {mark} {_day(l['at'])[:10]} {l['said_by']} ({l['kind']}, {l['strength']:.2f}) [e{l['episode']}]"
                     f" {l['episode_text'][:140]}" + (f"  ({l['note']})" if l["note"] else "")
                     + (f"  (via its opposite b{l['via']})" if l.get("via") else ""))
    return "\n".join(lines)


def for_turn(r):
    """The memory block a live turn is given under the incoming message."""
    if r.missed:
        return "Memory: nothing close to this message. If it asks for something from before, say you don't remember."
    out = ["Memory (closest first; ids let you pin):"]
    if r.scratch:
        out.append("Said recently, word for word (newest wins over beliefs):")
        out += [f"  [{i.tag}] {_day(i.when)} {i.who}{' (voice)' if i.extra['channel'] == 'voice' else ''}: {i.text}"
                for i in r.scratch]
    if r.beliefs:
        out.append("Beliefs (standing worked out from the evidence; + supports, - against):")
        out += ["  " + belief_line(i) for i in r.beliefs]
    if r.episodes:
        out.append("Episodes:")
        out += [f"  [{i.tag}] {_day(i.when)[:10]} from {i.who} ({i.extra['kind']}): {i.text}" for i in r.episodes]
    return "\n".join(out)


def explain(r):
    """For `ask`: every recalled item with its scores and why it ranked."""
    out = [f"Question: {r.query}", f"As of {_day(r.at)}; closest anything came: {r.best:.3f}"]
    if r.missed:
        out.append("Nothing cleared the closeness floor: the turn would say \"I don't remember\".")
        return "\n".join(out)
    for title, items in (("Scratch pad", r.scratch), ("Beliefs", r.beliefs), ("Episodes", r.episodes)):
        if not items:
            continue
        out.append(f"\n{title}:")
        for i in items:
            why = f"score {i.score:.3f} = closeness {i.closeness:.3f} + recency {i.recency:.3f}"
            if i.kind == "belief":
                why += f" + certainty {i.certainty:.2f} (weighted)"
                out.append(f"  {why}\n  " + belief_line(i, links_shown=6))
            elif i.kind == "entry":
                out.append(f"  {why}\n  [{i.tag}] {_day(i.when)} {i.who} ({i.extra['channel']}): {i.text}")
            else:
                out.append(f"  {why}\n  [{i.tag}] {_day(i.when)} from {i.who} ({i.extra['kind']}, {i.extra['channel']},"
                           f" entries {i.extra['entries']}): {i.text}")
    return "\n".join(out)
