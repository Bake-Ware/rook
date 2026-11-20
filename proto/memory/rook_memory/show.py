"""What is in memory, in a form a person can read."""

import json
import textwrap

from .ingest import short
from .store import now, parse_time


def _wrap(text, indent="    "):
    return textwrap.fill(" ".join(text.split()), width=110, initial_indent=indent, subsequent_indent=indent)


def _when(at):
    return at[:16].replace("T", " ")


def _ago(at, ref=None):
    hours = (parse_time(ref or now()) - parse_time(at)).total_seconds() / 3600
    return f"{hours:.0f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def sessions(store, out=print):
    out("== sessions taken in")
    for s in store.q("SELECT s.*, (SELECT COUNT(*) FROM scratch WHERE session = s.id) AS scratch,"
                     " (SELECT COUNT(*) FROM episodes WHERE session = s.id) AS episodes FROM sessions s ORDER BY s.id"):
        state = f"scratch condensed {_when(s['scratch_condensed_at'])}" if s["scratch_condensed_at"] else \
            f"{s['scratch']} scratch entries"
        out(f"  {s['id']}. {short(s['path'])}  {_when(s['first_at'] or '')} to {_when(s['last_at'] or '')}, "
            f"{s['lines_taken']} lines, {s['episodes']} episodes, {state}")
        out(f"     {s['path']}")


def episodes(store, out=print, session=None, limit=None):
    sql = "SELECT e.*, s.path FROM episodes e JOIN sessions s ON s.id = e.session"
    args = ()
    if session:
        sql += " WHERE e.session = ?"
        args = (session,)
    sql += " ORDER BY e.happened_at, e.id"
    rows = store.q(sql, *args)
    out(f"== episodes ({len(rows)})")
    for e in rows[:limit] if limit else rows:
        dreamed = f"dreamed in {e['dreamed_in']}" if e["dreamed_in"] else "not dreamed yet"
        out(f"  [e{e['id']}] {_when(e['happened_at'])}  importance {e['importance']}  "
            f"{short(e['path'])} L{e['first_line']}-{e['last_line']}  last read {_ago(e['last_read'])}  {dreamed}")
        out(_wrap(e["text"]))


def _support(store, belief):
    lines = []
    for s in store.support_of(belief):
        table = "episodes" if s["kind"] == "episode" else "beliefs"
        row = store.one(f"SELECT text FROM {table} WHERE id = ?", s["ref"])
        tag = ("e" if s["kind"] == "episode" else "b") + str(s["ref"])
        lines.append(f"      [{tag}] {' '.join((row['text'] if row else '(gone)').split())[:150]}")
    return lines


def beliefs(store, out=print):
    rows = store.q("SELECT * FROM beliefs ORDER BY id")
    standing = [b for b in rows if b["superseded_by"] is None]
    out(f"== beliefs ({len(standing)} standing, {len(rows) - len(standing)} superseded)")
    for b in rows:
        state = "" if b["superseded_by"] is None else \
            f"  SUPERSEDED by b{b['superseded_by']} ({b['how']}) {_when(b['superseded_at'])}"
        out(f"  [b{b['id']}] confidence {b['confidence']:.2f}  importance {b['importance']}  dream {b['dream']}{state}")
        out(_wrap(b["text"]))
        if b["question"]:
            out(_wrap(f"from the question: {b['question']}", "      "))
        out("    because:")
        for line in _support(store, b["id"]) or ["      (no support it named exists)"]:
            out(line)
    chains = store.q("SELECT o.id AS old, o.text AS old_text, o.confidence AS oc, o.how, n.id AS new, n.text AS new_text,"
                     " n.confidence AS nc FROM beliefs o JOIN beliefs n ON n.id = o.superseded_by ORDER BY o.id")
    out(f"== what superseded what ({len(chains)})")
    for c in chains:
        out(f"  b{c['old']} ({c['oc']:.2f}) --{c['how']}--> b{c['new']} ({c['nc']:.2f})")
        out(_wrap("was: " + c["old_text"]))
        out(_wrap("now: " + c["new_text"]))


def scratch(store, out=print, limit=None):
    rows = store.q("SELECT c.*, s.path FROM scratch c JOIN sessions s ON s.id = c.session ORDER BY c.session, c.line, c.id")
    out(f"== scratch pad ({len(rows)} entries, word for word)")
    for c in rows[-limit:] if limit else rows:
        out(f"  [s{c['id']}] {short(c['path'])} L{c['line']} {_when(c['at'])} {c['role']}: "
            f"{' '.join(c['text'].split())[:300]}")


def dreams(store, out=print):
    rows = store.q("SELECT * FROM dreams ORDER BY id")
    out(f"== dream passes ({len(rows)})")
    for d in rows:
        out(f"  dream {d['id']} ({d['trigger']}) {_when(d['started_at'])} to {_when(d['finished_at'] or '')}: "
            f"{d['episodes_in']} episodes in, {d['beliefs_made']} beliefs, {d['superseded']} superseded, "
            f"{d['scratch_condensed']} scratch entries condensed")
        for q in json.loads(d["questions"] or "[]"):
            out(_wrap("? " + q, "      "))


def calls(store, out=print):
    rows = store.q("SELECT * FROM calls ORDER BY id")
    out(f"== model calls ({len(rows)})")
    by = {}
    for c in rows:
        key = (c["purpose"].split()[0], c["model"])
        n, s, cost, tin, tout, bad = by.get(key, (0, 0.0, 0.0, 0, 0, 0))
        by[key] = (n + 1, s + (c["seconds"] or 0), cost + (c["cost_usd"] or 0), tin + (c["input_tokens"] or 0),
                   tout + (c["output_tokens"] or 0), bad + (0 if c["ok"] else 1))
    for (kind, mdl), (n, s, cost, tin, tout, bad) in sorted(by.items()):
        out(f"  {kind:9} {mdl:7} {n:4} calls  {s / 60:6.1f} min  ${cost:7.3f}  {tin:9} tokens in  {tout:7} out"
            f"  {bad} failed")
    for c in rows:
        if not c["ok"]:
            out(f"  failed: {c['purpose']}: {c['note']}")
