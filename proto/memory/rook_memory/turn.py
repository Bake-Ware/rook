"""Live turns: a message comes in, memory is recalled for it, the house agent answers.

Each live session is a real Claude Code session (`claude -p`, then `--resume` for every later turn),
with the memory tools (search, pin, unpin) served to it. Per turn:
1. if the session has been idle long enough, it dreams first (one trigger of two; the other is
   compaction, for which there is the `hook` command);
2. the message goes into the record, stamped with who sent it;
3. recall runs on it and the closest memories, with their ids, go in front of the agent;
4. what recall handed over counts as used, if the message is Bake's (a real turn with a person);
5. the agent's reply goes into the record as "house".

`play` runs the made-up conversations through this, one live session each, and dreams at the end of
each one (the session going idle). The house agent's own lines in the file are not sent; the live
agent answers for itself.
"""

import datetime

from . import dream, importer, mcp, model, recall
from .store import parse_time, stamp


def session(store, name):
    return store.session(name, "live", name)


def say(store, knobs, sess, text, speaker, at, channel="chat", log=print):
    if dream.due(store, knobs, sess["id"], at):
        dream.run(store, knobs, sess["id"], trigger="idle", log=log)
    at = stamp(at)
    entry = store.add_entry(sess["id"], at, speaker, channel, text)
    r = recall.recall(store, knobs, text, at, exclude_entries=[entry])
    if speaker == knobs["people"]["person"]:
        store.use(at, [(i.kind, i.id) for i in r.items()], sess["id"], entry, how="recall")
    head = f"[{at[:16].replace('T', ' ')}, {speaker}{', voice' if channel == 'voice' else ''}]"
    prompt = f"{head} {text}\n\n{recall.for_turn(r)}"
    sess = store.one("SELECT * FROM sessions WHERE id = ?", sess["id"])
    cc = sess["cc_session"]
    try:
        reply = model.call(store, knobs, f"turn {sess['name']}", prompt, model=knobs["model"]["turn_model"],
                           system=knobs["prompts"]["house"], new_session=not cc, resume=cc,
                           mcp=mcp.config(store, knobs, "turn", session=sess["id"], entry=entry, at=at),
                           tools=["search", "pin", "unpin"])
    except model.ModelError as e:
        log(f"  (no reply: {e})")
        return None, r
    if not cc:
        store.run("UPDATE sessions SET cc_session = ? WHERE id = ?", reply["session_id"], sess["id"])
    answer = str(reply.get("result") or "").strip()
    later = (parse_time(at) + datetime.timedelta(seconds=20)).isoformat()
    store.add_entry(sess["id"], later, "house", "chat", answer)
    return answer, r


def play(store, knobs, path, convs, dream_after=True, log=print):
    source = "play:" + str(path)
    for c in convs:
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
            recalled = ", ".join(i.tag for i in r.items()) or "nothing (I don't remember)"
            log(f"    recalled: {recalled}")
            for p in pins:
                log(f"    {p['action'].upper()} b{p['belief']}: {p['text']}")
            log(f"  agent: {answer}")
        if dream_after:
            dream.run(store, knobs, s["id"], trigger="idle", log=log)


def made_up(path, knobs, picks=()):
    return importer.pick(importer.conversations(path, knobs), picks)
