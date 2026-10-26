"""Whiteboard (agent sessions design item 13): a living page per work
session that the hub keeps from the agent's own calls, so the state of the
work survives a lost context without anyone writing a handoff.

A board belongs to a claim: one actor working one task, from claim to
release. The hub writes it as the work happens, from what it links to the
claim anyway:

* every band call (``rook_call``): its why, the worker, the cap and whether it
  worked. Calls with the same why on the same worker, close together, are one
  line with a count;
* consoles opened and closed (with the close summary), handoffs saved,
  secrets used (by vault name), commits and pull requests found in calls.

The agent adds what calls can't show, with ``rook_task(action="jot")``: the
session's ``goal``, a ``decision``, an ``open`` item, ``done`` (closes an open
item) and a ``note``.

``rook_task(action="whiteboard")`` reads it. A compact board rides the session
brief (rook/band_mcp/brief.py) for each claimed task, so an agent that lost
its context gets its working state back with the brief. When a session ends,
or a claim is released without a handoff, a board with work since the last
handoff is saved as that handoff (:func:`as_handoff`).

Calls go to the claim of the MCP session that made them (:data:`SESSION`),
not to the actor's newest claim: several agents often share one token.
"""
from __future__ import annotations

import time
from collections import Counter
from contextvars import ContextVar

#: The MCP session the current call comes from: a dict the bridge keeps per
#: session, holding ``task`` once that session claims one. None outside an MCP call.
SESSION: ContextVar[dict | None] = ContextVar('rook_whiteboard_session', default=None)

#: Auto-link kinds and the board line each becomes.
AUTO = {'journal': 'call', 'console': 'console', 'handoff': 'handoff', 'secret': 'secret',
        'commit': 'commit', 'url': 'link', 'session': 'session'}
#: What an agent can jot.
JOTS = ('goal', 'decision', 'open', 'done', 'note')
WORK = ('call', 'console', 'commit', 'link', 'secret', 'session', 'handoff')
MAX_TEXT = 500
MAX_JOTS = 10        # per kind in one jot
MAX_ROWS = 400       # work lines kept per claim (the oldest go); jotted lines always stay
COLLAPSE = 5         # a call joins a matching line among the claim's last few work lines
WORK_SHOWN = 30      # work lines in a read, newest last; data.limit for more
BRIEF_WORK = 5
BRIEF_LIST = 8
BRIEF_TEXT = 200


def session_task() -> str | None:
    s = SESSION.get()
    return s.get('task') if s else None


def bind(task: str | None, *, unless: str | None = None) -> None:
    """Point the current MCP session at ``task`` (a claim) or at nothing.
    ``unless``: only clear when the session is on that task."""
    s = SESSION.get()
    if s is None:
        return
    if unless is not None and s.get('task') != unless:
        return
    s['task'] = task


def _clip(value, limit=MAX_TEXT) -> str:
    value = ' '.join(str(value or '').split())
    return value if len(value) <= limit else value[:limit - 1] + '…'


def add(db, claim, kind, text, *, worker=None, cap=None, ref=None, ok=None, now=None) -> int | None:
    """One line on ``claim``'s board (a claims row). Returns its seq."""
    text = _clip(text)
    if claim is None or not text:
        return None
    now = now or time.time()
    if kind == 'call':
        for row in db.execute("SELECT * FROM whiteboard WHERE claim=? AND kind IN ({}) ORDER BY seq DESC "
                              "LIMIT ?".format(','.join('?' * len(WORK))),
                              (claim['id'], *WORK, COLLAPSE)).fetchall():
            if row['kind'] == 'call' and row['text'] == text and row['worker'] == worker:
                caps = (row['cap'] or '').split(', ') if row['cap'] else []
                if cap and cap not in caps:
                    caps.append(cap)
                db.execute('UPDATE whiteboard SET n=n+1, ts=?, ref=?, ok=?, cap=? WHERE seq=?',
                           (now, ref, None if ok is None else int(bool(ok)),
                            _clip(', '.join(caps), 200) or None, row['seq']))
                return row['seq']
    cur = db.execute('INSERT INTO whiteboard(band,task,claim,actor,kind,text,worker,cap,ref,ok,first,ts) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (claim['band'], claim['task'], claim['id'], claim['actor'], kind, text, worker,
                      cap, None if ref is None else str(ref)[:500], None if ok is None else int(bool(ok)),
                      now, now))
    if kind in WORK:
        marks = ','.join('?' * len(WORK))
        over = db.execute(f'SELECT count(*) FROM whiteboard WHERE claim=? AND kind IN ({marks})',
                          (claim['id'], *WORK)).fetchone()[0] - MAX_ROWS
        if over > 0:
            db.execute(f'DELETE FROM whiteboard WHERE seq IN (SELECT seq FROM whiteboard WHERE claim=? '
                       f'AND kind IN ({marks}) ORDER BY seq LIMIT ?)', (claim['id'], *WORK, over))
    return cur.lastrowid


def from_link(db, claim, kind, ref, note, board=None) -> int | None:
    """The board line for an auto-link the hub just made on ``claim``'s task.
    ``board`` carries what the link can't: {text, worker, cap, ok}."""
    board = board or {}
    line = AUTO.get(kind)
    if line is None:
        return None
    text = board.get('text') or note or ref
    if line == 'secret':
        text = f'used secret {ref}' + (f' ({note})' if note else '')
    return add(db, claim, line, text, worker=board.get('worker'), cap=board.get('cap'),
               ref=ref, ok=board.get('ok'))


def jot(db, claim, data, now=None) -> dict:
    """Agent-written lines: data {goal, decision, open, done, note}, each a
    string or a list of them. ``done`` closes the open item with that seq or
    text; anything else is recorded as done."""
    now = now or time.time()
    added, closed = [], []
    given = False
    for kind in JOTS:
        values = data.get(kind)
        if values is None:
            continue
        values = values if isinstance(values, list) else [values]
        if len(values) > MAX_JOTS:
            raise ValueError(f'at most {MAX_JOTS} {kind} lines at once')
        for v in values:
            if not isinstance(v, (str, int)) or isinstance(v, bool) or not str(v).strip():
                raise ValueError(f'{kind} takes text (or, for done, an open item seq)')
            given = True
            if kind == 'done':
                seq = _open_item(db, claim, v)
                if seq is not None:
                    db.execute('UPDATE whiteboard SET closed=? WHERE seq=?', (now, seq))
                    closed.append(seq)
                    continue
                if isinstance(v, int) or str(v).strip().isdigit():
                    raise ValueError(f'no open item {v} on this whiteboard')
            added.append(add(db, claim, kind, v, now=now))
    if not given:
        raise ValueError('jot needs data with at least one of: ' + ', '.join(JOTS))
    return {'added': added, 'closed': closed}


def _open_item(db, claim, ref):
    rows = db.execute("SELECT seq, text FROM whiteboard WHERE claim=? AND kind='open' AND closed IS NULL",
                      (claim['id'],)).fetchall()
    s = str(ref).strip()
    for r in rows:
        if (s.isdigit() and r['seq'] == int(s)) or r['text'].lower() == _clip(s).lower():
            return r['seq']
    return None


def view(db, claim, limit=WORK_SHOWN) -> dict:
    """``claim``'s board, rendered: goal, decisions, open items, work done
    (newest last), machines touched."""
    rows = db.execute('SELECT * FROM whiteboard WHERE claim=? ORDER BY first, seq', (claim['id'],)).fetchall()
    out = {'claim': claim['id'], 'actor': claim['actor'], 'since': claim['started'],
           **({'released': claim['released']} if claim['released'] else {})}
    goals = [r for r in rows if r['kind'] == 'goal']
    if goals:
        out['goal'] = goals[-1]['text']
    for key, kind in (('decisions', 'decision'), ('notes', 'note')):
        items = [{'seq': r['seq'], 'text': r['text'], 'ts': r['ts']} for r in rows if r['kind'] == kind]
        if items:
            out[key] = items
    still = [{'seq': r['seq'], 'text': r['text']} for r in rows if r['kind'] == 'open' and not r['closed']]
    if still:
        out['open'] = still
    done = [{'seq': r['seq'], 'text': r['text'], 'ts': r['closed'] or r['ts']} for r in rows
            if (r['kind'] == 'open' and r['closed']) or r['kind'] == 'done']
    if done:
        out['done'] = done
    work = [r for r in rows if r['kind'] in WORK]
    if work:
        machines = Counter()
        for r in work:
            if r['worker']:
                machines[r['worker']] += r['n']
        limit = max(1, min(int(limit or WORK_SHOWN), MAX_ROWS))
        if len(work) > limit:
            out['earlier'] = len(work) - limit
        out['work'] = [_work(r) for r in work[-limit:]]
        if machines:
            out['machines'] = dict(machines.most_common())
    if rows:
        out['updated'] = max(r['ts'] for r in rows)
    return out


def _work(r) -> dict:
    item = {'seq': r['seq'], 'ts': r['ts'], 'what': r['text']}
    if r['kind'] != 'call':
        item['kind'] = r['kind']
    for key in ('worker', 'cap', 'ref'):
        if r[key]:
            item[key] = r[key]
    if r['n'] > 1:
        item['n'] = r['n']
    if r['ok'] == 0:
        item['ok'] = False
    return item


def brief(board: dict) -> dict | None:
    """The compact board the session brief carries for a claimed task."""
    out = {}
    if board.get('goal'):
        out['goal'] = _clip(board['goal'], BRIEF_TEXT)
    for key in ('decisions', 'open'):
        if board.get(key):
            out[key] = [_clip(x['text'], BRIEF_TEXT) for x in board[key][-BRIEF_LIST:]]
    if board.get('work'):
        out['recent'] = [_line(w) for w in board['work'][-BRIEF_WORK:]]
    if board.get('machines'):
        out['machines'] = list(board['machines'])[:BRIEF_LIST]
    return out or None


def _line(w: dict) -> str:
    where = f" on {w['worker']}" if w.get('worker') else ''
    times = f" (x{w['n']})" if w.get('n') else ''
    failed = ' [failed]' if w.get('ok') is False else ''
    kind = f"{w['kind']}: " if w.get('kind') and w['kind'] != 'call' else ''
    return _clip(f"{kind}{w['what']}{where}{times}{failed}", BRIEF_TEXT)


def news_since(db, claim, since: float) -> bool:
    """Work or jots on ``claim``'s board after ``since`` (a handoff's time)."""
    return db.execute("SELECT 1 FROM whiteboard WHERE claim=? AND kind<>'handoff' AND ts>? LIMIT 1",
                      (claim['id'], since)).fetchone() is not None


def as_handoff(board: dict, task: dict, reason: str) -> dict:
    """A handoff written from ``board`` (``reason``: why the hub wrote it)."""
    work = [_line(w) for w in board.get('work', []) if w.get('kind') != 'handoff'][-8:]
    state = [f'Saved by the hub from the whiteboard ({reason}).']
    if work:
        state.append('Recent work: ' + '; '.join(work) + '.')
    if board.get('done'):
        state.append('Done: ' + '; '.join(d['text'] for d in board['done'][-5:]) + '.')
    if board.get('machines'):
        state.append('Machines: ' + ', '.join(board['machines']) + '.')
    ref = task.get('slug') or task['id']
    steps = [x['text'] for x in board.get('open', [])]
    steps.append(f'Full whiteboard: rook_task(action="whiteboard", id="{ref}")')
    artifacts = [f"console {w['ref']}" for w in board.get('work', []) if w.get('kind') == 'console' and w.get('ref')]
    artifacts += [f"{w['kind']} {w['ref']}" for w in board.get('work', [])
                  if w.get('kind') in ('commit', 'link') and w.get('ref')]
    return {'goal': board.get('goal') or task['title'], 'state': ' '.join(state),
            'decisions': [d['text'] for d in board.get('decisions', [])],
            'next_steps': steps, 'artifacts': list(dict.fromkeys(artifacts))[:20]}
