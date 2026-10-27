"""Pull requests behind tasks: deterministic checks against GitHub.

Three failures this aims at (2026-10-27): tasks marked ``done`` before their
pull request merged, claims left ``in_progress`` after it merged, and agents
asking for merges, marking done or reporting green while CI was red or still
running. No model is involved: the hub reads the task's own links and the
pull request's state on GitHub, and says what to do.

* **Done check** (:meth:`PullRequests.done_check`, called by the knowledge
  service before an ``update`` to ``done``): a task whose linked pull request
  is still open is refused for an agent, with the way out in the message
  (get it merged, or retract the link if it is not this task's). A person
  gets the same text as a note and the update goes through: people decide.
  GitHub unreachable: the update goes through with a note (fail open).
* **Sweep** (:meth:`PullRequests.sweep`, every ``hygiene_github_minutes``
  from the hygiene loop): for in-progress tasks and recently done ones that
  link a pull request, raises hygiene findings (docs/design/hygiene.md):

  - ``pr_merged``: every linked PR is merged or closed (one merged) while
    the task is still ``in_progress``. The merge commit is linked as
    evidence (automatic link), so ``done`` needs only the outcome.
  - ``pr_ci_failed``: an open linked PR whose checks failed at its head.
    The finding follows the head: a new failing head re-delivers it.
  - ``pr_open_done``: the task is ``done`` but a linked PR is still open
    (finished elsewhere, or while GitHub was unreachable).

  Each resolves when its condition stops holding. A sweep that cannot reach
  GitHub changes nothing for the PRs it could not read.
* **Merge hook** (:meth:`PullRequests.on_merged`): the hub's own merge (the
  huddle merge vote) tells the tasks that link that PR at once.

Which links count: ``url`` links (or any link whose ref is a pull request
URL) with relation ``evidence``, ``produced`` or ``touched`` (the work's own
links; ``mentions``, ``source``, ``blocked_by`` and the rest are about other
work). Retracted links do not count.

Writes stay inside the hygiene rules: findings, events, and the automatic
merge-commit link. Never a task's state, never a claim, never a hold.
"""
from __future__ import annotations

import json
import logging
import re
import time

from ....core.text import clip
from .store import packed

log = logging.getLogger("rook.hub.plugins.knowledge.prs")

SYSTEM = {'id': 'system:hygiene', 'kind': 'system', 'label': 'Rook hygiene'}
PR_REF = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)(?!\d)', re.I)
WORK_RELATIONS = ('evidence', 'produced', 'touched')
OPEN_TASK = ('todo', 'in_progress', 'blocked', 'paused')
#: Findings this module owns (raised and resolved by the sweep).
KINDS = ('pr_merged', 'pr_ci_failed', 'pr_open_done')
#: At most this many PRs read from GitHub per sweep / per done check.
SWEEP_MAX = 40
DONE_MAX = 5
#: A done check reuses a PR read this recent (seconds).
DONE_FRESH = 60.0
CI_OK = ('success', 'neutral', 'skipped')


def pr_key(repo: str, number) -> tuple[str, int]:
    return str(repo).lower(), int(number)


def label(repo: str, number) -> str:
    return f'{repo}#{int(number)}'


def linked_prs(store, db, task_id) -> list[dict]:
    """The task's work pull requests: [{repo, number, link, relation}], one
    per PR (the first live link to it)."""
    out, seen = [], set()
    for l in store._live_links(db, task_id):
        if l['relation'] not in WORK_RELATIONS:
            continue
        m = PR_REF.search(str(l['ref'] or ''))
        if not m:
            continue
        key = pr_key(m.group(1), m.group(2))
        if key in seen:
            continue
        seen.add(key)
        out.append({'repo': m.group(1), 'number': int(m.group(2)), 'link': l['id'],
                    'relation': l['relation']})
    return out


def ci_summary(checks: dict) -> dict:
    """GitHub check runs + commit statuses -> {state: green|failing|running|none,
    failed: [names], running: [names], count}. Failing wins over running."""
    runs = (checks or {}).get('runs') or []
    statuses = (checks or {}).get('statuses') or []
    failed = [f"{r.get('name')}: {r.get('conclusion')}" for r in runs
              if r.get('status') == 'completed' and r.get('conclusion') not in CI_OK]
    failed += [f"{s.get('context')}: {s.get('state')}" for s in statuses
               if s.get('state') in ('failure', 'error')]
    running = [str(r.get('name')) for r in runs if r.get('status') != 'completed']
    running += [str(s.get('context')) for s in statuses if s.get('state') == 'pending']
    count = len(runs) + len(statuses)
    state = 'failing' if failed else 'running' if running else 'green' if count else 'none'
    return {'state': state, 'failed': failed[:6], 'running': running[:6], 'count': count}


def ci_words(ci: dict | None) -> str:
    if not ci:
        return ''
    if ci['state'] == 'failing':
        return 'CI failing: ' + '; '.join(ci['failed'][:3])
    if ci['state'] == 'running':
        return f"CI still running ({', '.join(ci['running'][:3])})"
    if ci['state'] == 'green':
        return f"CI green ({ci['count']} checks)"
    return 'no CI checks'


class PullRequests:
    def __init__(self, store, engine, github=None, clock=time.time):
        """``engine``: the :class:`HygieneEngine` (settings, findings).
        ``github``: callable() -> a client with ``pr(repo, number)`` and
        ``checks(repo, sha)`` (rook.hub.plugins.huddle.table.GitHub), or None
        when there is none; called once per sweep or check."""
        self.store = store
        self.engine = engine
        self.github = github
        self.clock = clock
        self._cache: dict = {}        # (repo, number) -> info, with 'at'
        self._ci: dict = {}           # (repo, number, head) -> (at, summary)
        self.last_sweep = 0.0
        self.last_error = None

    # -- GitHub ---------------------------------------------------------------
    def _client(self):
        if self.github is None:
            return None
        return self.github()

    def read(self, client, repo, number, max_age, checks=False) -> dict:
        """The PR's state (cached ``max_age`` seconds; merged ones for good),
        plus ``ci`` (its head's checks) when ``checks`` and it is open.
        Raises whatever the client raises."""
        key = pr_key(repo, number)
        now = self.clock()
        info = self._cache.get(key)
        if info is None or (not info['merged'] and now - info['at'] > max_age):
            p = client.pr(repo, int(number))
            info = {'repo': repo, 'number': int(number), 'state': p.get('state'),
                    'merged': bool(p.get('merged')), 'draft': bool(p.get('draft')),
                    'head': p.get('head') or '', 'merge_sha': p.get('merge_sha') or '',
                    'url': p.get('url') or f'https://github.com/{repo}/pull/{int(number)}',
                    'at': now}
            self._cache[key] = info
        info = dict(info)
        if checks and info['state'] == 'open' and info['head']:
            ck = key + (info['head'],)
            got = self._ci.get(ck)
            # A green head stays green; anything else is read again when stale.
            if got is None or (got[1]['state'] != 'green' and now - got[0] > max_age):
                got = (now, ci_summary(client.checks(repo, info['head'])))
                self._ci[ck] = got
            info['ci'] = got[1]
        return info

    def merged(self, repo, number, merge_sha='', head=''):
        """Record a merge the hub carried out itself (no GitHub read needed)."""
        key = pr_key(repo, number)
        old = self._cache.get(key) or {}
        self._cache[key] = {**old, 'repo': repo, 'number': int(number), 'state': 'closed',
                            'merged': True, 'draft': False, 'head': head or old.get('head', ''),
                            'merge_sha': merge_sha or old.get('merge_sha', ''),
                            'url': old.get('url') or f'https://github.com/{repo}/pull/{int(number)}',
                            'at': self.clock()}

    # -- the done check -------------------------------------------------------
    def done_check(self, task_id) -> dict:
        """Before a task goes done: {'refuse': text or None, 'notes': [text]}.
        ``refuse`` is set when a linked PR is still open."""
        with self.store.db(False) as db:
            t = db.execute('SELECT slug FROM records WHERE id=?', (task_id,)).fetchone()
            prs = linked_prs(self.store, db, task_id)
        if t is None or not prs or not self.engine.enabled:
            return {'refuse': None, 'notes': []}
        notes, still = [], []
        try:
            client = self._client()
        except Exception as e:  # noqa: BLE001 - fail open
            client, notes = None, [f'could not set up a GitHub client ({clip(str(e), 120)})']
        if client is None:
            return {'refuse': None, 'notes': notes + [
                'could not check the linked pull requests on GitHub (no client); make sure they merged']}
        merged_any = False
        for p in prs[:DONE_MAX]:
            name = label(p['repo'], p['number'])
            try:
                info = self.read(client, p['repo'], p['number'], DONE_FRESH, checks=True)
            except Exception as e:  # noqa: BLE001 - fail open, say so
                notes.append(f'could not check {name} on GitHub ({clip(str(e), 120)}); make sure it merged')
                continue
            if info['merged']:
                merged_any = True
            elif info['state'] == 'open':
                what = f"{name} is still open{' (a draft)' if info['draft'] else ''}"
                ci = ci_words(info.get('ci'))
                still.append((what + (f', {ci}' if ci else ''), p['link']))
            else:
                notes.append(f'{name} was closed without merging')
        if len(prs) > DONE_MAX:
            notes.append(f'only the first {DONE_MAX} linked pull requests were checked')
        if not still:
            if notes and not merged_any and not any('could not' in n for n in notes):
                notes.append('no linked pull request merged: if the work landed another way, link that commit')
            return {'refuse': None, 'notes': notes}
        text = ('Not done yet: ' + '; '.join(s for s, _ in still) + '. A task is done once its pull '
                'request has merged: get CI green, ask the huddle chair to pin it and call the merge '
                'vote (the hub merges it and links the merge commit here), then mark it done. If that '
                'PR is not this task\'s work, retract its link first (rook_task retract id='
                + ', id='.join(l for _, l in still) + '). Work stopped? Use paused, blocked or cancelled.')
        return {'refuse': text, 'notes': notes}

    # -- the sweep --------------------------------------------------------------
    def due(self, now=None) -> bool:
        minutes = float(self.engine.cfg('hygiene_github_minutes') or 0)
        if minutes <= 0 or not self.engine.enabled or self.github is None:
            return False
        return (now or self.clock()) - self.last_sweep >= minutes * 60

    def sweep(self, now=None) -> list[dict]:
        """Read the linked PRs of in-progress and recently done tasks and
        raise or resolve the PR findings. Returns the findings raised."""
        now = now or self.clock()
        self.last_sweep = now
        minutes = float(self.engine.cfg('hygiene_github_minutes') or 0)
        if minutes <= 0 or not self.engine.enabled:
            return []
        max_age = max(30.0, minutes * 60 - 5)
        since = now - float(self.engine.cfg('hygiene_done_window_days')) * 86400
        with self.store.db(False) as db:
            rows = db.execute(
                "SELECT * FROM records r WHERE r.kind='task' AND (r.state='in_progress' OR "
                "(r.state='done' AND r.updated>=?)) AND EXISTS (SELECT 1 FROM links l WHERE "
                "l.record=r.id AND l.ref LIKE '%github.com/%/pull/%' AND l.retracts IS NULL)",
                (since,)).fetchall()
            tasks = []
            for r in rows:
                prs = linked_prs(self.store, db, r['id'])
                if not prs:
                    continue
                claimants = sorted({c['actor'] for c in db.execute(
                    'SELECT DISTINCT actor FROM claims WHERE task=? AND released IS NULL', (r['id'],))})
                done_by = None
                if r['state'] == 'done':
                    e = db.execute("SELECT actor FROM events WHERE record=? AND action='updated' "
                                   "AND actor NOT LIKE 'system:%' ORDER BY seq DESC LIMIT 1",
                                   (r['id'],)).fetchone()
                    done_by = e['actor'] if e else None
                evidence = {l['ref'] for l in self.store._live_links(db, r['id']) if l['kind'] == 'commit'}
                tasks.append({'id': r['id'], 'band': r['band'], 'slug': r['slug'], 'state': r['state'],
                              'prs': prs, 'claimants': claimants, 'done_by': done_by,
                              'commits': evidence})
        if not tasks:
            self._resolve_all_but(set(), set(), now)
            self.last_error = None
            return []
        try:
            client = self._client()
        except Exception as e:  # noqa: BLE001
            client, self.last_error = None, f'GitHub client: {e}'
        if client is None:
            return []  # nothing read: change nothing
        infos: dict = {}
        unread: set = set()
        reads = 0
        self.last_error = None
        # A PR's checks matter only for in-progress work.
        checks = {pr_key(p['repo'], p['number']) for t in tasks if t['state'] == 'in_progress'
                  for p in t['prs']}
        for t in tasks:
            for p in t['prs']:
                key = pr_key(p['repo'], p['number'])
                if key in infos or key in unread:
                    continue
                cached = self._cache.get(key)
                fresh = cached is not None and (cached['merged'] or now - cached['at'] <= max_age)
                if not fresh and reads >= SWEEP_MAX:
                    unread.add(key)
                    continue
                try:
                    reads += 0 if fresh else 1
                    infos[key] = self.read(client, p['repo'], p['number'], max_age,
                                           checks=key in checks)
                except Exception as e:  # noqa: BLE001 - fail open for this PR
                    self.last_error = f'{label(p["repo"], p["number"])}: {clip(str(e), 160)}'
                    unread.add(key)
                    if getattr(e, 'status', None) in (0, 403, 429):
                        # Unreachable or rate limited: stop reading this sweep.
                        reads = SWEEP_MAX
        holding, skipped, want, links = set(), set(), [], []
        for t in tasks:
            keys = [pr_key(p['repo'], p['number']) for p in t['prs']]
            if any(k in unread for k in keys):
                skipped.add(t['id'])
                continue
            got = [infos[k] for k in keys]
            who = t['claimants'] or ['']
            slug = t['slug']
            if t['state'] == 'in_progress':
                open_ = [i for i in got if not i['merged'] and i['state'] == 'open']
                merged = [i for i in got if i['merged']]
                if merged and not open_:
                    shas = [i['merge_sha'] for i in merged if i['merge_sha']]
                    for i in merged:
                        if i['merge_sha'] and i['merge_sha'] not in t['commits']:
                            links.append((t, i))
                    names = ', '.join(label(i['repo'], i['number']) for i in merged)
                    text = (f'[[{slug}]] is still in_progress but its pull request {names} merged'
                            + (f" (merge commit {', '.join(s[:7] for s in shas)}, linked as evidence)"
                               if shas else '')
                            + f'. Finished? rook_task update id={slug} state done with attrs.outcome. '
                              'More to do? Link the next PR and carry on.')
                    for a in who:
                        want.append((t, 'pr_merged', a, text, {'prs': names, 'merge': shas}))
                for i in open_:
                    ci = i.get('ci') or {}
                    if ci.get('state') != 'failing':
                        continue
                    name = label(i['repo'], i['number'])
                    text = (f'CI is red on {name} at {i["head"][:7]} ({"; ".join(ci["failed"][:3])}), '
                            f'linked from [[{slug}]]. Fix it before asking for a merge, marking the task '
                            f'done or reporting it green: gh pr checks {i["number"]} --repo {i["repo"]}.')
                    for a in who:
                        want.append((t, 'pr_ci_failed', a, text, {'pr': name, 'head': i['head'],
                                                                  'failed': ci['failed']}))
                    break  # one CI finding per task
            else:
                open_ = [i for i in got if not i['merged'] and i['state'] == 'open']
                if open_:
                    names = ', '.join(label(i['repo'], i['number']) for i in open_)
                    text = (f'[[{slug}]] is done but {names} is still open (not merged). Reopen it '
                            f'(rook_task update id={slug} state in_progress) until the PR merges, or '
                            f'retract that link if the PR is not this task\'s work.')
                    for a in sorted(set(t['claimants']) | ({t['done_by']} if t['done_by'] else set())) or ['']:
                        want.append((t, 'pr_open_done', a, text, {'prs': names}))
        for t, kind, actor, _, _ in want:
            holding.add((t['id'], kind, actor))
        raised = []
        with self.store.db() as db:
            for t, info in links:
                self._link_merge(db, t['band'], t['id'], info, 'seen merged on GitHub')
            # Open PR findings: refresh a CI finding whose head moved, keep the rest.
            open_now = {(r['record'], r['kind'], r['actor']): r for r in db.execute(
                'SELECT id, record, kind, actor, data FROM hygiene WHERE resolved IS NULL AND kind IN ('
                + ','.join('?' * len(KINDS)) + ')', KINDS).fetchall()}
            for t, kind, actor, text, data in want:
                old = open_now.get((t['id'], kind, actor))
                if old is not None:
                    if kind == 'pr_ci_failed' and json.loads(old['data'] or '{}').get('head') != data['head']:
                        db.execute('UPDATE hygiene SET text=?, data=?, delivered=NULL WHERE id=?',
                                   (text, packed(data), old['id']))
                    continue
                hid = self.engine.raise_(t['band'], t['id'], kind, actor, text, data, now, db)
                if hid:
                    raised.append({'id': hid, 'kind': kind, 'record': t['id'], 'actor': actor, 'text': text})
            gone = [r['id'] for key, r in open_now.items() if key not in holding and key[0] not in skipped]
            db.executemany('UPDATE hygiene SET resolved=? WHERE id=? AND resolved IS NULL',
                           [(now, i) for i in gone])
        return raised

    def _resolve_all_but(self, holding, skipped, now):
        with self.store.db() as db:
            rows = db.execute('SELECT id, record, kind, actor FROM hygiene WHERE resolved IS NULL AND kind IN ('
                              + ','.join('?' * len(KINDS)) + ')', KINDS).fetchall()
            gone = [r['id'] for r in rows
                    if (r['record'], r['kind'], r['actor']) not in holding and r['record'] not in skipped]
            if gone:
                db.executemany('UPDATE hygiene SET resolved=? WHERE id=? AND resolved IS NULL',
                               [(now, i) for i in gone])

    def _link_merge(self, db, band, task_id, info, how):
        """Link a merged PR's merge commit to the task as evidence (once)."""
        sha = info.get('merge_sha')
        if not sha:
            return None
        if db.execute("SELECT 1 FROM links WHERE record=? AND kind='commit' AND ref=? AND retracts IS NULL",
                      (task_id, sha)).fetchone():
            return None
        note = f"merge commit of {label(info['repo'], info['number'])} ({how})"
        lid = self.store._link(db, band, task_id, SYSTEM, 'commit', sha, 'evidence', note, auto=True)
        self.store._event(db, band, task_id, SYSTEM, 'linked',
                          {'link': lid, 'kind': 'commit', 'ref': sha, 'relation': 'evidence', 'auto': True})
        return lid

    # -- the hub's own merge ----------------------------------------------------
    def on_merged(self, repo, number, merge_sha='', head='', how='merged by the hub', now=None) -> list[str]:
        """The hub merged ``repo#number``: link the merge commit to every open
        task that links that PR, and tell the claimants of the in-progress
        ones whose other linked PRs are settled. Returns the task ids."""
        if not self.engine.enabled:
            return []
        self.merged(repo, number, merge_sha, head)
        now = now or self.clock()
        info = self._cache[pr_key(repo, number)]
        key = pr_key(repo, number)
        out = []
        with self.store.db() as db:
            rows = db.execute(
                "SELECT DISTINCT r.* FROM links l JOIN records r ON r.id=l.record WHERE r.kind='task' "
                "AND l.retracts IS NULL AND lower(l.ref) LIKE ?",
                (f'%github.com/{key[0]}/pull/{key[1]}%',)).fetchall()
            for r in rows:
                if r['state'] not in OPEN_TASK:
                    continue
                prs = linked_prs(self.store, db, r['id'])
                if key not in [pr_key(p['repo'], p['number']) for p in prs]:
                    continue
                self._link_merge(db, r['band'], r['id'], info, how)
                out.append(r['id'])
                if r['state'] != 'in_progress':
                    continue
                others = [self._cache.get(pr_key(p['repo'], p['number'])) for p in prs
                          if pr_key(p['repo'], p['number']) != key]
                if any(o is None or (not o['merged'] and o['state'] == 'open') for o in others):
                    continue  # the sweep decides once it has read them
                who = sorted({c['actor'] for c in db.execute(
                    'SELECT DISTINCT actor FROM claims WHERE task=? AND released IS NULL', (r['id'],))}) or ['']
                name = label(repo, number)
                text = (f'[[{r["slug"]}]] is still in_progress but its pull request {name} merged'
                        + (f' (merge commit {merge_sha[:7]}, linked as evidence)' if merge_sha else '')
                        + f'. Finished? rook_task update id={r["slug"]} state done with attrs.outcome. '
                          'More to do? Link the next PR and carry on.')
                for a in who:
                    self.engine.raise_(r['band'], r['id'], 'pr_merged', a, text,
                                       {'prs': name, 'merge': [merge_sha] if merge_sha else []}, now, db)
        return out
