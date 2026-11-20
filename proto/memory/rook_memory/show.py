"""What is in memory, in a form a person can read; and one belief's whole history."""

import json
import textwrap

from . import standing


def _wrap(text, indent="     "):
    return textwrap.fill(" ".join(str(text).split()), width=110, initial_indent=indent, subsequent_indent=indent)


def _when(at):
    return str(at or "")[:16].replace("T", " ")


def sessions(store, out=print):
    out("== sessions")
    for s in store.all("SELECT s.*, (SELECT COUNT(*) FROM entries WHERE session = s.id) AS n,"
                       " (SELECT COUNT(*) FROM entries WHERE session = s.id AND condensed_at IS NULL) AS scratch,"
                       " (SELECT COUNT(*) FROM episodes WHERE session = s.id) AS eps FROM sessions s ORDER BY s.started_at, s.id"):
        live = f", Claude Code session {s['cc_session'][:8]}" if s["cc_session"] else ""
        out(f"  {s['id']}. {s['name']}{' (made up)' if s['made_up'] else ''}  {_when(s['started_at'])} to {_when(s['last_at'])}:"
            f" {s['n']} entries ({s['scratch']} still in the scratch pad), {s['eps']} episodes{live}")


def entries(store, out=print, session=None, scratch_only=False):
    sql, args = "SELECT * FROM entries WHERE 1", []
    if session:
        sql += " AND session = ?"
        args.append(session)
    if scratch_only:
        sql += " AND condensed_at IS NULL"
    rows = store.all(sql + " ORDER BY id", *args)
    out(f"== {'scratch pad' if scratch_only else 'record'} ({len(rows)} entries)")
    for e in rows:
        state = "" if e["condensed_at"] is None else "  (dreamed over)"
        out(f"  [s{e['id']}] {_when(e['at'])} {e['speaker']} ({e['channel']}){state}")
        out(_wrap(e["text"]))


def episodes(store, out=print, session=None):
    sql, args = "SELECT * FROM episodes", []
    if session:
        sql += " WHERE session = ?"
        args.append(session)
    rows = store.all(sql + " ORDER BY happened_at, id", *args)
    out(f"== episodes ({len(rows)})")
    for e in rows:
        used = f", last used {_when(e['last_used'])}" if e["last_used"] else ""
        made = f"dream {e['dream']}" if e["dream"] else "a pin"
        out(f"  [e{e['id']}] {_when(e['happened_at'])} from {e['said_by']} ({e['kind']}, {e['channel']}),"
            f" entries {e['entries']}, by {made}{used}")
        out(_wrap(e["text"]))


def beliefs(store, knobs, out=print):
    rows = store.all("SELECT * FROM beliefs ORDER BY id")
    out(f"== beliefs ({len(rows)})")
    for b in rows:
        st = standing.of(store, knobs, b["id"])
        out(f"  [b{b['id']}] about {b['about']}: {standing.summary(st)}")
        out(_wrap(b["text"]))
        for l in store.links(b["id"]):
            out(f"       {'+' if l['stance'] > 0 else '-'} {_when(l['at'])[:10]} {l['said_by']} {l['kind']} {l['strength']:.2f}"
                f" [e{l['episode']}]" + (f" ({l['note']})" if l["note"] else ""))


def history(store, knobs, belief, out=print):
    b = store.one("SELECT * FROM beliefs WHERE id = ?", belief)
    if not b:
        out(f"no belief b{belief}")
        return
    made = f"dream {b['dream']}" if b["dream"] else "a pin"
    out(f"[b{b['id']}] about {b['about']}, made {_when(b['made_at'])} by {made}")
    out(_wrap(b["text"], "  "))
    events = [("link", l["at"], l["id"], l) for l in store.links(belief)]
    events += [("pin", p["at"], p["id"], p) for p in store.pins(belief)]
    events.sort(key=lambda e: (str(e[1]), e[0] == "pin", e[2]))
    out("\nIn the order it happened, with the standing after each:")
    for kind, at, _, row in events:
        if kind == "pin":
            out(f"  {_when(at)}  {row['action'].upper()} from his message s{row['entry']}: \"{row['words']}\"")
        else:
            mark = "supports" if row["stance"] > 0 else "AGAINST "
            out(f"  {_when(at)}  {mark} {row['strength']:.2f}  {row['said_by']}, {row['kind']}, {row['channel']}"
                f" [e{row['episode']}, dream {row['dream'] or '-'}]" + (f"  ({row['note']})" if row["note"] else ""))
            out(_wrap(row["episode_text"], "        "))
            if row["why"]:
                out(_wrap("why: " + row["why"], "        "))
        st = standing.of(store, knobs, belief, upto=at)
        out(f"        -> {standing.summary(st)}")
    out(f"\nNow: {standing.summary(standing.of(store, knobs, belief))}")
    uses = store.all("SELECT * FROM uses WHERE kind = 'belief' AND ref = ? ORDER BY id", belief)
    if uses:
        out(f"Used by real turns {len(uses)} times, last {_when(uses[-1]['at'])}.")


def dreams(store, out=print):
    out("== dreams")
    for d in store.all("SELECT d.*, s.name FROM dreams d JOIN sessions s ON s.id = d.session ORDER BY d.id"):
        calls = store.one("SELECT COUNT(*) AS n, SUM(seconds) AS s, SUM(cost_usd) AS c, SUM(turns) AS t FROM calls"
                          " WHERE purpose = ? OR purpose LIKE ?", f"dream {d['id']}", f"dream {d['id']} %")
        out(f"  dream {d['id']} ({d['trigger']}, {d['mode']}) over {d['name']}: entries s{d['entries_from']}-s{d['entries_to']},"
            f" {d['facts']} facts, {d['links']} links, {d['new_beliefs']} new beliefs;"
            f" {calls['n']} call(s), {calls['t'] or 0} model turns, {calls['s'] or 0:.0f} s, ${calls['c'] or 0:.3f}")
        for h in json.loads(d["held_back"] or "[]"):
            out(_wrap(f"held back: {h.get('belief') or h.get('fact')} ({h['why']})", "     "))


def calls(store, out=print):
    out("== model calls")
    for r in store.all("SELECT CASE WHEN purpose LIKE 'dream%' THEN 'dream' ELSE 'turn' END AS what, model,"
                       " COUNT(*) AS n, SUM(seconds) AS s, SUM(cost_usd) AS c, SUM(turns) AS t, SUM(ok) AS ok"
                       " FROM calls GROUP BY what, model"):
        out(f"  {r['what']:5} {r['model']:8} {r['n']} calls ({r['ok']} ok), {r['t'] or 0} model turns inside,"
            f" {r['s'] or 0:.0f} s, ${r['c'] or 0:.3f}")
    for r in store.all("SELECT * FROM calls WHERE ok = 0"):
        out(f"  failed: {r['purpose']}: {r['note']}")


def tools(store, out=print):
    out("== memory tool calls")
    for t in store.all("SELECT * FROM tool_log ORDER BY id"):
        out(f"  {t['role']} s{t['entry'] or '-'} {t['tool']} {t['args']}")
        out(_wrap(t["answer"].splitlines()[0] if t["answer"] else "", "       "))
