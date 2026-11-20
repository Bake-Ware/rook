"""The dream: one more turn of the session, at compaction or when the session goes idle.

It runs as a fork of the live Claude Code session (`claude -p --resume ID --fork-session`, saving
nothing), so its own talk never lands in the real conversation and the whole context is in front of
it, with the memory search tool. A conversation taken in from an export has no live session; it is
replayed instead, one fresh call with the record in the prompt. Either way it is one call that does
two things, in order, and asks itself no invented questions:

1. Distil facts from the record since the last dream, each tied to the entries it comes from.
2. Novelty: each fact against what is held. Already known: a link that supports. Conflicts: a link
   against. Nothing matches: a new belief.

The record, not the model, says who said what. A belief about what Bake wants is made, or moved,
only by his own words: anything else that would do it is recorded and held back (see `held_back`).
Pinned beliefs get their links like any other, and do not move.
"""

from string import Template

from . import mcp, model, schemas, standing
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


def apply(store, knobs, dream, session, entries, answer, when, log=print):
    """Writes what the dream answered: episodes, links and new beliefs, with the record's rules."""
    person = knobs["people"]["person"]
    abouts = [person, "world", *knobs["people"]["others"]]
    mine = {e["id"]: e for e in entries}
    held_back, n_facts, n_links, n_new = [], 0, 0, 0
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
        kind = f.get("kind") if f.get("kind") in ("correction", "statement", "mention", "unclear") else "mention"
        at = min(e["at"] for e in srcs)
        ep = store.add_episode(session, text, said_by, channel, kind, [e["id"] for e in srcs], at, dream, at=when)
        n_facts += 1
        weight = standing.strength(knobs, kind, said_by, channel)
        occ = standing.occasion(knobs, at, session)
        log(f"  e{ep} [{said_by}, {kind}, {channel}] {text}")
        for h in f.get("held") or []:
            ids = _ids([h.get("belief")], "b")
            b = store.one("SELECT * FROM beliefs WHERE id = ?", ids[0]) if ids else \
                store.one("SELECT * FROM beliefs WHERE lower(text) = lower(?)", str(h.get("belief") or "").strip())
            if not b:
                held_back.append({"fact": text, "why": f"links to {h.get('belief')!r}, which is no belief"})
                continue
            stance = -1 if h.get("stance") == "against" else 1
            w, note = weight, None
            if b["about"] == person and said_by != person:
                w, note = 0.0, f"not {person}'s own words: recorded, not counted"
            elif w <= 0:
                note = f"{said_by}'s words: recorded, not counted"
            elif store.pinned(b["id"]):
                note = "pinned: recorded, does not move it"
            store.add_link(b["id"], ep, stance, w, occ, dream, why=h.get("why"), note=note, at=when)
            n_links += 1
            log(f"      {'+' if stance > 0 else '-'} b{b['id']} ({w:.2f}{'; ' + note if note else ''}) {b['text']}")
        nb = f.get("new_belief")
        if isinstance(nb, dict) and str(nb.get("belief") or "").strip():
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
            bid = store.add_belief(nb["belief"].strip(), about, dream, at=when)
            store.add_link(bid, ep, +1, weight, occ, dream, why="made from this", at=when)
            n_new += 1
            n_links += 1
            log(f"      new b{bid} [{about}] {nb['belief'].strip()}")
    store.finish_dream(dream, n_facts, n_links, n_new, held_back, answer)
    log(f"  dream {dream} done: {n_facts} facts, {n_links} links, {n_new} new beliefs, {len(held_back)} held back")
    return {"dream": dream, "facts": n_facts, "links": n_links, "new": n_new, "held_back": held_back}
