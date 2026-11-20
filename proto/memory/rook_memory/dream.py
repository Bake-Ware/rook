"""The dream: one more turn of the session, at compaction or when the session goes idle.

It runs as a fork of the live Claude Code session (`claude -p --resume ID --fork-session`, saving
nothing), so its own talk never lands in the real conversation and the whole context is in front of
it, with the memory search tool. A conversation taken in from an export has no live session; it is
replayed instead, one fresh call with the record in the prompt. Either way it is one call that does
two things, in order, and asks itself no invented questions:

1. Distil facts from the record since the last dream, each tied to the entries it comes from.
2. Novelty: each fact against what is held. Already known: a link that supports. Conflicts: a link
   against. Nothing matches: a new belief, unless a held belief already says the same (checked by
   closeness, then by one small call: same, opposite or different).

The record, not the model, says who said what. A belief about what Bake wants is made, or moved,
only by his own words: anything else that would do it is recorded and held back (see `held_back`).
Pinned beliefs get their links like any other, and do not move.
"""

from string import Template

from . import embed, mcp, model, schemas, search, standing
from .store import parse_time


def _when(t):
    return str(t)[:16].replace("T", " ")


def listing(entries):
    return "\n".join(f"[s{e['id']}] {_when(e['at'])} {e['speaker']} ({e['channel']}): {e['text']}" for e in entries)


def stretches(entries, chars):
    run, size = [], 0
    for e in entries:
        n = len(e["text"]) + 40
        if run and size + n > chars:
            yield run
            run, size = [], 0
        run.append(e)
        size += n
    if run:
        yield run


def due(store, knobs, session, at):
    """Idle long enough since the session's last entry, with something not yet dreamed over."""
    s = store.one("SELECT * FROM sessions WHERE id = ?", session)
    if not s or not s["last_at"] or not store.undreamed(session):
        return False
    idle = (parse_time(at) - parse_time(s["last_at"])).total_seconds() / 60
    return idle >= knobs["dream"]["idle_minutes"]


def run(store, knobs, session, trigger="hand", log=print):
    s = store.one("SELECT * FROM sessions WHERE id = ?", session)
    entries = store.undreamed(session)
    if not entries:
        log(f"{s['name']}: nothing new to dream over")
        return None
    when = entries[-1]["at"]
    mode = "fork" if knobs["dream"]["fork"] and s["cc_session"] else "replay"
    dream = store.start_dream(session, mode, trigger, entries[0]["id"], entries[-1]["id"])
    log(f"dream {dream} ({trigger}, {mode}) over {s['name']}: entries s{entries[0]['id']}-s{entries[-1]['id']}")
    chosen = knobs["model"]["dream_model"] or knobs["model"]["turn_model"]
    tools = mcp.config(store, knobs, "dream", session=session, at=when)
    facts = []
    runs = [entries] if mode == "fork" else list(stretches(entries, knobs["dream"]["replay_stretch_chars"]))
    for n, part in enumerate(runs, 1):
        body = Template(knobs["prompts"]["dream"]).safe_substitute(entries=listing(part), now=_when(when))
        try:
            if mode == "fork":
                reply = model.call(store, knobs, f"dream {dream}", body, model=chosen, schema=schemas.dream(knobs),
                                   resume=s["cc_session"], fork=True, persist=False, mcp=tools, tools=["search"])
            else:
                prompt = Template(knobs["prompts"]["replay"]).safe_substitute(name=s["name"], dream=body)
                reply = model.call(store, knobs, f"dream {dream} {n}/{len(runs)}", prompt, model=chosen,
                                   system=knobs["prompts"]["house"], schema=schemas.dream(knobs), persist=False,
                                   mcp=tools, tools=["search"])
        except model.ModelError as e:
            log(f"  no answer ({e}); these entries stay undreamed")
            store.finish_dream(dream, 0, 0, 0, [{"why": str(e)}], None)
            return None
        facts += reply["structured_output"].get("facts") or []
    result = apply(store, knobs, dream, session, entries, {"facts": facts}, when, log)
    last = entries[-1]["id"]
    if knobs["dream"]["condense_after_dream"]:
        store.run("UPDATE entries SET condensed_at = ? WHERE session = ? AND id <= ? AND condensed_at IS NULL",
                  when, session, last)
    store.run("UPDATE sessions SET dreamed_to = ? WHERE id = ?", last, session)
    return result


def _ids(tags, prefix):
    out = []
    for t in tags or []:
        t = str(t).strip().strip("[]").lower()
        if t.startswith(prefix) and t[1:].isdigit():
            out.append(int(t[1:]))
    return out


def apply(store, knobs, dream, session, entries, answer, when, log=print, emb=None):
    """Writes what the dream answered: episodes, links and new beliefs, with the record's rules.

    A new belief close to one already held (or to another new one) is checked first, in one call per
    dream: the same (the episode supports the held one instead), the opposite (a new belief, marked as
    its opposite) or different (a new belief)."""
    person = knobs["people"]["person"]
    abouts = [person, "how", "world"]
    mine = {e["id"]: e for e in entries}
    held_back, pending, made = [], [], []
    count = {"facts": 0, "links": 0, "new": 0, "same": 0, "opposite": 0}

    def link(b, ep, stance, weight, said_by, occ, why):
        w, note = weight, None
        if b["about"] == person and said_by != person:
            w, note = 0.0, f"not {person}'s own words: recorded, not counted"
        elif w <= 0:
            note = f"{said_by}'s words: recorded, not counted"
        elif store.pinned(b["id"]):
            note = "pinned: recorded, does not move it"
        elif not standing.strong(knobs, w):
            note = "weak: recorded, cannot flip it"
        store.add_link(b["id"], ep, stance, w, occ, dream, why=why, note=note, at=when)
        count["links"] += 1
        log(f"      {'+' if stance > 0 else '-'} b{b['id']} ({w:.2f}{'; ' + note if note else ''}) {b['text']}")

    def create(text, about, ep, weight, occ, opposite_of=None, wordings=None):
        bid = store.add_belief(text, about, dream, at=when, wordings=wordings)
        store.add_link(bid, ep, +1, weight, occ, dream, why="made from this", at=when)
        count["new"] += 1
        count["links"] += 1
        log(f"      new b{bid} [{about}] {text}")
        for o in opposite_of or []:
            if store.one("SELECT 1 FROM beliefs WHERE id = ?", o):
                store.mark_opposites(bid, o, when, f"dream {dream}")
                count["opposite"] += 1
                log(f"        marked the opposite of b{o}")
        return bid

    for f in answer.get("facts") or []:
        text = str(f.get("fact") or "").strip()
        srcs = [mine[i] for i in _ids(f.get("entries"), "s") if i in mine]
        if not text:
            continue
        if not srcs:
            held_back.append({"fact": text, "why": "names no entry of this dream's record"})
            continue
        # Whose words the fact rests on: the model names them, but only a speaker of the entries it cites
        # counts. A fact resting on the agent's answer to Bake's question is the agent's.
        speakers = [e["speaker"] for e in srcs]
        said_by = f.get("said_by") if f.get("said_by") in speakers else speakers[0]
        srcs = [e for e in srcs if e["speaker"] == said_by] or srcs
        channel = "voice" if any(e["channel"] == "voice" for e in srcs) else "chat"
        kind = f.get("kind") if f.get("kind") in KINDS else "mention"
        at = min(e["at"] for e in srcs)
        ep = store.add_episode(session, text, said_by, channel, kind, [e["id"] for e in srcs], at, dream, at=when)
        count["facts"] += 1
        weight = standing.strength(knobs, kind, said_by, channel)
        occ = standing.occasion(knobs, at, session)
        log(f"  e{ep} [{said_by}, {kind}, {channel}] {text}")
        for h in f.get("held") or []:
            b = _belief(store, h.get("belief"))
            if not b:
                held_back.append({"fact": text, "why": f"links to {h.get('belief')!r}, which is no belief"})
                continue
            link(b, ep, -1 if h.get("stance") == "against" else 1, weight, said_by, occ, h.get("why"))
        nb = f.get("new_belief")
        if not (isinstance(nb, dict) and str(nb.get("belief") or "").strip()):
            continue
        about = nb.get("about") if nb.get("about") in abouts else "world"
        if about == person and said_by != person:
            held_back.append({"fact": text, "belief": nb["belief"],
                              "why": f"about what {person} wants, but from {said_by}'s words"})
            log(f"      held back (from {said_by}, not {person}): {nb['belief']}")
            continue
        if weight <= 0:
            held_back.append({"fact": text, "belief": nb["belief"],
                              "why": f"{said_by}'s words count for nothing, so a belief on them would stand on nothing"})
            log(f"      held back (stands on nothing): {nb['belief']}")
            continue
        opp = [b["id"] for b in [_belief(store, nb.get("opposite_of"))] if b]
        others = [str(w).strip() for w in nb.get("other_wordings") or [] if str(w).strip()]
        pending.append({"text": nb["belief"].strip(), "about": about, "ep": ep, "weight": weight, "occ": occ,
                        "said_by": said_by, "opposite_of": opp, "wordings": others})

    # The same check: each new belief against the held ones (and the new ones before it) it is close to.
    pairs = _close_pairs(store, knobs, pending, emb)
    verdicts = _same(store, knobs, dream, pairs, log) if pairs else {}
    for n, p in enumerate(pending):
        mine_pairs = [(k, ref) for k, (i, ref) in enumerate(pairs) if i == n]
        same = [ref for k, ref in mine_pairs if verdicts.get(k) == "same"]
        opposite = [ref for k, ref in mine_pairs if verdicts.get(k) == "opposite"]
        resolve = lambda ref: ref[1] if ref[0] == "b" else made[ref[1]]
        if same:
            b = store.one("SELECT * FROM beliefs WHERE id = ?", resolve(same[0]))
            log(f"    same as b{b['id']}, so no new belief: {p['text']}")
            link(b, p["ep"], +1, p["weight"], p["said_by"], p["occ"], "says the same as the new belief the dream proposed")
            count["same"] += 1
            made.append(b["id"])
            continue
        made.append(create(p["text"], p["about"], p["ep"], p["weight"], p["occ"],
                           p["opposite_of"] + [resolve(r) for r in opposite], p["wordings"]))
    store.finish_dream(dream, count["facts"], count["links"], count["new"], held_back, answer)
    log(f"  dream {dream} done: {count['facts']} facts, {count['links']} links, {count['new']} new beliefs"
        f" ({count['same']} found already held, {count['opposite']} opposites marked), {len(held_back)} held back")
    return {"dream": dream, **count, "held_back": held_back}


KINDS = ("correction", "decision", "statement", "mention", "unclear")


def _belief(store, tag):
    if tag is None:
        return None
    ids = _ids([tag], "b")
    if ids:
        return store.one("SELECT * FROM beliefs WHERE id = ?", ids[0])
    return store.one("SELECT * FROM beliefs WHERE lower(text) = lower(?)", str(tag).strip())


def _close_pairs(store, knobs, pending, emb):
    """[(index of the new belief, ("b", held id) or ("n", index of an earlier new one))]."""
    if not pending:
        return []
    from .knobs import search as search_knobs
    held = store.all("SELECT * FROM beliefs ORDER BY id")
    texts = [p["text"] for p in pending]
    grid = search.pair(store, knobs, texts, held, emb=emb if emb is not None else "load") if held else [[] for _ in texts]
    floor = search_knobs(knobs)["same_check_from"]
    kind = search.kind(knobs)
    if kind == "keyword":
        among = [[search.dice(a, b) for b in texts] for a in texts]
    else:
        e = embed.embedder(store.folder, knobs) if emb in (None, "load") else emb
        tv = e(texts)
        among = [[float(tv[i] @ tv[j]) for j in range(len(texts))] for i in range(len(texts))]
    pairs = []
    for i, p in enumerate(pending):
        for j, b in enumerate(held):
            if b["id"] not in p["opposite_of"] and grid[i][j] >= floor:
                pairs.append((i, ("b", b["id"])))
        for j in range(i):
            if among[i][j] >= floor:
                pairs.append((i, ("n", j)))
    pairs.sort(key=lambda x: x[0])
    _close_pairs.texts = {("n", j): p["text"] for j, p in enumerate(pending)}
    _close_pairs.texts.update({("b", b["id"]): b["text"] for b in held})
    _close_pairs.new = texts
    return pairs


def _same(store, knobs, dream, pairs, log):
    lines = []
    for k, (i, ref) in enumerate(pairs):
        lines.append(f'{k}. new: "{_close_pairs.new[i]}"\n   held: "{_close_pairs.texts[ref]}"')
    prompt = Template(knobs["prompts"]["same"]).safe_substitute(pairs="\n".join(lines))
    chosen = knobs["model"]["same_model"] or knobs["model"]["dream_model"] or knobs["model"]["turn_model"]
    try:
        reply = model.call(store, knobs, f"dream {dream} same", prompt, model=chosen, schema=schemas.SAME, persist=False)
    except model.ModelError as e:
        log(f"    same check failed ({e}); the new beliefs are written as they are")
        return {}
    out = {}
    for v in reply["structured_output"].get("verdicts") or []:
        try:
            out[int(v.get("pair"))] = v.get("verdict")
        except (TypeError, ValueError):
            pass
    log(f"    same check: {len(pairs)} pair(s): " + ", ".join(f"{k}={out.get(k)}" for k in range(len(pairs))))
    return out
