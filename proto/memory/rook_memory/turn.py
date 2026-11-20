"""Live turns: a message comes in, memory is recalled for it, the house agent answers.

Each live session is a real Claude Code session (`claude -p`, then `--resume` for every later turn),
with the memory tools (search, pin, unpin) served to it. Per turn:
1. if the session has been idle long enough, it dreams first, and then compacts (`/compact`), so
   the same session goes on from a summary (the other trigger is compaction itself: the `hook`
   command, for Claude Code's PreCompact hook);
2. the message goes into the record, stamped with who sent it;
3. recall runs on it and the closest memories, with their ids, go in front of the agent;
4. what recall handed over counts as used, if the message is Bake's (a real turn with a person);
5. the agent's reply goes into the record as "house".

`play` runs the made-up days through this: by default as one house session that dreams and compacts
between days (knob play.one_session), else one fresh session per day with a dream at its end. The
house agent's own lines in the file are not sent; the live agent answers for itself.
"""

import datetime
import json
import pathlib
import re

from . import dream, importer, mcp, model, recall
from .store import parse_time, stamp


def session(store, name):
    return store.session(name, "live", name)


def say(store, knobs, sess, text, speaker, at, channel="chat", log=print):
    if dream.due(store, knobs, sess["id"], at):
        dream.run(store, knobs, sess["id"], trigger="idle", log=log)
        if knobs["dream"]["compact_after_idle_dream"]:
            compact(store, knobs, sess["id"], log=log)
    at = stamp(at)
    entry = store.add_entry(sess["id"], at, speaker, channel, text)
    with_memory = knobs["play"]["memory_in_turns"]
    r = recall.recall(store, knobs, text, at, exclude_entries=[entry]) if with_memory else None
    if r and speaker == knobs["people"]["person"]:
        store.use(at, [(i.kind, i.id) for i in r.items()], sess["id"], entry, how="recall")
    # Every turn's recall is kept; a turn whose recall turned up no belief is a possible miss for the dream.
    store.run("INSERT INTO turns (session, entry, at, recalled) VALUES (?, ?, ?, ?)", sess["id"], entry, at,
              json.dumps([i.tag for i in r.items()] if r else None))
    if r is not None and not r.beliefs:
        store.run("INSERT INTO misses (session, entry, at, query, recalled, found) VALUES (?, ?, ?, ?, '[]', '[]')",
                  sess["id"], entry, at, text)
    head = f"[{at[:16].replace('T', ' ')}, {speaker}{', voice' if channel == 'voice' else ''}]"
    prompt = f"{head} {text}" + (f"\n\n{recall.for_turn(r)}" if r else "")
    sess = store.one("SELECT * FROM sessions WHERE id = ?", sess["id"])
    cc = sess["cc_session"]
    try:
        reply = model.call(store, knobs, f"turn {sess['name']}", prompt, model=knobs["model"]["turn_model"],
                           system=knobs["prompts"]["house"], new_session=not cc, resume=cc,
                           mcp=mcp.config(store, knobs, "turn", session=sess["id"], entry=entry, at=at) if with_memory else None,
                           tools=["search", "pin", "unpin"] if with_memory else ())
    except model.ModelError as e:
        log(f"  (no reply: {e})")
        return None, r
    if not cc:
        store.run("UPDATE sessions SET cc_session = ? WHERE id = ?", reply["session_id"], sess["id"])
    answer = str(reply.get("result") or "").strip()
    later = (parse_time(at) + datetime.timedelta(seconds=20)).isoformat()
    reply_entry = store.add_entry(sess["id"], later, "house", "chat", answer)
    store.run("UPDATE turns SET reply = ? WHERE entry = ?", reply_entry, entry)
    return answer, r


def session_file(store, cc):
    folder = re.sub(r"[^A-Za-z0-9]", "-", str((store.folder / "house").resolve()))
    return pathlib.Path.home() / ".claude" / "projects" / folder / f"{cc}.jsonl"


def compact(store, knobs, session_id, log=print):
    """Compacts the live session (`/compact` through `claude -p --resume`) and keeps its summary."""
    s = store.one("SELECT * FROM sessions WHERE id = ?", session_id)
    if not s["cc_session"]:
        return None
    try:
        model.call(store, knobs, f"compact {s['name']}", "/compact", model=knobs["model"]["turn_model"],
                   resume=s["cc_session"])
    except model.ModelError as e:
        log(f"  compaction failed: {e}")
        return None
    summary = None
    f = session_file(store, s["cc_session"])
    if f.exists():
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("isCompactSummary"):
                content = (rec.get("message") or {}).get("content")
                summary = content if isinstance(content, str) else "\n".join(
                    c.get("text", "") for c in content or [] if isinstance(c, dict))
    store.run("INSERT INTO compactions (session, at, summary) VALUES (?, ?, ?)", session_id, s["last_at"], summary)
    log(f"  compacted ({len(summary or '')} characters of summary)")
    return summary


def play(store, knobs, path, convs, dream_after=True, log=print):
    source = "play:" + str(path)
    one = knobs["play"]["one_session"]
    for c in convs:
        if one:
            s = store.session("house (made up, one session)", source, "one-session", made_up=True)
            if store.one("SELECT COUNT(*) AS n FROM entries WHERE session = ? AND at >= ?", s["id"],
                         stamp(c["messages"][0]["at"]))["n"]:
                log(f"{c['name']}: played before, skipped")
                continue
        else:
            s = store.session(c["name"], source, c["id"], c["made_up"])
            if store.one("SELECT COUNT(*) AS n FROM entries WHERE session = ?", s["id"])["n"]:
                log(f"{c['name']}: played before, skipped")
                continue
        log(f"\n=== {c['name']}")
        for m in c["messages"]:
            if m["house"]:
                continue
            log(f"[{str(m['at'])[:16]}] {m['speaker']}{' (voice)' if m['channel'] == 'voice' else ''}: {m['text']}")
            answer, r = say(store, knobs, s, m["text"], m["speaker"], m["at"], m["channel"], log=log)
            pins = store.all("SELECT p.*, b.text FROM pins p JOIN beliefs b ON b.id = p.belief WHERE p.entry ="
                             " (SELECT MAX(id) FROM entries WHERE session = ? AND speaker != 'house')", s["id"])
            if r is not None:
                log(f"    recalled: {', '.join(i.tag for i in r.items()) or 'nothing (I do not remember)'}")
            for p in pins:
                log(f"    {p['action'].upper()} b{p['belief']}: {p['text']}")
            log(f"  agent: {answer}")
        if dream_after and not one:
            dream.run(store, knobs, s["id"], trigger="idle", log=log)
    if dream_after and one and convs:
        s = store.session("house (made up, one session)", source, "one-session", made_up=True)
        dream.run(store, knobs, s["id"], trigger="idle", log=log)


def made_up(path, knobs, picks=()):
    return importer.pick(importer.conversations(path, knobs), picks)
