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
automatic links (a commit to the task it names or the caller's claim) and the
claim's ``dirty`` mark the deck already shows. It never changes a record's
state, title, body or attrs, and never releases a claim: state changes are
proposals in the finding's text. Every hook is bookkeeping: callers wrap it so
a failure here never fails the write or call that triggered it.

Triggers:

* ``work_signal`` (event): a commit or PR is linked to an open task (by hand,
  or detected in a ``rook_call`` reply: ``git commit`` output, ``gh pr``
  URLs), or a console linked to it is closed. Commits are auto-linked first.
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
import time
import uuid

from .store import packed

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


def _strings(value, depth=0):
    """Every string in a call's args (the command, a commit message...)."""
    if isinstance(value, str):
        yield value
    elif depth < 4 and isinstance(value, dict):
        for v in value.values():
            yield from _strings(v, depth + 1)
    elif depth < 4 and isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, depth + 1)


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

    def take(self, actor, now=None, limit=None) -> list[dict]:
        """The findings to show ``actor`` on its next reply (oldest first, at
        most ``hygiene_hints_per_reply``), marked delivered. Each goes once;
        ``repeat`` kinds again after the renotify period while still open."""
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
        if not due:
            return []
        with self.store.db() as db:
            for r in due:
                db.execute('UPDATE hygiene SET delivered=?,deliveries=deliveries+1 WHERE id=?', (now, r['id']))
        out = []
        for r in due:
            hint = {'kind': r['kind'], 'id': r['slug'] or r['record'], 'say': r['text']}
            data = json.loads(r['data'] or '{}')
            if data.get('suggest'):
                hint['suggest'] = data['suggest']
            out.append(hint)
        return out

    def open(self, records=None, actor=None, limit=50) -> list[dict]:
        sql, params = ('SELECT h.*, r.slug FROM hygiene h LEFT JOIN records r ON r.id=h.record '
                       'WHERE h.resolved IS NULL'), []
        if records:
            sql += ' AND h.record IN (' + ','.join('?' * len(records)) + ')'
            params += list(records)
        if actor is not None:
            sql += ' AND h.actor=?'
            params.append(actor)
        sql += ' ORDER BY h.created DESC LIMIT ?'
        params.append(max(1, min(int(limit), 500)))
        with self.store.db(False) as db:
            return [self._row(r) | {'slug': r['slug']} for r in db.execute(sql, params)]

    def flags(self) -> dict:
        """record id -> sorted open finding kinds (for the deck)."""
        out: dict = {}
        with self.store.db(False) as db:
            for r in db.execute('SELECT DISTINCT record, kind FROM hygiene WHERE resolved IS NULL'):
                out.setdefault(r['record'], set()).add(r['kind'])
        return {k: sorted(v) for k, v in out.items()}

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

    def on_call(self, actor, cap, args, reply, worker=None, now=None):
        """Look for finished work in a band call's reply: ``git commit`` output
        and GitHub PR URLs. A commit is auto-linked to the task its message
        names (``rook: <task id or slug>``, or a bare task id) as evidence, or
        else to the caller's live claim as ``produced``. Returns the linked
        task ids."""
        if not self.enabled or not isinstance(reply, dict) or not reply.get('ok'):
            return []
        res = reply.get('result')
        out = res.get('stdout') if isinstance(res, dict) else res if isinstance(res, str) else None
        if not isinstance(out, str) or not out:
            return []
        said = '\n'.join(_strings(args))[:20000]
        if 'git' not in said and 'gh ' not in said:
            return []
        named = TASK_REF.findall(said) or BARE_TASK_ID.findall(said)
        found = [('commit', m.group(2), f'{m.group(1)}: {m.group(3)[:160]}') for m in COMMIT_LINE.finditer(out)]
        if 'gh ' in said:
            found += [('url', u, 'pull request') for u in dict.fromkeys(PR_URL.findall(out))]
        linked = []
        for kind, ref, note in found[:5]:
            note = (note + (f' (on {worker})' if worker else ''))[:500]
            task = None
            for name in named:
                try:
                    task = self.store.auto_link(actor, kind, ref, relation='evidence', note=note, task=name)
                    break
                except ValueError:
                    continue
            if task is None:
                task = self.store.auto_link(actor, kind, ref, relation='produced', note=note)
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
        """A page or link changed: re-check open done_without_knowledge findings."""
        if not self.enabled:
            return 0
        with self.store.db(False) as db:
            tasks = [r['record'] for r in db.execute(
                "SELECT DISTINCT record FROM hygiene WHERE kind='done_without_knowledge' AND resolved IS NULL")]
            covered = [t for t in tasks if self._has_knowledge(db, t)]
        for t in covered:
            self.resolve(t, ('done_without_knowledge',), None, now)
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
        findings raised, as {id, kind, record, actor, text}."""
        if not self.enabled:
            return []
        now = now or time.time()
        holding: set = set()          # (record, kind, actor) that still hold
        raised: list = []

        def want(band, record, kind, actor, text, data=None):
            holding.add((record, kind, actor or ''))
            pending.append((band, record, kind, actor or '', text, data))
        pending: list = []

        idle_s = float(self.cfg('hygiene_idle_minutes')) * 60
        dirty_s = float(self.cfg('hygiene_dirty_hours')) * 3600
        release_s = float(self.cfg('hygiene_release_hours')) * 3600
        with self.store.db() as db:
            # Idle claims: nudge, then mark dirty, then propose a release.
            for c in db.execute("SELECT c.*, r.slug, r.title, r.band rband FROM claims c JOIN records r "
                                "ON r.id=c.task WHERE c.released IS NULL AND r.state='in_progress'").fetchall():
                quiet = now - c['last_active']
                if quiet < idle_s or not self._unhanded(db, c):
                    continue
                want(c['rband'], c['task'], 'idle_claim', c['actor'],
                     f'[[{c["slug"]}]] is claimed by you and idle {_ago(quiet)} with work since its last '
                     f'handoff. Stopped? rook_handoff_save, link evidence, record knowledge, set the state. '
                     f'Still on it? Carry on.')
                if quiet >= dirty_s and not c['dirty']:
                    db.execute('UPDATE claims SET dirty=? WHERE id=?', (now, c['id']))
                    self.store._event(db, c['rband'], c['task'], SYSTEM, 'hygiene_dirty',
                                      {'claim': c['id'], 'idle': int(quiet)})
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
                    pending.append(('check_done', t['id'], None, None, None, None))
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
            # Resolve scan-owned findings whose condition stopped holding.
            scan_kinds = [k for k, v in KINDS.items() if v['scan']]
            for r in db.execute('SELECT id, record, kind, actor FROM hygiene WHERE resolved IS NULL AND kind IN ('
                                + ','.join('?' * len(scan_kinds)) + ')', scan_kinds).fetchall():
                if (r['record'], r['kind'], r['actor']) not in holding:
                    db.execute('UPDATE hygiene SET resolved=? WHERE id=?', (now, r['id']))
            # Event findings outlive their moment: a day by default.
            ttl = now - float(self.cfg('hygiene_signal_hours')) * 3600
            db.execute("UPDATE hygiene SET resolved=? WHERE resolved IS NULL AND kind IN "
                       "('work_signal','handoff_saved','session_ended') AND created<?", (now, ttl))
            for band, record, kind, actor, text, data in pending:
                if band == 'check_done':
                    continue
                hid = self.raise_(band, record, kind, actor, text, data, now, db)
                if hid:
                    raised.append({'id': hid, 'kind': kind, 'record': record, 'actor': actor, 'text': text})
        for band, record, *_ in pending:
            if band == 'check_done':
                for hid in self._check_done(record, None, now):
                    raised.append({'id': hid, 'kind': 'done_without_knowledge', 'record': record})
        return raised
