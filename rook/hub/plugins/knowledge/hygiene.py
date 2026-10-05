"""Hygiene triggers: keep tasks and knowledge current as work happens.

Agents forget to close the loop: a commit lands but the task stays
``in_progress``, a task goes ``done`` and nothing is written down, a session
ends with a claim open, a project's last task finishes and the project stays
``active``. This engine turns those moments into **findings**: small,
deduplicated records (table ``hygiene``) that say what to do next. See
docs/design/hygiene.md.

Delivery. A finding addressed to an actor rides that actor's next MCP reply as
``_hygiene`` (:meth:`HygieneEngine.take`, called by the bridge), once (a few
kinds repeat after ``hygiene_renotify_hours``). Every open finding also shows on
the deck (``hygiene`` on the row) and in ``rook_task(action="hygiene")``, so
the next agent or person sees it even if the addressee never comes back.

Safety. The engine only adds rows to its own table, events on records,
automatic links (a commit to the claimed task its message names, or the
caller's claim) and the claim's ``dirty`` mark the deck already shows. It never changes a record's
state, title, body or attrs, and never releases a claim: state changes are
proposals in the finding's text. Every hook is bookkeeping: callers wrap it so
a failure here never fails the write or call that triggered it.

Triggers:

* ``work_signal`` (event): a commit or PR is linked to an open task (by hand,
  or detected in a ``rook_call`` reply: ``git commit`` output, the URL ``gh pr create``
  prints), or a console linked to it is closed. Commits are auto-linked first.
* ``handoff_saved`` (event): a handoff lands on a task that is still
  ``in_progress``: stopping? release it or set the state.
* ``idle_claim`` (scan): claimed, in progress, idle past
  ``hygiene_idle_minutes`` with work since the last handoff. Past
  ``hygiene_dirty_hours`` the claim is also marked dirty (deck
  ``needs_hygiene``).
* ``release_proposed`` (scan): idle past ``hygiene_release_hours``: propose
  releasing the claim with a handoff (anyone may, per STALE_CLAIM_SECS).
* ``session_ended`` (event): an MCP session closed (DELETE) while its actor
  held an in-progress claim with work since the last handoff, and no other
  session of that actor is open. The claim is marked dirty.
* ``done_without_knowledge`` (event + scan): a task went ``done`` and no
  knowledge page links it, mentions it, or was written by its claimants while
  they worked on it. Suggests existing pages from search.
* ``project_complete`` (scan): an active project whose tasks are all finished
  and quiet for ``hygiene_project_idle_hours``: propose closing it.
* ``stale_knowledge`` (scan): an active page that mentions or links a
  superseded page, or links a cancelled task.
"""
from __future__ import annotations

import json
import logging
import re
import shlex
import time
import uuid

from .store import STALE_CLAIM_SECS, packed

log = logging.getLogger("rook.hub.plugins.knowledge.hygiene")

SYSTEM = {'id': 'system:hygiene', 'kind': 'system', 'label': 'Rook hygiene'}

#: Setting name (on the knowledge plugin) -> default. Kept here so the engine
#: works without a plugin (tests, tools) and the plugin declares the same keys.
DEFAULTS = {
    'hygiene_enabled': True,
    'hygiene_idle_minutes': 30,
    'hygiene_dirty_hours': 4.0,
    'hygiene_release_hours': 24.0,
    'hygiene_renotify_hours': 6.0,
    'hygiene_signal_hours': 24.0,
    'hygiene_done_window_days': 3.0,
    'hygiene_project_idle_hours': 24.0,
    'hygiene_hints_per_reply': 1,
    'hygiene_scan_seconds': 300,
    'hygiene_notify_people': False,
}

# Per kind: ``once`` = never raised again for the same (record, actor) after it
# resolves; ``repeat`` = re-delivered every renotify period while open;
# ``scan`` = the periodic scan owns it (resolves it when the condition stops).
KINDS = {
    'work_signal': {'once': False, 'repeat': False, 'scan': False},
    'handoff_saved': {'once': False, 'repeat': False, 'scan': False},
    'idle_claim': {'once': False, 'repeat': True, 'scan': True},
    'release_proposed': {'once': False, 'repeat': True, 'scan': True},
    'session_ended': {'once': False, 'repeat': False, 'scan': False},
    'done_without_knowledge': {'once': True, 'repeat': False, 'scan': True},
    'project_complete': {'once': False, 'repeat': True, 'scan': True},
    'stale_knowledge': {'once': False, 'repeat': False, 'scan': True},
}
#: Kinds about the claimant's current work: finishing or stopping it resolves them.
CLAIM_KINDS = ('work_signal', 'handoff_saved', 'idle_claim', 'release_proposed', 'session_ended')
#: Kinds worth a notify.send to people when ``hygiene_notify_people`` is on.
NOTIFY_KINDS = ('release_proposed', 'project_complete')
FINISHED = ('done', 'closed', 'cancelled', 'archived')

# ``[main 1a2b3c4] subject`` / ``[main (root-commit) 1a2b3c4] subject``
COMMIT_LINE = re.compile(r'^\[([^\s\]]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] ?(.*)$', re.M)
PR_URL = re.compile(r'https://github\.com/[\w.-]+/[\w.-]+/pull/\d+')
# ``rook: t_<hex>`` or ``rook: <slug>`` in a commit message names the task.
TASK_REF = re.compile(r'\brook:\s*(t_[0-9a-f]{32}|[a-z0-9][a-z0-9-]{2,79})\b')
BARE_TASK_ID = re.compile(r'\b(t_[0-9a-f]{32})\b')
# ``git commit`` / ``git -C dir commit`` in a shell command.
GIT_COMMIT = re.compile(r'\bgit\s+(?:[^\s;&|]+\s+){0,4}?commit\b')
# Only ``gh pr create`` prints a PR this call produced (``gh pr view/list``
# print PRs that already exist).
GH_PR_CREATE = re.compile(r'\bgh\s+pr\s+create\b')
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$|\))", re.S)
SEPARATORS = set(';&|')


def _strings(value, depth=0):
    """Every string in a call's args (the command, a commit message...). A
    list of strings (an argv) also yields the joined command line."""
    if isinstance(value, str):
        yield value
    elif depth < 4 and isinstance(value, dict):
        for v in value.values():
            yield from _strings(v, depth + 1)
    elif depth < 4 and isinstance(value, (list, tuple)):
        if value and all(isinstance(v, str) for v in value):
            yield shlex.join(value)
        for v in value:
            yield from _strings(v, depth + 1)


def _tokens(text):
    for attempt in (text, HEREDOC.sub('', text)):
        try:
            lex = shlex.shlex(attempt, posix=True, punctuation_chars=';&|')
            lex.whitespace_split = True
            return list(lex)
        except ValueError:
            continue
    return text.split()


def commit_messages(command: str) -> list[str]:
    """The messages of the ``git commit`` commands in a shell command line:
    ``-m``/``--message`` values (``-am``, ``-m"..."``, ``--message=``), and
    the here-document fed to ``-F -``. Nothing else in the command counts."""
    out = []
    for m in GIT_COMMIT.finditer(command):
        rest = command[m.end():]
        take, stdin = None, False
        for tok in _tokens(rest):
            if take:
                if take == 'm':
                    out.append(tok)
                else:
                    stdin = stdin or tok == '-'
                take = None
                continue
            if tok and set(tok) <= SEPARATORS:
                break  # the next command
            if tok in ('-m', '--message'):
                take = 'm'
            elif tok in ('-F', '--file'):
                take = 'F'
            elif tok.startswith('--message='):
                out.append(tok[len('--message='):])
            elif tok in ('--file=-', '-F-'):
                stdin = True
            elif tok.startswith('-') and not tok.startswith('--') and 'm' in tok[1:]:
                i = tok.index('m', 1)  # -am / -m"msg" / -amsg
                if i == len(tok) - 1:
                    take = 'm'
                else:
                    out.append(tok[i + 1:])
        if stdin:
            h = HEREDOC.search(rest)
            if h:
                out.append(h.group(3))
    return out


def _ago(seconds: float) -> str:
    return f'{int(seconds // 60)} min' if seconds < 5400 else f'{seconds / 3600:.1f} h'


class HygieneEngine:
    def __init__(self, store, conf=None, search=None):
        """``conf``: callable() -> mapping of the settings above (missing keys
        use DEFAULTS). ``search``: optional callable(band, query, limit) ->
        records, for suggestions (default: the store's keyword search)."""
        self.store = store
        self._conf = conf or (lambda: {})
        self._search = search

    # -- configuration --------------------------------------------------------
    def cfg(self, key):
        try:
            value = self._conf().get(key)
        except Exception:
            value = None
        return DEFAULTS[key] if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self.cfg('hygiene_enabled'))

    # -- the findings table ---------------------------------------------------
    def raise_(self, band, record, kind, actor, text, data=None, now=None, db=None):
        """Open a finding unless one is already open for (record, kind, actor),
        or it is a ``once`` kind that was resolved before, or the same finding
        was raised within the renotify period (rate limit). Returns the new
        finding id, or None."""
        if kind not in KINDS:
            raise ValueError(f'unknown hygiene kind {kind!r}')
        now = now or time.time()
        actor = actor or ''
        if db is None:
            with self.store.db() as db:
                return self.raise_(band, record, kind, actor, text, data, now, db)
        if db.execute('SELECT 1 FROM hygiene WHERE record=? AND kind=? AND actor=? AND resolved IS NULL',
                      (record, kind, actor)).fetchone():
            return None
        if KINDS[kind]['once']:
            if db.execute('SELECT 1 FROM hygiene WHERE record=? AND kind=? AND actor=?',
                          (record, kind, actor)).fetchone():
                return None
        elif db.execute('SELECT 1 FROM hygiene WHERE record=? AND kind=? AND actor=? AND created>?',
                        (record, kind, actor, now - self.cfg('hygiene_renotify_hours') * 3600)).fetchone():
            return None
        hid = 'h_' + uuid.uuid4().hex
        db.execute('INSERT INTO hygiene(id,band,record,kind,actor,text,data,created) VALUES(?,?,?,?,?,?,?,?)',
                   (hid, band, record, kind, actor, text, packed(data or {}), now))
        self.store._event(db, band, record, SYSTEM, 'hygiene', {'kind': kind, 'finding': hid,
                                                                 **({'for': actor} if actor else {})})
        return hid

    def resolve(self, record, kinds=None, actor=None, now=None, db=None) -> int:
        now = now or time.time()
        if db is None:
            with self.store.db() as db:
                return self.resolve(record, kinds, actor, now, db)
        sql, params = 'UPDATE hygiene SET resolved=? WHERE record=? AND resolved IS NULL', [now, record]
        if kinds:
            sql += ' AND kind IN (' + ','.join('?' * len(kinds)) + ')'
            params += list(kinds)
        if actor is not None:
            sql += ' AND actor=?'
            params.append(actor)
        return db.execute(sql, params).rowcount

    @staticmethod
    def _row(r) -> dict:
        out = {k: r[k] for k in ('id', 'record', 'kind', 'actor', 'text', 'created')}
        out['data'] = json.loads(r['data'] or '{}')
        return out

    def due(self, actor, now=None, limit=None) -> list[dict]:
        """The findings to show ``actor`` on its next reply (oldest first, at
        most ``hygiene_hints_per_reply``), NOT yet marked delivered: call
        :meth:`delivered` with the hints once they are actually on a reply.
        Each goes once; ``repeat`` kinds again after the renotify period while
        still open. Each hint carries its finding id as ``_id`` (strip it with
        :meth:`public` before showing)."""
        if not actor or not self.enabled:
            return []
        now = now or time.time()
        limit = int(limit or self.cfg('hygiene_hints_per_reply'))
        if limit <= 0:
            return []
        again = now - self.cfg('hygiene_renotify_hours') * 3600
        repeat = [k for k, v in KINDS.items() if v['repeat']]
        marks = ','.join('?' * len(repeat))
        with self.store.db(False) as db:
            due = db.execute(
                'SELECT h.*, r.slug FROM hygiene h LEFT JOIN records r ON r.id=h.record '
                f'WHERE h.actor=? AND h.resolved IS NULL AND (h.delivered IS NULL OR '
                f'(h.kind IN ({marks}) AND h.delivered<?)) ORDER BY h.created LIMIT ?',
                (actor, *repeat, again, limit)).fetchall()
        out = []
        for r in due:
            hint = {'kind': r['kind'], 'id': r['slug'] or r['record'], 'say': r['text'], '_id': r['id']}
            data = json.loads(r['data'] or '{}')
            if data.get('suggest'):
                hint['suggest'] = data['suggest']
            out.append(hint)
        return out

    @staticmethod
    def public(hints) -> list[dict]:
        return [{k: v for k, v in h.items() if k != '_id'} for h in hints]

    def delivered(self, hints, now=None) -> None:
        """Mark the findings behind ``hints`` (from :meth:`due`) delivered."""
        ids = [h['_id'] for h in hints if h.get('_id')]
        if not ids:
            return
        now = now or time.time()
        with self.store.db() as db:
            db.executemany('UPDATE hygiene SET delivered=?,deliveries=deliveries+1 WHERE id=?',
                           [(now, i) for i in ids])

    def take(self, actor, now=None, limit=None) -> list[dict]:
        """:meth:`due` and :meth:`delivered` in one step, for a caller that
        always shows what it takes."""
        hints = self.due(actor, now, limit)
        self.delivered(hints, now)
        return self.public(hints)

    @staticmethod
    def _bands_sql(bands, column='h.band'):
        if bands is None:
            return '', []
        return f' AND {column} IN (' + ','.join('?' * len(bands)) + ')', list(bands)

    def open(self, records=None, actor=None, limit=50, bands=None) -> list[dict]:
        """Open findings, newest first; ``bands`` limits them to those bands."""
        sql, params = ('SELECT h.*, r.slug FROM hygiene h LEFT JOIN records r ON r.id=h.record '
                       'WHERE h.resolved IS NULL'), []
        if records:
            sql += ' AND h.record IN (' + ','.join('?' * len(records)) + ')'
            params += list(records)
        if actor is not None:
            sql += ' AND h.actor=?'
            params.append(actor)
        more, extra = self._bands_sql(bands)
        sql += more + ' ORDER BY h.created DESC LIMIT ?'
        params += extra + [max(1, min(int(limit), 500))]
        with self.store.db(False) as db:
            return [self._row(r) | {'slug': r['slug'], 'band': r['band']} for r in db.execute(sql, params)]

    def flags(self, bands=None) -> dict:
        """record id -> sorted open finding kinds (for the deck)."""
        out: dict = {}
        more, params = self._bands_sql(bands, 'band')
        with self.store.db(False) as db:
            for r in db.execute('SELECT DISTINCT record, kind FROM hygiene WHERE resolved IS NULL' + more, params):
                out.setdefault(r['record'], set()).add(r['kind'])
        return {k: sorted(v) for k, v in out.items()}

    def notified(self, record, actor, since, kind='idle_claim') -> bool:
        """A ``kind`` finding for (record, actor) was delivered at or after
        ``since`` and is still open (the band-side nudge loop checks this so
        an agent is not nudged twice for one idle period)."""
        with self.store.db(False) as db:
            return bool(db.execute('SELECT 1 FROM hygiene WHERE record=? AND kind=? AND actor=? '
                                   'AND resolved IS NULL AND delivered>=?',
                                   (record, kind, actor, since)).fetchone())

    # -- helpers ----------------------------------------------------------------
    @staticmethod
    def _task(db, rid):
        row = db.execute("SELECT * FROM records WHERE (id=? OR slug=?) AND kind='task'", (rid, rid)).fetchall()
        return row[0] if len(row) == 1 else None

    def _claimants(self, db, task_id):
        return [c['actor'] for c in db.execute(
            'SELECT DISTINCT actor FROM claims WHERE task=? AND released IS NULL', (task_id,))]

    def _signal(self, actor, task_id, what, now=None, kind='work_signal'):
        """Tell the claimants (and ``actor``) that work on an open task looks
        finished or paused, and propose the state change."""
        with self.store.db() as db:
            t = self._task(db, task_id)
            if t is None or t['state'] in FINISHED:
                return []
            if kind == 'handoff_saved':
                if t['state'] != 'in_progress':
                    return []
                say = (f'{what} on [[{t["slug"]}]] while it is still in_progress. Stopping? '
                       f'rook_task release id={t["slug"]} (the handoff counts) or update state '
                       f'paused/blocked/done. Still working? Ignore this.')
            else:
                say = (f'{what} on [[{t["slug"]}]] ({t["state"]}). If the work is finished: rook_task update '
                       f'state done with attrs.outcome (link evidence first); if not, carry on.')
            who = set(self._claimants(db, t['id']))
            if actor and actor.get('id') and actor.get('kind') != 'system':
                who.add(actor['id'])
            return [h for a in sorted(who or {''})
                    if (h := self.raise_(t['band'], t['id'], kind, a, say, {'signal': what}, now, db))]

    # -- event triggers ---------------------------------------------------------
    def on_link(self, actor, task_id, kind, ref, relation='evidence', auto=False, now=None):
        """A link was added to a record. Commits and PRs on an open task are a
        work-completed signal; a handoff on in-progress work asks for a state."""
        if not self.enabled or not task_id:
            return []
        ref = str(ref or '')
        if kind == 'commit' or (kind == 'url' and ('/pull/' in ref or '/commit/' in ref)):
            what = ('PR ' if '/pull/' in ref else 'Commit ') + ref[:80] + ' linked'
            return self._signal(actor, task_id, what, now)
        if kind == 'handoff':
            return self._signal(actor, task_id, 'Handoff saved', now, kind='handoff_saved')
        return []

    def _claimed(self, actor_id, name, band=None):
        """The task id named ``name`` (id or slug) that ``actor_id`` holds a
        live claim on (in ``band`` when given), or None."""
        sql = ("SELECT DISTINCT r.id FROM claims c JOIN records r ON r.id=c.task WHERE c.actor=? "
               "AND c.released IS NULL AND c.last_active>=? AND r.kind='task' AND (r.id=? OR r.slug=?)")
        params = [actor_id, time.time() - STALE_CLAIM_SECS, name, name]
        if band:
            sql += ' AND r.band=?'
            params.append(band)
        with self.store.db(False) as db:
            rows = [r['id'] for r in db.execute(sql, params)]
        return rows[0] if len(rows) == 1 else None

    def on_call(self, actor, cap, args, reply, worker=None, now=None, band=None):
        """Look for finished work in a band call's reply: ``git commit`` output
        and the PR URL ``gh pr create`` prints. The commit's own message may
        name the task (``rook: <task id or slug>``, or a bare task id): the
        commit is evidence on it only when the caller holds a live claim on
        that task (in ``band``, the band of the worker the call ran on, when
        known). Anything else goes to the caller's own live claim as
        ``produced``, never as evidence: a message can name any task, and
        evidence is what lets a task go done. Returns the linked task ids."""
        if not self.enabled or not isinstance(reply, dict) or not reply.get('ok'):
            return []
        aid = (actor or {}).get('id')
        if not aid:
            return []
        res = reply.get('result')
        out = res.get('stdout') if isinstance(res, dict) else res if isinstance(res, str) else None
        if not isinstance(out, str) or not out:
            return []
        commands = list(dict.fromkeys(_strings(args)))
        said = '\n'.join(commands)[:20000]
        if 'git' not in said and 'gh ' not in said:
            return []
        commits = list(COMMIT_LINE.finditer(out)) if GIT_COMMIT.search(said) else []
        found = [('commit', m.group(2), f'{m.group(1)}: {m.group(3)[:160]}') for m in commits]
        if GH_PR_CREATE.search(said):
            found += [('url', u, 'pull request') for u in dict.fromkeys(PR_URL.findall(out))]
        if not found:
            return []
        messages = [msg for c in commands for msg in commit_messages(c)] + [m.group(3) for m in commits]
        said_in_messages = '\n'.join(messages)
        named = list(dict.fromkeys(TASK_REF.findall(said_in_messages) + BARE_TASK_ID.findall(said_in_messages)))
        evidence = next((t for t in (self._claimed(aid, n, band) for n in named[:5]) if t), None)
        linked = []
        for kind, ref, note in found[:5]:
            note = (note + (f' (on {worker})' if worker else ''))[:500]
            if evidence:
                task = self.store.auto_link(actor, kind, ref, relation='evidence', note=note, task=evidence)
            else:
                task = self.store.auto_link(actor, kind, ref, relation='produced', note=note, band=band)
            if task:
                linked.append(task)
                self.on_link(actor, task, kind, ref, auto=True, now=now)
        return linked

    def on_console_closed(self, actor, room, now=None):
        if not self.enabled or not room:
            return []
        with self.store.db(False) as db:
            tasks = {l['record'] for l in db.execute(
                "SELECT record FROM links WHERE kind='console' AND ref=? AND retracts IS NULL", (room,))}
        return [h for t in sorted(tasks) for h in self._signal(actor, t, f'Console {room[:40]} closed', now)]

    def on_record_changed(self, actor, before, after, now=None):
        """An update went through. Finishing or stopping a task resolves the
        claim findings; ``done`` checks for a knowledge page."""
        if not self.enabled or not before or not after:
            return []
        if after.get('kind') != 'task' or before.get('state') == after.get('state'):
            return []
        with self.store.db() as db:
            if after['state'] != 'in_progress':
                self.resolve(after['id'], CLAIM_KINDS, None, now, db)
            if after['state'] in FINISHED and after['state'] != 'done':
                self.resolve(after['id'], ('done_without_knowledge',), None, now, db)
        if after['state'] == 'done':
            return self._check_done(after['id'], actor, now)
        return []

    def on_claim(self, actor_id, task_id, now=None):
        """The claimant is active again: its idle findings no longer hold."""
        if task_id and actor_id:
            self.resolve(task_id, ('idle_claim', 'release_proposed', 'session_ended'), actor_id, now)

    def on_release(self, task_id, actor_id, now=None):
        if task_id and actor_id:
            self.resolve(task_id, CLAIM_KINDS, actor_id, now)

    def after_knowledge_write(self, now=None):
        """A page or link changed: re-check open done_without_knowledge
        findings (in a read transaction; one short write for the resolves)."""
        if not self.enabled:
            return 0
        with self.store.db(False) as db:
            tasks = [r['record'] for r in db.execute(
                "SELECT DISTINCT record FROM hygiene WHERE kind='done_without_knowledge' AND resolved IS NULL")]
            covered = [t for t in tasks if self._has_knowledge(db, t)]
        if covered:
            with self.store.db() as db:
                for t in covered:
                    self.resolve(t, ('done_without_knowledge',), None, now, db)
        return len(covered)

    def on_session_end(self, actor_id, now=None):
        """An MCP session closed. Its actor's in-progress claims with work since
        the last handoff get a ``session_ended`` finding (delivered when that
        actor is back) and are marked dirty for the deck."""
        if not self.enabled or not actor_id or actor_id in ('unverified', 'anonymous'):
            return []
        now = now or time.time()
        raised = []
        with self.store.db() as db:
            claims = db.execute("SELECT c.*, r.slug, r.title, r.band rband FROM claims c JOIN records r ON r.id=c.task "
                                "WHERE c.actor=? AND c.released IS NULL AND r.state='in_progress'",
                                (actor_id,)).fetchall()
            for c in claims:
                if not self._unhanded(db, c):
                    continue
                say = (f'Your session ended with [[{c["slug"]}]] claimed and work since its last handoff. '
                       f'Save a handoff (rook_handoff_save task={c["slug"]}), then set the state or release it.')
                hid = self.raise_(c['rband'], c['task'], 'session_ended', actor_id, say, {}, now, db)
                if hid:
                    raised.append(hid)
                    if not c['dirty']:
                        db.execute('UPDATE claims SET dirty=? WHERE id=?', (now, c['id']))
        return raised

    # -- conditions -------------------------------------------------------------
    @staticmethod
    def _unhanded(db, claim) -> bool:
        """Work since the last handoff on the claimed task."""
        last = db.execute("SELECT max(ts) FROM links WHERE record=? AND kind='handoff' AND retracts IS NULL",
                          (claim['task'],)).fetchone()[0] or 0
        return last < claim['last_active']

    def _has_knowledge(self, db, task_id) -> bool:
        """A knowledge page covers the task: it links the task (or the task
        links it), mentions [[slug]], or a claimant wrote it while working on it."""
        t = db.execute('SELECT * FROM records WHERE id=?', (task_id,)).fetchone()
        if t is None:
            return True
        live = self.store._live_links(db, task_id)
        refs = [l['ref'] for l in live if l['kind'] == 'record']
        if refs and db.execute("SELECT 1 FROM records WHERE kind='knowledge' AND id IN ("
                               + ','.join('?' * len(refs)) + ')', refs).fetchone():
            return True
        back = db.execute("SELECT l.id, l.record FROM links l JOIN records r ON r.id=l.record WHERE r.kind='knowledge' "
                          "AND l.kind='record' AND l.ref=? AND l.retracts IS NULL", (task_id,)).fetchall()
        for b in back:
            if not db.execute('SELECT 1 FROM links WHERE retracts=?', (b['id'],)).fetchone():
                return True
        if db.execute("SELECT 1 FROM records WHERE kind='knowledge' AND state='active' AND body LIKE ?",
                      ('%[[' + t['slug'] + ']]%',)).fetchone():
            return True
        claims = db.execute('SELECT actor, started FROM claims WHERE task=?', (task_id,)).fetchall()
        for c in claims:
            if db.execute("SELECT 1 FROM events e JOIN records r ON r.id=e.record WHERE r.kind='knowledge' "
                          "AND e.actor=? AND e.action IN ('created','updated') AND e.ts>=?",
                          (c['actor'], c['started'])).fetchone():
                return True
        return False

    def _suggest(self, band, title):
        try:
            found = (self._search(band, title, 8) if self._search else self.store.lexical(band, title, 8))
        except Exception:
            return []
        return [r['slug'] for r in found if r.get('kind') == 'knowledge'][:3]

    def _check_done(self, task_id, actor=None, now=None):
        with self.store.db(False) as db:
            t = db.execute('SELECT * FROM records WHERE id=?', (task_id,)).fetchone()
            if t is None or t['state'] != 'done' or self._has_knowledge(db, task_id):
                return []
            who = {c['actor'] for c in db.execute('SELECT DISTINCT actor FROM claims WHERE task=?', (task_id,))}
        if actor and actor.get('id') and actor.get('kind') != 'system':
            who.add(actor['id'])
        suggest = self._suggest(t['band'], t['title'])
        say = (f'[[{t["slug"]}]] is done but no knowledge page links or mentions it. If it taught anything '
               f'durable (a decision, procedure, fact), update a page'
               + (' (maybe ' + ', '.join(f'[[{s}]]' for s in suggest) + ')' if suggest else '')
               + f' or create one, and link it: rook_task link id={t["slug"]} data {{kind:"record", '
               f'ref:<page>, relation:"produced"}}. Nothing worth keeping? Ignore this.')
        with self.store.db() as db:
            return [h for a in sorted(who or {''})
                    if (h := self.raise_(t['band'], task_id, 'done_without_knowledge', a, say,
                                         {'suggest': suggest} if suggest else {}, now, db))]

    # -- the periodic scan ------------------------------------------------------
    def scan(self, now=None) -> list[dict]:
        """Evaluate the scan-owned conditions: raise new findings, resolve the
        ones that no longer hold, mark long-idle claims dirty. Returns the
        findings raised, as {id, kind, record, actor, text}.

        The conditions are evaluated in a read transaction; the writes
        (inserts, resolves, dirty marks) go in one short write transaction
        afterwards, so a scan never holds the write lock while it reads."""
        if not self.enabled:
            return []
        now = now or time.time()
        holding: set = set()          # (record, kind, actor) that still hold
        pending: list = []            # findings to raise
        done_checks: list = []        # done tasks without knowledge
        dirty: list = []              # (claim id, band, task, last_active, quiet)
        raised: list = []

        def want(band, record, kind, actor, text, data=None):
            holding.add((record, kind, actor or ''))
            pending.append((band, record, kind, actor or '', text, data))

        idle_s = float(self.cfg('hygiene_idle_minutes')) * 60
        dirty_s = float(self.cfg('hygiene_dirty_hours')) * 3600
        release_s = float(self.cfg('hygiene_release_hours')) * 3600
        scan_kinds = [k for k, v in KINDS.items() if v['scan']]
        with self.store.db(False) as db:
            # Idle claims: nudge, then mark dirty, then propose a release.
            for c in db.execute("SELECT c.*, r.slug, r.title, r.band rband FROM claims c JOIN records r "
                                "ON r.id=c.task WHERE c.released IS NULL AND r.state='in_progress'").fetchall():
                quiet = now - c['last_active']
                if quiet < idle_s or not self._unhanded(db, c):
                    continue
                if not self._nudged(db, c):
                    want(c['rband'], c['task'], 'idle_claim', c['actor'],
                         f'[[{c["slug"]}]] is claimed by you and idle {_ago(quiet)} with work since its last '
                         f'handoff. Stopped? rook_handoff_save, link evidence, record knowledge, set the state. '
                         f'Still on it? Carry on.')
                if quiet >= dirty_s and not c['dirty']:
                    dirty.append((c['id'], c['rband'], c['task'], c['last_active'], quiet))
                if quiet >= release_s:
                    text = (f'Claim by {c["actor"]} on [[{c["slug"]}]] idle {_ago(quiet)}. Proposal: if the '
                            f'work stopped, release it with a handoff: rook_task release id={c["slug"]} '
                            f'data {{actor:"{c["actor"]}", handoff:{{goal,state,next_steps}}}}.')
                    want(c['rband'], c['task'], 'release_proposed', c['actor'], text)
                    want(c['rband'], c['task'], 'release_proposed', '', text)
            # Done tasks without knowledge (recent ones: an old backlog is not news).
            since = now - float(self.cfg('hygiene_done_window_days')) * 86400
            for t in db.execute("SELECT id FROM records WHERE kind='task' AND state='done' AND updated>=?",
                                (since,)).fetchall():
                if not self._has_knowledge(db, t['id']):
                    for r in db.execute("SELECT actor FROM hygiene WHERE record=? AND kind='done_without_knowledge' "
                                        'AND resolved IS NULL', (t['id'],)):
                        holding.add((t['id'], 'done_without_knowledge', r['actor']))
                    done_checks.append(t['id'])
            # Projects whose tasks are all finished and quiet.
            quiet_s = float(self.cfg('hygiene_project_idle_hours')) * 3600
            for p in db.execute("SELECT * FROM records WHERE kind='project' AND state='active'").fetchall():
                tasks, todo = [], [p['id']]
                while todo:
                    kids = db.execute("SELECT id,state,updated FROM records WHERE parent=? AND kind='task'",
                                      (todo.pop(),)).fetchall()
                    tasks += kids
                    todo += [k['id'] for k in kids]
                if not tasks or any(k['state'] not in FINISHED for k in tasks):
                    continue
                if now - max(k['updated'] for k in tasks) < quiet_s:
                    continue
                ids = [k['id'] for k in tasks]
                last = db.execute("SELECT actor FROM events WHERE record IN (" + ','.join('?' * len(ids)) + ") "
                                  "AND actor NOT LIKE 'system:%' ORDER BY seq DESC LIMIT 1", ids).fetchone()
                text = (f'Every task under project [[{p["slug"]}]] is finished. Proposal: rook_project update '
                        f'id={p["slug"]} state done (or add the next task if it is not).')
                for a in {p['creator'], last['actor'] if last else ''}:
                    want(p['band'], p['id'], 'project_complete', '' if a.startswith('system:') else a, text)
            # Pages that lean on superseded pages or cancelled tasks.
            gone = {r['id']: r for r in db.execute(
                "SELECT id,slug,kind,state FROM records WHERE (kind='knowledge' AND state='superseded') "
                "OR (kind='task' AND state='cancelled')")}
            if gone:
                stale: dict = {}
                for g in gone.values():
                    if g['kind'] == 'knowledge':
                        for r in db.execute("SELECT id FROM records WHERE kind='knowledge' AND state='active' "
                                            'AND id<>? AND body LIKE ?', (g['id'], '%[[' + g['slug'] + ']]%')):
                            stale.setdefault(r['id'], set()).add(g['slug'])
                marks = ','.join('?' * len(gone))
                for l in db.execute(f"SELECT l.id lid, l.record, l.ref FROM links l JOIN records r ON r.id=l.record "
                                    f"WHERE r.kind='knowledge' AND r.state='active' AND l.kind='record' "
                                    f"AND l.retracts IS NULL AND l.relation<>'supersedes' AND l.ref IN ({marks})",
                                    list(gone)).fetchall():
                    if not db.execute('SELECT 1 FROM links WHERE retracts=?', (l['lid'],)).fetchone():
                        stale.setdefault(l['record'], set()).add(gone[l['ref']]['slug'])
                for rid, refs in stale.items():
                    page = db.execute('SELECT band, slug FROM records WHERE id=?', (rid,)).fetchone()
                    who = db.execute("SELECT actor FROM events WHERE record=? AND action IN ('created','updated') "
                                     "AND actor NOT LIKE 'system:%' ORDER BY seq DESC LIMIT 1", (rid,)).fetchone()
                    refs = sorted(refs)
                    want(page['band'], rid, 'stale_knowledge', who['actor'] if who else '',
                         f'[[{page["slug"]}]] relies on ' + ', '.join(f'[[{s}]]' for s in refs)
                         + ', now superseded or cancelled. Check it still holds; update it, or supersede it.',
                         {'refs': refs})
            # Scan-owned findings whose condition stopped holding. Only rows
            # seen here: one raised by an event after this read is left alone.
            open_now = {(r['record'], r['kind'], r['actor']): r['id'] for r in db.execute(
                'SELECT id, record, kind, actor FROM hygiene WHERE resolved IS NULL AND kind IN ('
                + ','.join('?' * len(scan_kinds)) + ')', scan_kinds).fetchall()}
            stopped = [hid for key, hid in open_now.items() if key not in holding]
            pending = [f for f in pending if (f[1], f[2], f[3]) not in open_now]  # already open

        if dirty or stopped or pending or self._expired_signals(now):
            with self.store.db() as db:
                for cid, band, task, last_active, quiet in dirty:
                    # Unless the claim came back to life since the read.
                    if db.execute('UPDATE claims SET dirty=? WHERE id=? AND dirty IS NULL AND released IS NULL '
                                  'AND last_active=?', (now, cid, last_active)).rowcount:
                        self.store._event(db, band, task, SYSTEM, 'hygiene_dirty', {'claim': cid, 'idle': int(quiet)})
                db.executemany('UPDATE hygiene SET resolved=? WHERE id=? AND resolved IS NULL',
                               [(now, i) for i in stopped])
                # Event findings outlive their moment: a day by default.
                db.execute("UPDATE hygiene SET resolved=? WHERE resolved IS NULL AND kind IN "
                           "('work_signal','handoff_saved','session_ended') AND created<?",
                           (now, now - float(self.cfg('hygiene_signal_hours')) * 3600))
                for band, record, kind, actor, text, data in pending:
                    hid = self.raise_(band, record, kind, actor, text, data, now, db)
                    if hid:
                        raised.append({'id': hid, 'kind': kind, 'record': record, 'actor': actor, 'text': text})
        for record in done_checks:
            for hid in self._check_done(record, None, now):
                raised.append({'id': hid, 'kind': 'done_without_knowledge', 'record': record})
        return raised

    def _expired_signals(self, now) -> bool:
        with self.store.db(False) as db:
            return bool(db.execute("SELECT 1 FROM hygiene WHERE resolved IS NULL AND kind IN "
                                   "('work_signal','handoff_saved','session_ended') AND created<? LIMIT 1",
                                   (now - float(self.cfg('hygiene_signal_hours')) * 3600,)).fetchone())

    @staticmethod
    def _nudged(db, claim) -> bool:
        """The band-side idle loop (rook/band_mcp/hygiene.py) already nudged
        this claim's agent for its current idle period (claims.nudged, with
        a ``hygiene_nudge`` event: a ``hygiene_dirty`` outcome reached nobody)."""
        if not claim['nudged'] or claim['nudged'] < claim['last_active']:
            return False
        return bool(db.execute("SELECT 1 FROM events WHERE band=? AND record=? AND action='hygiene_nudge' "
                               "AND ts>=? LIMIT 1", (claim['rband'], claim['task'], claim['last_active'])).fetchone())
