"""Service layer for shared knowledge and agent work: one ``dispatch`` used by
the hub caps (``knowledge.*``, ``task.*``), the MCP tools generated from them
and the operator's Knowledge page.

Attribution only: every write records the caller's compound identity
(``token.client.host@dir``, see ``rook.band_mcp.attribution``). Nothing here
sits in the band call path or can deny a call. See
docs/DESIGN-agent-work-system.md.

Bands are the operator's existing enrollment bands. Records are addressed by
id or slug, and tasks and projects also by their speakable name (rook.core.names:
reads may take a single clear near miss, writes need the exact name); ids are
global, so most calls need no ``band``. The deck covers all bands.
"""
import asyncio
import logging
import time
from ....band_mcp.secret_mask import scrub
from . import whiteboard
from .store import KnowledgeStore, Unresolved
from .search import RELATED_MAX, Search

log = logging.getLogger(__name__)

WRITES = ('create', 'update', 'link', 'retract', 'claim', 'release', 'review', 'note', 'jot', 'batch')
BATCH_OPS = ('create', 'update', 'link', 'retract', 'claim', 'release', 'note', 'jot')
BATCH_MAX = 50
CLOSED_TASK = ('done', 'closed', 'cancelled', 'archived')

# MCP replies (not the operator's web page) get lean defaults: agents pay for
# every character. ``data.limit`` and ``data.fields`` override them.
MCP_SEARCH_LIMIT = 5
MCP_LIST_LIMIT = 20
MCP_SEARCH_FIELDS = ('id', 'name', 'slug', 'kind', 'title', 'state', 'score', 'section', 'excerpt')
MCP_LIST_FIELDS = ('id', 'name', 'slug', 'kind', 'title', 'state', 'parent', 'excerpt')
EXCERPT = 240
MCP_GET_EVENTS = 10
# A lean get lists a page's neighbours as stubs ({slug, title, gist}, a few
# dozen tokens each) so the next hop needs no search: links_out and
# backlinks (curated [[links]]) up to MCP_GET_STUBS each, related (semantic,
# scored; data.related = 0..MCP_GET_STUBS, default search.RELATED_MAX).
MCP_GET_STUBS = 10
MCP_OUTCOME_CHARS = 240
# Deck paging (data.limit): projects per page.
DECK_LIMIT = 100


def _fields(value):
    if value is None or value == '':
        return None
    if isinstance(value, str):
        return [f.strip() for f in value.split(',') if f.strip()]
    return [str(f) for f in value]


def project(rows, fields, default):
    """Keep ``fields`` (or ``default``) of each row; ``excerpt`` is derived
    from ``body`` when a row has none; ``fields=['all']`` keeps every field."""
    wanted = _fields(fields) or list(default)
    out = []
    for r in rows:
        r = dict(r)
        if 'excerpt' not in r and 'body' in r:
            r['excerpt'] = r['body'][:EXCERPT]
        out.append(r if 'all' in wanted else {k: r[k] for k in wanted if k in r})
    return out


class KnowledgeService:
    def __init__(self, path, principal, enrollment=None, handoffs=None, search=None,
                 handoff_list=None, handoff_close=None):
        """``search``: optional ``callable(store) -> Search`` (the plugin passes
        its configured embedder); default reads the legacy env vars."""
        self.store = KnowledgeStore(path)
        self.principal = principal
        self.enrollment = enrollment
        self.handoffs = handoffs  # callable(author, handoff dict) -> handoff thread_id
        self.handoff_list = handoff_list    # callable() -> active threads (latest handoff each)
        self.handoff_close = handoff_close  # callable(thread_id, author, reason) -> None
        self.search = search(self.store) if search else Search(self.store)
        self.last_maintenance = None
        self.last_error = None
        self._band_cache = (0, [])
        # HygieneEngine (hygiene.py), set by the knowledge plugin; None = off.
        self.hygiene = None
        # PullRequests (prs.py): a task's linked PRs on GitHub; None = off.
        self.prs = None

    def _hook(self, name, *args, **kwargs):
        """Run a hygiene hook. Bookkeeping: never fails the caller."""
        engine = self.hygiene
        if engine is None:
            return None
        try:
            return getattr(engine, name)(*args, **kwargs)
        except Exception:
            log.exception('hygiene hook %s failed', name)
            return None

    def auto_link(self, actor, kind, ref, relation='touched', note='', task=None, board=None):
        """The store's auto_link (which also writes the claim's whiteboard
        line; ``board`` adds {text, worker, cap, ok}) plus its hygiene trigger
        (a handoff on in-progress work asks for a state). Returns the task id or None."""
        linked = self.store.auto_link(actor, kind, ref, relation=relation, note=note, task=task, board=board)
        if linked:
            self._hook('on_link', actor, linked, kind, ref, relation, True)
        return linked

    def bands(self):
        """The operator's existing bands (all of them, until per-user band
        access lands). Without an enrollment registry there is one: default."""
        if time.monotonic() - self._band_cache[0] < 5:
            return self._band_cache[1]
        if self.enrollment:
            result = [{'id': b['id'], 'name': b['name'], 'label': b['label'],
                       'primary': bool(b['is_primary'])}
                      for b in self.enrollment.bands(active_only=True)]
        else:
            result = [{'id': 'default', 'name': 'Default', 'label': None, 'primary': True}]
        self._band_cache = (time.monotonic(), result)
        return result

    def actor(self):
        """The attributed caller, by compound identity. Unverified callers may
        still write; their records say so."""
        p = self.principal() or {}
        aid = p.get('actor') or ('unverified' if p.get('kind') == 'unverified' or not p else p.get('identity'))
        return {'id': aid or 'unverified', 'kind': p.get('kind') or 'unverified', 'label': aid or 'unverified',
                **{k: p.get(k) for k in ('key_id', 'agent_id', 'token', 'client', 'host', 'dir') if p.get(k)}}

    def band(self, requested=None):
        """Resolve a band by enrollment ID, name or 8-hex label; omitted means
        the primary band (used for new records)."""
        bands = self.bands()
        if requested:
            match = [b for b in bands if requested in (b['id'], b['name'], b['label'])]
        else:
            match = [b for b in bands if b['primary']] or bands[:1]
        if len(match) != 1:
            raise ValueError('Unknown band; rook_knowledge(action="bands") lists them')
        return match[0]['id']

    def _band_for(self, band, rid):
        return self.resolve(band, rid, write=True)[0]

    def resolve(self, band, rid, write=False):
        """``(band, id, resolved)`` for a record id, slug or task/project
        name (KnowledgeStore.resolve) in ``band``, or in any band when
        omitted. ``resolved`` is set when a read took a near-miss name."""
        bands = [self.band(band)] if band else [b['id'] for b in self.bands()]
        return self.store.resolve(rid, bands, write=write)

    def _project_ref(self, band, ref):
        """``(project id, resolved)`` for a deck or board filter; a ref that
        is no record is left for the filter to report."""
        if not ref:
            return ref, None
        try:
            return self.resolve(band, ref)[1:]
        except Unresolved:
            raise
        except ValueError:
            return ref, None

    def _inline_handoff(self, band, actor, rid, data, note='inline handoff'):
        """Save data.handoff via the handoff store and link it to the task."""
        h = data.pop('handoff', None)
        if not h:
            return None
        if not self.handoffs:
            raise ValueError('Handoff store unavailable; save one with rook_handoff_save and link it')
        if not isinstance(h, dict) or not h.get('goal'):
            raise ValueError('data.handoff needs at least {goal, state, next_steps}')
        if not h.get('thread_id'):
            # Continue the task's own thread; a new one per update piles up
            # duplicates nobody can tell apart.
            with self.store.db(False) as db:
                mine = [l for l in self.store._live_links(db, rid) if l['kind'] == 'handoff']
            if mine:
                h = {**h, 'thread_id': mine[-1]['ref']}
        thread = self.handoffs(actor['id'], h)
        return self.store.mutate(band, actor, f'handoff:{thread}:{time.time_ns()}', 'link',
                                 {'id': rid, 'kind': 'handoff', 'ref': thread, 'relation': 'produced',
                                  'note': note})

    def _task_thread(self, rid):
        with self.store.db(False) as db:
            mine = [l for l in self.store._live_links(db, rid) if l['kind'] == 'handoff']
        return mine[-1]['ref'] if mine else None

    def board_handoffs(self, actor_id, tasks=None, reason='session ended'):
        """The automatic handoff (whiteboard.py): for each of ``actor_id``'s
        open claims (only ``tasks`` when given) whose whiteboard has work since
        the task's last handoff, save the board as the task's handoff and link
        it. Returns the task ids handed off. Bookkeeping: never raises."""
        done = []
        if not self.handoffs or not actor_id:
            return done
        try:
            claims = [c for c in self.store.active_claims() if c['actor'] == actor_id
                      and (tasks is None or c['task'] in tasks)]
        except Exception:
            log.exception('whiteboard handoff: claims lookup failed')
            return done
        for c in claims:
            try:
                h = self.store.board_handoff(c['task'], actor_id, reason)
                if h is None:
                    continue
                thread = self._task_thread(c['task'])
                tid = self.handoffs(actor_id, {**h, **({'thread_id': thread} if thread else {})})
                self.store.link_board_handoff(c['task'], actor_id, tid)
                done.append(c['task'])
            except Exception:
                log.exception('whiteboard handoff for %s failed', c['task'])
        return done

    def _deck(self, band, rid, data, fields, lean):
        """The deck, led by its totals: ``total_projects`` / ``total_tasks``
        (before paging), ``loose_tasks``, the ``filters`` applied, and
        ``offset`` / ``limit`` / ``next_offset`` (null on the last page).
        data {states, done_days, fields, project_fields, project, offset,
        limit (1..DECK_LIMIT), handoffs, outcome}."""
        bands = [b['id'] for b in self.bands()] if not band else [self.band(band)]
        full = data.get('outcome') == 'full'
        project, resolved = self._project_ref(band, rid or data.get('project'))
        done_days = data.get('done_days', 7)
        offset = max(0, int(data.get('offset') or 0))
        limit = None if data.get('limit') is None else max(1, min(int(data['limit']), DECK_LIMIT))
        states = _fields(data.get('states'))
        got = self.store.deck_page(bands, project=project, done_days=done_days, states=states,
                                   outcome_chars=None if full or not lean else MCP_OUTCOME_CHARS,
                                   offset=offset, limit=limit)
        if project and not got['total_projects']:
            raise ValueError(f'Project {project!r} not found')
        deck, loose = got['deck'], got.get('loose')
        wanted, heads = _fields(fields), _fields(data.get('project_fields'))
        filters = {'done_days': done_days}
        for key, value in (('states', states), ('project', project),
                           ('fields', wanted), ('project_fields', heads)):
            if value:
                filters[key] = value
        if wanted and 'all' not in wanted:
            keep = set(wanted) | {'id'}
            for entry in deck + ([loose] if loose else []):
                for key, items in entry.items():
                    if isinstance(items, list):
                        entry[key] = [{k: v for k, v in i.items() if k in keep} for i in items]
        if heads and 'all' not in heads:
            keep = set(heads) | {'id'}
            for entry in deck:
                entry['project'] = {k: v for k, v in entry['project'].items() if k in keep}
        total = got['total_projects']
        out = {'total_projects': total, 'total_tasks': got['total_tasks'],
               'loose_tasks': got.get('loose_tasks', 0),
               'filters': filters, 'offset': offset, 'limit': limit,
               'next_offset': offset + limit if limit and offset + limit < total else None,
               'deck': deck, **({'loose': loose} if loose else {}),
               **({'resolved': resolved} if resolved else {})}
        flags = self._hook('flags', bands) or {}
        if flags:
            for entry in deck + ([loose] if loose else []):
                if 'project' in entry and entry['project']['id'] in flags:
                    entry['project']['hygiene'] = flags[entry['project']['id']]
                for items in entry.values():
                    if isinstance(items, list):
                        for item in items:
                            if item.get('id') in flags:
                                item['hygiene'] = flags[item['id']]
        if data.get('handoffs'):
            if not self.handoff_list:
                raise ValueError('Handoff store unavailable; use rook_handoff_list')
            linked = self.store.handoff_tasks(bands)
            out['handoffs'] = [{k: t.get(k) for k in ('thread_id', 'goal', 'author', 'as_of', 'next_steps')}
                               | {'tasks': linked.get(t['thread_id'], [])} for t in self.handoff_list()]
        return out

    async def _board(self, band, rid, data):
        """Projects and tasks, flat, for the Work board, list and timeline
        (``KnowledgeStore.board``). ``id`` = a project: only it, plus its page
        extras (activity, linked rooms and knowledge). data {done_days (default
        30; null = all), archived}."""
        bands = [b['id'] for b in self.bands()] if not band else [self.band(band)]
        done_days = data.get('done_days', 30)
        rid, resolved = self._project_ref(band, rid)
        # Off the event loop: a big tree is a few thousand rows to walk.
        out = await asyncio.to_thread(self.store.board, bands, project=rid, archived=bool(data.get('archived')),
                                      done_days=None if done_days is None else float(done_days))
        if rid:
            if not out['projects']:
                raise ValueError(f'Project {rid!r} not found')
            p = out['projects'][0]
            out.update(await asyncio.to_thread(self.store.overview, p['band'], p['id'], data.get('activity', 25)))
            flags = self._hook('flags', bands) or {}
            for t in out['tasks']:
                if t['id'] in flags:
                    t['hygiene'] = flags[t['id']]
            if resolved:
                out['resolved'] = resolved
        return out

    def _related(self, band, got, linked, limit):
        """Stubs (with ``score``) of the wiki pages semantically nearest
        ``got``, leaving out the pages it is already structurally tied to
        (its links either way, parent and children): those are listed
        already, and ``related`` is for what nobody linked. Never fails the
        get: without vectors, or on any error, there are none."""
        try:
            limit = max(0, min(int(limit), MCP_GET_STUBS))
            skip = linked | {c['id'] for c in got.get('children', [])} | ({got['parent']} if got.get('parent') else set())
            near = self.search.related(band, got['id'], skip, limit)
            scores = dict(near)
            return [self.store.stub(r, score=round(scores[r['id']], 2))
                    for r in self.store.rows(band, [r for r, _ in near])]
        except Exception:
            log.exception('related pages failed for %s', got.get('id'))
            return []

    def _close_handoffs(self, band, actor, record):
        """A finished task takes its handoff threads with it, unless another
        open task still uses them. Bookkeeping: never fails the update."""
        if not self.handoff_close:
            return []
        try:
            with self.store.db(False) as db:
                threads = {l['ref'] for l in self.store._live_links(db, record['id']) if l['kind'] == 'handoff'}
            still = self.store.handoff_tasks([b['id'] for b in self.bands()])
            closed = []
            for thread in sorted(threads):
                if not any(t['id'] != record['id'] for t in still.get(thread, [])):
                    if self.handoff_close(thread, actor['id'], f"task {record['slug']} is {record['state']}"):
                        closed.append(thread)
            return closed
        except Exception:
            log.exception('closing handoff threads failed')
            return []

    async def _batch(self, band, kind, data, request_id, actor, lean):
        ops = data.get('ops')
        if not isinstance(ops, list) or not ops or len(ops) > BATCH_MAX:
            raise ValueError(f'batch needs data.ops: 1 to {BATCH_MAX} items of {{action, id?, data?}}')
        if not request_id:
            raise ValueError('request_id is required for writes')
        results = []
        for n, op in enumerate(ops):
            try:
                if not isinstance(op, dict) or op.get('action') not in BATCH_OPS:
                    raise ValueError('op.action must be one of ' + ', '.join(BATCH_OPS))
                results.append({'ok': True, 'result': await self.dispatch(
                    op['action'], op.get('band') or band, kind, op.get('id'), '', op.get('data'),
                    f'{request_id}:{n}', actor, lean)})
            except (ValueError, KeyError, TypeError, PermissionError) as error:
                results.append({'ok': False, 'error': str(error), 'code': type(error).__name__,
                                **({'current_revision': error.revision}
                                   if getattr(error, 'revision', None) is not None else {}),
                                **({'candidates': error.payload()['candidates']}
                                   if getattr(error, 'candidates', None) else {})})
        return {'results': results, 'failed': sum(1 for r in results if not r['ok'])}

    async def dispatch(self, action, band=None, kind=None, rid=None, query='', data=None,
                       request_id=None, actor=None, lean=False):
        """``lean`` (MCP callers): search/list default to fewer rows, excerpts
        and a small field set, overridable with data.limit / data.fields.

        One door for the hub caps, the MCP tools and the Knowledge page, so
        known vault values are masked here as {{secret:name}}: in what is
        written (the store masks again) and in what is returned, which covers
        records stored before a secret entered the vault."""
        return scrub(await self._dispatch(action, band, kind, rid, query, scrub(data), request_id, actor, lean))

    async def _dispatch(self, action, band=None, kind=None, rid=None, query='', data=None,
                        request_id=None, actor=None, lean=False):
        data = dict(data or {})
        fields = data.pop('fields', None) if action in ('search', 'list', 'deck') else None
        if action == 'bands':
            return self.bands()
        if action == 'deck':
            return self._deck(band, rid, data, fields, lean)
        if action == 'board':
            return await self._board(band, rid, data)
        if action == 'hygiene':
            if self.hygiene is None:
                return {'findings': [], 'enabled': False}
            who = self.actor()['id'] if data.get('mine') else None
            bands = [self.band(band)] if band else [b['id'] for b in self.bands()]
            records = None
            if rid:
                records = [self.resolve(band, rid)[1]]
            return {'findings': self.hygiene.open(records, who, data.get('limit', 50), bands=bands)}
        if action == 'review' and (actor or {}).get('kind') != 'human':
            raise PermissionError('review is for people, from the Knowledge page')
        actor = actor or self.actor()
        if action == 'batch':
            return await self._batch(band, kind, data, request_id, actor, lean)
        if action in ('whiteboard', 'jot') and not rid:
            # Your own board: this session's claim, else your newest.
            claim = self.store.current_claim(actor['id'])
            if claim is None:
                if action == 'jot':
                    raise ValueError('No claimed task: claim one (rook_task action=claim), then jot; '
                                     'or pass id=<task>')
                return {'boards': [], 'hint': 'No claimed task. Claim one: the hub keeps its whiteboard '
                                              'from your calls and their why.'}
            band, rid = claim['band'], claim['task']
        if action == 'whiteboard':
            b, rid, resolved = self.resolve(band, rid)
            got = self.store.whiteboards(b, rid, actor['id'], data.get('claims', 3),
                                         data.get('limit', whiteboard.WORK_SHOWN))
            return {**got, **({'resolved': resolved} if resolved else {})}
        if action in ('get', 'update', 'link', 'retract', 'claim', 'release', 'review', 'note') and not rid:
            raise ValueError(f'{action} needs id (a record id or slug; for retract, the link id)')
        resolved = None
        if action == 'retract':
            b = self.store.link_band(rid)
        elif rid:
            # Names resolve here, once: a write needs the exact name.
            b, rid, resolved = self.resolve(band, rid, write=action in WRITES)
        else:
            b = self.band(band)
        if action == 'list':
            if lean:
                data.setdefault('limit', MCP_LIST_LIMIT)
            records = [self.store.brief(r) for r in self.store.list(
                b, kind=kind, **{k: v for k, v in data.items() if k in ('worker', 'parent', 'state', 'limit', 'offset', 'attention')})]
            if lean or fields:
                records = project(records, fields, MCP_LIST_FIELDS if lean else ('all',))
            return {'records': records}
        if action == 'get':
            if not lean:
                got = self.store.get(b, rid)
            else:
                got = self.store.get(b, rid, auto_links=data.get('links') == 'all',
                                     events=data.get('events', MCP_GET_EVENTS), stubs=MCP_GET_STUBS)
                # A scan of the band's block vectors: off the event loop.
                related = await asyncio.to_thread(self._related, b, got, got.pop('_linked'),
                                                  data.get('related', RELATED_MAX))
                if related:
                    got['related'] = related
            found = self._hook('open', [got['id']])
            if found:
                got['hygiene'] = [{k: f[k] for k in ('kind', 'actor', 'text', 'created')} for f in found]
            if resolved:
                got['resolved'] = resolved
            return got
        if action == 'search':
            found = await self.search.query(b, query, kind, data.get('worker'),
                                            int(data.get('limit', MCP_SEARCH_LIMIT if lean else 20)))
            named = self.store.names_for(r['id'] for r in found['results'] if r.get('kind') in ('task', 'project'))
            for r in found['results']:
                if r['id'] in named:
                    r['name'] = named[r['id']]
            if lean or fields:
                found['results'] = project(found['results'], fields, MCP_SEARCH_FIELDS if lean else ('all',))
            if lean:
                found = {k: v for k, v in found.items() if v is not None and v is not False}
            return found
        if action == 'context':
            return self.store.context(b, data.get('worker'))
        if action == 'status':
            with self.store.db(False) as db:
                counts = {r['kind']: r['n'] for r in db.execute('SELECT kind,count(*) n FROM records WHERE band=? GROUP BY kind', (b,))}
            return {'band': b, 'counts': counts, 'semantic_configured': self.search.configured,
                    'semantic_error': self.search.last_error, 'last_maintenance': self.last_maintenance,
                    'maintenance_error': self.last_error}
        if action not in WRITES:
            raise ValueError('Actions: bands, deck, board, hygiene, list, get, search, context, status, '
                             'whiteboard, create, update, link, retract, claim, release, note, jot, batch')
        if action == 'review':
            data = {k: data.get(k) for k in ('revision', 'verdict', 'note')}
        pr_notes = None
        if action in ('update', 'release'):
            record = self.store.get(b, rid)
            note = 'inline handoff'
            to = (data.get('patch') or {}).get('state') if isinstance(data.get('patch'), dict) else None
            if action == 'update' and to == 'done' and record['kind'] == 'task' and record['state'] != 'done':
                pr_notes = await self._pr_done_check(record, actor)
            stopping = action == 'release' or (record['state'] == 'in_progress' and to in ('paused', 'blocked', 'todo'))
            if stopping and not data.get('handoff'):
                # No handoff given: the whiteboard of the claim being stopped
                # is the handoff, when it has work since the last one. A
                # release's own claim; a state change's caller's, else the
                # claim active most recently (a person stopping an agent's work).
                if action == 'release':
                    h = self.store.board_handoff(record['id'], str(data.get('actor') or '').strip()[:200]
                                                 or actor['id'], 'claim released')
                else:
                    h = (self.store.board_handoff(record['id'], actor['id'], f'set {to}')
                         or self.store.board_handoff(record['id'], None, f'set {to}'))
                if h:
                    data['handoff'], note = h, 'automatic handoff from the whiteboard'
            self._inline_handoff(b, actor, record['id'], data, note)
        if rid:
            data['id'] = rid
        if kind and action == 'create':
            data['kind'] = kind
        if action == 'retract':
            data.setdefault('link', rid)
        result = self.store.mutate(b, actor, request_id, action, data)
        if action == 'claim' or (action in ('jot', 'note') and self.store.claimed_by(actor['id'], rid)):
            # This session's calls now go to that claim (a session that
            # reconnected after its idle timeout points itself again this way).
            whiteboard.bind(result.get('task') or rid)
        elif action == 'release' and not data.get('actor'):
            whiteboard.bind(None, unless=result['task'])
        if action == 'update' and result.get('kind') == 'task' and result.get('state') in CLOSED_TASK \
                and record['state'] not in CLOSED_TASK:
            closed = self._close_handoffs(b, actor, result)
            if closed:
                result = {**result, 'closed_handoffs': closed}
        self._after_write(action, actor, data, result, record if action in ('update', 'release') else None)
        if pr_notes:
            result = {**result, 'pr_check': pr_notes}
        if action == 'create':
            result = {**result, 'comparable': [
                {'id': r['id'], 'slug': r['slug'], 'title': r['title'], 'kind': r['kind'],
                 **({'name': r['name']} if r.get('name') else {})}
                for r in self.store.lexical(b, result['title'], 5) if r['id'] != result['id']]}
        return result

    async def _pr_done_check(self, record, actor):
        """A task going done while a pull request it links is still open
        (prs.py): refused for an agent, a note for a person (people decide).
        GitHub unreachable or no client: a note, never a refusal. Returns the
        notes for the reply (``pr_check``), or None."""
        prs = self.prs
        if prs is None:
            return None
        try:
            got = await asyncio.to_thread(prs.done_check, record['id'])
        except Exception:
            log.exception('PR done check for %s failed', record.get('slug'))
            return ['could not check the linked pull requests; make sure they merged']
        if got.get('refuse'):
            if (actor or {}).get('kind') != 'human':
                raise ValueError(got['refuse'])
            return [got['refuse']] + list(got.get('notes') or [])
        return list(got.get('notes') or []) or None

    def _after_write(self, action, actor, data, result, before):
        """Hygiene triggers for a write that went through."""
        if self.hygiene is None or not isinstance(result, dict):
            return
        if action == 'link':
            self._hook('on_link', actor, result.get('record'), result.get('kind'), result.get('ref'),
                       result.get('relation'))
        elif action == 'update':
            self._hook('on_record_changed', actor, before, result)
        elif action == 'claim':
            self._hook('on_claim', actor.get('id'), result.get('task'))
        elif action == 'release':
            self._hook('on_release', result.get('task'), data.get('actor') or actor.get('id'))
        elif action == 'note' and data.get('session_end'):
            # A note saying a session working on the task ended (the Sessions
            # page posts one): ask for a handoff.
            self._hook('session_end_prompt', result.get('id'), str(data['session_end'])[:200], actor)
        # A new or changed knowledge page, or a new link, can cover a done
        # task; task writes, claims and retractions cannot.
        if action == 'link' or (action in ('create', 'update') and result.get('kind') == 'knowledge'):
            self._hook('after_knowledge_write')

    async def maintain(self):
        """Background embedding indexing. Failures are recorded, never raised."""
        while True:
            try:
                await self.search.index_batch()
                self.last_maintenance = time.time()
                self.last_error = None
            except Exception as error:
                self.last_error = type(error).__name__
                log.exception('knowledge maintenance failed')
            await asyncio.sleep(30)
