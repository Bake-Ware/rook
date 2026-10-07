"""Durable work review, worker history discovery, and agent process transport."""
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

import aiohttp
from aiohttp import web, WSMsgType
from .account_web import COOKIE, NO_STORE
from .term_hub import TermHub, Viewer, frame

log = logging.getLogger(__name__)


# Kept as an import for callers of the former web projection helper.
from ..worker.work_runtime import project

METADATA_KEYS = frozenset(('id', 'owner', 'title', 'cwd', 'model', 'worker_id',
    'worker_name', 'band', 'agent', 'imported', 'source_id', 'source_version',
    'source_updated', 'last_activity', 'message_count', 'thread_id', 'turn_id', 'status',
    'review_status', 'revision', 'updated', 'error', 'disconnected',
    'external_handle', 'external_cursor', 'resume_note', 'remote_runtime',
    'worker_revision', 'running', 'needs_input', 'legacy_runtime', 'active', 'messageable', 'message_note',
    'created_by', 'harness', 'term_id', 'term_running', 'term_exit', 'term_note',
    'term_started', 'mcp_token_id', 'mcp_token_revoke', 'persona', 'task'))

HARNESSES = ('shell', 'claude', 'codex', 'hermes')
TERM_CAPS = ('work.stream.open', 'work.stream.read', 'work.stream.write')
VENDOR = {'xterm.mjs': 'application/javascript', 'xterm.css': 'text/css',
          'addon-fit.mjs': 'application/javascript'}
# Scoped MCP tokens minted for launched agents. Until the permissions layer
# lands the scope tag is attribution only: the token can call what any
# operator API token can. See docs/web/worklog.md.
SESSION_TOKEN_TTL = 86400
INPUT_FRAME_MAX = 96 * 1024
# The merged Sessions catalog (docs/design/sessions.md §3.6).
CATALOG_LIMIT = 50                  # sessions asked of each worker by default
CATALOG_LIMIT_MAX = 200
CATALOG_TIMEOUT = 15
# The Sessions page's per-session routes, POST /account/work/session/<op>
# (docs/design/sessions.md §3.6).
SESSION_OPS = ('mirror', 'follow', 'send', 'stop', 'resume', 'new', 'attach', 'link')
MIRROR_WAIT_MAX = 20                # seconds a mirror long-poll may hold
MIRROR_WATCHERS = 32                # long-polls held at once, hub-wide (bounded memory)
MIRROR_EVENTS_MAX = 500
TEXT_MAX = 24000
TASK_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$')


class HostError(Exception):
    """The worker failed or refused; the page shows the message."""


def check_cwd(value):
    """An absolute directory on the host (POSIX, or a Windows drive path)."""
    cwd = str(value or '').strip()
    if len(cwd) > 2000 or not (cwd.startswith('/') or re.match(r'^[A-Za-z]:[\\/]', cwd)):
        raise ValueError('Enter an absolute working directory.')
    return cwd


def check_task(value):
    """A Rook task id or slug, or '' for none."""
    task = str(value or '').strip()
    if task and not TASK_RE.match(task):
        raise ValueError('Enter a task id (t_…) or slug.')
    return task



def actor(user):
    """Audit identity for a signed-in account, stamped on band calls."""
    return 'human:' + str(user.get('username') or user['id'])


def legacy_records(items, live):
    """§3.1 records from an older worker's ``work.sessions`` shape (history
    ``items`` plus ``live`` terminals) or its ``*-history.pull`` entries.
    What it cannot know stays conservative: no mirror, unknown inbox policy."""
    terms = {}
    for t in live:
        if not isinstance(t, dict) or not t.get('id'):
            continue
        agent = t.get('harness') if t.get('harness') in HARNESSES else 'shell'
        native = str(t.get('resume') or t['id'])
        if (agent, native) not in terms or t.get('running'):
            terms[(agent, native)] = t
    out, seen = [], set()
    for i in items:
        agent, native = i.get('agent'), i.get('session_id')
        if agent not in ('claude', 'codex') or not native:
            continue
        seen.add((agent, native))
        term = terms.get((agent, native))
        running = bool(term and term.get('running'))
        active = bool(i.get('active')) or running
        state = 'closed' if not active else 'idle' if i.get('activity') == 'ready' and not running else 'live'
        inbox = active and bool(i.get('messageable'))
        out.append({'agent': agent, 'native_id': native, 'title': i.get('title') or native,
                    'cwd': i.get('cwd'), 'state': state, 'origin': 'rook' if term else 'external',
                    'updated': i.get('updated'), 'messages': i.get('messages'),
                    'view': {'terminal': term['id'] if term else None, 'mirror': False, 'transcript': True},
                    'input': 'pty' if running else 'inbox' if inbox else 'none',
                    'inbox_policy': 'unknown', 'links': {}, 'resumable': state == 'closed',
                    **({'activity': i['activity']} if i.get('activity') else {})})
    for (agent, native), t in terms.items():
        if (agent, native) in seen:
            continue
        running = bool(t.get('running'))
        transcript = agent in ('claude', 'codex') and native != t['id']
        out.append({'agent': agent, 'native_id': native, 'title': t.get('title') or native,
                    'cwd': t.get('cwd'), 'state': 'live' if running else 'closed', 'origin': 'rook',
                    'updated': t.get('last_output') or t.get('started'), 'messages': None,
                    'view': {'terminal': t['id'], 'mirror': False, 'transcript': transcript},
                    'input': 'pty' if running else 'none', 'inbox_policy': 'unknown',
                    'links': {'work_session': t['session']} if t.get('session') else {},
                    'resumable': not running and transcript})
    return out


def filter_records(items, query, live_only):
    q = (query or '').lower()
    return [i for i in items
            if (not live_only or i.get('state') in ('live', 'idle'))
            and (not q or q in f"{i.get('title')} {i.get('cwd')} {i.get('native_id')}".lower())]


class WorkStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS work_sessions(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, updated REAL NOT NULL,
                    state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_index(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, updated REAL NOT NULL,
                    state TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS work_index_owner ON work_index(owner, updated);
                INSERT OR IGNORE INTO work_index
                    SELECT id, owner, updated, json_remove(state, '$.items', '$.order',
                        '$.terminal', '$.diff', '$.rpc', '$.pending', '$.partial') FROM work_sessions
                    WHERE NOT EXISTS (SELECT 1 FROM work_index WHERE work_index.id=work_sessions.id);
                CREATE TABLE IF NOT EXISTS work_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                    created REAL NOT NULL, event TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS work_events_session ON work_events(session,seq);
                CREATE TABLE IF NOT EXISTS work_commands(
                    session TEXT NOT NULL, id TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(session,id));
                CREATE TABLE IF NOT EXISTS session_links(
                    owner TEXT NOT NULL, key TEXT NOT NULL, task TEXT NOT NULL,
                    updated REAL NOT NULL, PRIMARY KEY(owner,key));
            """)
            # Audit attribution: which account submitted each command.
            if 'actor' not in {r[1] for r in db.execute('PRAGMA table_info(work_commands)')}:
                db.execute('ALTER TABLE work_commands ADD COLUMN actor TEXT')
            # A restarted web process cannot know whether the host received an
            # unfinished command. Keep its ID claimed and require human review.
            db.execute("UPDATE work_commands SET result=? WHERE json_extract(result, '$.status')='accepted'",
                       (json.dumps({'status': 'error', 'error': 'Submission was interrupted by a server restart. Check the host before retrying.'}),))
            # Retain legacy native state until the host acknowledges its archive.
            db.execute("UPDATE work_sessions SET state=json_set(state, '$.legacy_runtime', 1) WHERE COALESCE(json_extract(state, '$.imported'),0)=0 AND COALESCE(json_extract(state, '$.remote_runtime'),0)=0 AND json_type(state, '$.items') IS NOT NULL")
            # Remove copies written by the earlier import implementation.
            for table in ('work_sessions', 'work_index'):
                db.execute("UPDATE " + table + " SET state=json_remove(state, '$.items', '$.order', '$.terminal', '$.history_loading') WHERE json_extract(state, '$.imported')=1 AND (json_type(state, '$.items') IS NOT NULL OR json_type(state, '$.terminal') IS NOT NULL OR json_type(state, '$.history_loading') IS NOT NULL)")
            for row in db.execute('SELECT id,owner,updated,state FROM work_sessions').fetchall():
                metadata = self.metadata(json.loads(row['state']))
                db.execute('INSERT OR REPLACE INTO work_index VALUES(?,?,?,?)',
                           (row['id'], row['owner'], row['updated'], json.dumps(metadata)))
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, sid, owner=None):
        with self.db() as db:
            row = db.execute('SELECT * FROM work_index WHERE id=?', (sid,)).fetchone()
        if row is None or (owner is not None and row['owner'] != owner):
            raise web.HTTPNotFound()
        return self.metadata(json.loads(row['state']))

    @staticmethod
    def metadata(state):
        return {k: v for k, v in state.items() if k in METADATA_KEYS}

    def archive(self, sid):
        with self.db() as db:
            state = json.loads(db.execute('SELECT state FROM work_sessions WHERE id=?', (sid,)).fetchone()[0])
            events = [dict(r) for r in db.execute('SELECT * FROM work_events WHERE session=?', (sid,))]
            commands = [dict(r) for r in db.execute('SELECT id,result,actor FROM work_commands WHERE session=?', (sid,))]
        return dict(state=state, events=events, commands=commands)

    def all(self, owner=None, *, details=True):
        with self.db() as db:
            table = 'work_index'
            rows = db.execute('SELECT state FROM ' + table + ' ' +
                              ('WHERE owner=? ' if owner else '') + 'ORDER BY updated DESC',
                              (owner,) if owner else ()).fetchall()
        return [self.metadata(json.loads(r['state'])) for r in rows]

    def revision(self, sid, owner):
        with self.db() as db:
            row = db.execute('SELECT state FROM work_index WHERE id=? AND owner=?', (sid, owner)).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        return json.loads(row['state'])['revision']

    def save(self, state, event=None):
        state['updated'] = time.time()
        state['revision'] = state.get('revision', 0) + 1
        state = self.metadata(state)
        with self.db() as db:
            if state.get('legacy_runtime'):
                db.execute('UPDATE work_sessions SET updated=?,state=json_patch(state,?) WHERE id=?',
                           (state['updated'], json.dumps(state), state['id']))
            else:
                db.execute('INSERT OR REPLACE INTO work_sessions VALUES(?,?,?,?)',
                           (state['id'], state['owner'], state['updated'], json.dumps(state)))
                db.execute('DELETE FROM work_events WHERE session=?', (state['id'],))
            db.execute('INSERT OR REPLACE INTO work_index VALUES(?,?,?,?)',
                       (state['id'], state['owner'], state['updated'], json.dumps(state)))

    def set_task(self, owner, key, task):
        """Link a host session (by its catalog key) to a task; '' unlinks."""
        with self.db() as db:
            if task:
                db.execute('INSERT OR REPLACE INTO session_links VALUES(?,?,?,?)',
                           (owner, key, task, time.time()))
            else:
                db.execute('DELETE FROM session_links WHERE owner=? AND key=?', (owner, key))

    def tasks(self, owner):
        with self.db() as db:
            return {r['key']: r['task'] for r in
                    db.execute('SELECT key,task FROM session_links WHERE owner=?', (owner,))}

    def claim(self, sid, cid, actor=None):
        with self.db() as db:
            cur = db.execute('INSERT OR IGNORE INTO work_commands(session,id,result,actor) VALUES(?,?,?,?)',
                             (sid, cid, json.dumps({'status': 'accepted'}), actor))
            return cur.rowcount == 1

    def result(self, sid, cid, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('UPDATE work_commands SET result=? WHERE session=? AND id=?',
                           (json.dumps(value), sid, cid))
            row = db.execute('SELECT result FROM work_commands WHERE session=? AND id=?',
                             (sid, cid)).fetchone()
        return json.loads(row['result']) if row else None


class WorkWeb:
    def __init__(self, account):
        self.account = account
        self.server = account.server
        self.store = WorkStore(account.store.path.with_name('work.sqlite3'))
        self.locks = {}
        self.jobs = set()
        self.pump = None
        self.discovery = None
        self.external_output = {}
        # Sessions page with live terminals. ROOK_WORK_V2=0 keeps only the classic view.
        self.v2 = os.environ.get('ROOK_WORK_V2', '1') != '0'
        self.terms = TermHub(lambda: self.server._band, on_end=self.term_ended)
        self.token_url = os.environ.get('ROOK_TOKEN_ADMIN_URL', 'http://127.0.0.1:8765/tokens/account-api')
        self.mcp_url = os.environ.get('ROOK_WORK_MCP_URL', '')
        self._ticks = 0
        # worker_id -> the latest unfiltered catalog fetched from it, served
        # (marked stale) when the worker does not answer.
        self.catalogs = {}
        self.watching = 0             # mirror long-polls held right now

    def lock(self, sid):
        return self.locks.setdefault(sid, asyncio.Lock())

    def install(self, app):
        app.router.add_get('/account/work/bootstrap', self.bootstrap)
        app.router.add_get('/account/work/history/{session}', self.history_page)
        app.router.add_get('/account/work/view/{session}', self.view_page)
        app.router.add_get('/account/work/ws', self.websocket)
        app.router.add_get('/account/work/assets/{name}', self.asset)
        app.router.add_get('/account/work/assets/vendor/{name}', self.vendor_asset)
        app.router.add_get('/account/work/term/{session}', self.term_socket)
        app.router.add_get('/account/work/sessions', self.sessions_list)
        app.router.add_post('/account/work/session/{op}', self.session_op)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)

    async def start(self, app):
        self.pump = asyncio.create_task(self.collect())
        self.discovery = asyncio.create_task(self.discover())

    async def stop(self, app):
        await self.terms.stop()
        if self.discovery:
            self.discovery.cancel()
        if self.pump:
            self.pump.cancel()
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*(list(self.jobs) + ([self.pump] if self.pump else []) + ([self.discovery] if self.discovery else [])),
                             return_exceptions=True)

    def user(self, request):
        user = self.account.current(request)
        if not user:
            raise web.HTTPUnauthorized()
        if not user['admin']:
            raise web.HTTPForbidden()
        return user

    def workers(self, history=False):
        band = self.server._band
        if band is None:
            return []
        return [w for w in band.workers.values()
                if (history or all(c in w.get('caps', []) for c in ('work.create', 'work.command', 'work.view_page', 'work.status')))
                and time.time() - w.get('last_seen', 0) < 90
                and not self.server._ban_match(w.get('name'), w['worker_id'])]

    def worker(self, state):
        found = next((w for w in self.workers(history=True) if w['worker_id'] == state['worker_id']
                      and w.get('band') == state['band']), None)
        if found is None:
            raise ValueError('Host disconnected. Connect the host to read or resume this session.')
        return found

    async def rpc(self, state, cap, args, timeout=12, identity='system:work'):
        self.worker(state)
        reply = await self.server._band.call(cap=cap, args=args, target=state['worker_id'],
                                             timeout=timeout, identity=identity)
        if not reply.get('ok') or reply.get('from') != state['worker_id']:
            raise ValueError(reply.get('error') or 'Worker did not acknowledge the request.')
        result = reply.get('result') or {}
        if result.get('ok') is False:
            raise ValueError(result.get('error') or 'Worker operation failed.')
        return result

    async def asset(self, request):
        name = request.match_info['name']
        if name not in ('work.js', 'work.css', 'worklog.js', 'sessions.js', 'sessions.css'):
            raise web.HTTPNotFound()
        return web.Response(text=(Path(__file__).parents[1] / 'web' / name).read_text(),
                            content_type='text/css' if name.endswith('css') else 'application/javascript',
                            headers=NO_STORE)

    async def vendor_asset(self, request):
        name = request.match_info['name']
        if name not in VENDOR:
            raise web.HTTPNotFound()
        path = Path(__file__).parents[1] / 'web' / 'vendor' / 'xterm' / name
        # Versioned third-party files: cacheable, unlike the app's own assets.
        return web.Response(body=path.read_bytes(), content_type=VENDOR[name],
                            headers={'Cache-Control': 'public, max-age=86400'})

    async def bootstrap(self, request):
        user = self.user(request)
        return web.json_response({'csrf': user['csrf'], 'v2': self.v2,
                                  'harnesses': list(HARNESSES)}, headers=NO_STORE)

    def hosts(self):
        """Connected workers that can run Work sessions of either kind."""
        out = []
        for w in self.workers(history=True):
            caps = w.get('caps', [])
            term = all(c in caps for c in TERM_CAPS)
            runtime = all(c in caps for c in ('work.create', 'work.command', 'work.view_page', 'work.status'))
            history = [a for a in ('claude', 'codex') if a + '-history.pull' in caps]
            if not (term or runtime or history):
                continue
            hb = (w.get('hb') or {}).get('work') or {}
            harnesses = [h for h in hb.get('harnesses', []) if h in HARNESSES] if term else []
            out.append({'id': w['worker_id'], 'name': w.get('name', ''), 'band': w.get('band'),
                        'term': term, 'runtime': runtime,
                        'harnesses': harnesses or (list(HARNESSES) if term else []),
                        'history': history})
        return out

    def term_capable(self, state):
        try:
            caps = self.worker(state).get('caps', [])
        except ValueError:
            return False
        return all(c in caps for c in TERM_CAPS)

    # -- scoped MCP tokens ---------------------------------------------------

    async def token_call(self, request, user, payload):
        """Operate the MCP process's token store as the signed-in operator."""
        token = request.cookies.get(COOKIE, '')
        if request.headers.get('Authorization', '').startswith('Bearer '):
            token = request.headers['Authorization'][7:]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(self.token_url, json={**payload, 'csrf': user['csrf']},
                                        cookies={COOKIE: token}, allow_redirects=False) as upstream:
                    result = await upstream.json()
                    if upstream.status != 200:
                        raise ValueError(result.get('error') or 'Token service refused the request.')
                    return result
        except (aiohttp.ClientError, TimeoutError) as error:
            raise ValueError('Token service is unavailable; launch without a Rook MCP token.') from error

    async def mint_session_token(self, request, user, sid, harness):
        result = await self.token_call(request, user, {
            'op': 'create', 'name': f'work:{harness}:{sid[:8]}', 'ttl': SESSION_TOKEN_TTL,
            'scopes': ['rook', 'work-session:' + sid], 'role': 'agent'})
        return result['id'], result['token']

    async def revoke_session_tokens(self, request, user):
        """Revoke tokens of finished launches, using an operator's live socket
        (the token store only accepts operator-authenticated changes)."""
        for s in self.store.all(user['id'], details=False):
            if not s.get('mcp_token_revoke') or not s.get('mcp_token_id'):
                continue
            try:
                await self.token_call(request, user, {'op': 'revoke', 'id': s['mcp_token_id'], 'confirm': True})
            except ValueError as error:
                if 'no longer exists' not in str(error):
                    log.warning('Token revoke for %s deferred: %s', s['id'], error)
                    continue
            async with self.lock(s['id']):
                latest = self.store.get(s['id'])
                latest.update(mcp_token_id=None, mcp_token_revoke=False)
                self.store.save(latest)

    def mcp_endpoint(self):
        return self.mcp_url or (self.account.origin + '/mcp')

    # -- live terminals ------------------------------------------------------

    def term_ended(self, stream):
        """A followed terminal exited or was lost: record it on its session."""
        for s in self.store.all(details=False):
            if s.get('term_id') == stream.term_id and s.get('worker_id') == stream.worker_id and s.get('term_running'):
                self.mark_term_done(s['id'], stream.exit_code, stream.lost)

    def mark_term_done(self, sid, exit_code, note=''):
        s = self.store.get(sid)
        if not s.get('term_running'):
            return
        s.update(term_running=False, term_exit=exit_code, term_note=note or '',
                 last_activity=time.time())
        if not s.get('imported'):
            s['status'] = 'closed'
        if s.get('mcp_token_id'):
            s['mcp_token_revoke'] = True
        self.store.save(s)

    async def term_socket(self, request):
        """One live terminal: binary output frames out, JSON control in."""
        user = self.user(request)
        if request.headers.get('Origin') != self.account.origin:
            raise web.HTTPForbidden(text='Origin mismatch')
        s = self.store.get(request.match_info['session'], user['id'])
        if not s.get('term_id'):
            raise web.HTTPNotFound(text='This session has no live terminal.')
        existing = self.terms.get(s['worker_id'], s['term_id'])
        if existing is None:
            if not s.get('term_running'):
                raise web.HTTPGone(text='This terminal has ended.')
            try:
                self.worker(s)
            except ValueError as error:
                raise web.HTTPServiceUnavailable(text=str(error))
        try:
            stream = existing or self.terms.stream(s['worker_id'], s['term_id'])
        except ValueError as error:
            raise web.HTTPServiceUnavailable(text=str(error))
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=INPUT_FRAME_MAX)
        await ws.prepare(request)
        viewer = Viewer(str(user.get('username') or user['id'])[:60])
        since = request.query.get('since')
        try:
            stream.attach(viewer, int(since) if since and since.isdigit() else None)
        except ValueError as error:
            await ws.send_json({'type': 'error', 'error': str(error)})
            await ws.close()
            return ws

        async def writer():
            while not ws.closed:
                kind, item = await viewer.next()
                if kind == 'frame':
                    await ws.send_bytes(item)
                elif kind == 'resync':
                    viewer.resync = False
                    await ws.send_json({'type': 'reset', 'start': stream.start})
                    if stream.ring:
                        await ws.send_bytes(frame(stream.start, bytes(stream.ring)))
                    viewer.sent = stream.end
                else:
                    await ws.send_json(item)

        pump = asyncio.create_task(writer())
        checked = time.monotonic()
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                try:
                    # Revocation applies to open sockets within a few seconds
                    # without a session lookup on every keystroke.
                    if time.monotonic() - checked > 5:
                        user, checked = self.user(request), time.monotonic()
                    data = json.loads(msg.data)
                    if not isinstance(data, dict):
                        raise ValueError('Expected an object.')
                    self.account.csrf(request, data, user)
                    stream.control(viewer, data)
                    if data.get('op') == 'input':
                        self.touch(s['id'])
                except (ValueError, TypeError, PermissionError, json.JSONDecodeError) as error:
                    await ws.send_json({'type': 'error', 'error': str(error)})
                except (web.HTTPUnauthorized, web.HTTPForbidden):
                    await ws.close(code=1008, message=b'Sign in again')
                    break
        finally:
            stream.detach(viewer)
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        return ws

    def touch(self, sid):
        """Record activity at most every 30 s (it bumps the sidebar order)."""
        s = self.store.get(sid)
        if time.time() - (s.get('last_activity') or 0) > 30:
            s['last_activity'] = time.time()
            self.store.save(s)

    async def open_terminal(self, request, user, sid, cid, data):
        """Launch (or resume into) a PTY on the session's worker."""
        async with self.lock(sid):
            s = self.store.get(sid)
            token_id = None
            try:
                args = dict(harness=s['harness'], cwd=s.get('cwd') or '', title=s.get('title') or '',
                            model=s.get('model') or '', session=sid,
                            cols=int(data.get('cols') or 120), rows=int(data.get('rows') or 32))
                if s.get('imported') and s.get('source_id'):
                    args['resume'] = s['source_id']
                if s.get('persona'):
                    args['persona'] = s['persona']
                if data.get('mcp'):
                    token_id, secret = await self.mint_session_token(request, user, sid, s['harness'])
                    args.update(mcp_url=self.mcp_endpoint(), mcp_token=secret)
                result = await self.rpc(s, 'work.stream.open', args, timeout=40, identity=actor(user))
                s = self.store.get(sid)
                s.update(term_id=result['id'], term_running=True, term_exit=None, term_note='',
                         term_started=time.time(), error='', review_status=None,
                         last_activity=time.time(), mcp_token_id=token_id, mcp_token_revoke=False)
                if not s.get('imported'):
                    s['status'] = 'working'
                self.store.save(s)
                self.store.result(sid, cid, {'status': 'submitted'})
            except Exception as error:
                s = self.store.get(sid)
                s['error'] = str(error) or type(error).__name__
                if not s.get('imported'):
                    s['status'] = 'closed'
                if token_id:
                    s.update(mcp_token_id=token_id, mcp_token_revoke=True)
                self.store.save(s)
                self.store.result(sid, cid, {'status': 'error', 'error': s['error']})
                log.warning('Terminal launch for %s failed: %s', sid, error)

    def launch_terminal(self, request, user, sid, cid, data):
        job = asyncio.create_task(self.open_terminal(request, user, sid, cid, data))
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return job

    def new_launch(self, request, user, cid, data):
        """Record and start a new terminal session (the ``launch`` op of the
        Work socket and the Sessions page's New session). Idempotent per
        command id: returns ``(session id, launch job or None when that
        command already ran)``."""
        if not self.v2:
            raise ValueError('Live terminals are disabled on this hub.')
        sid = uuid.uuid5(uuid.NAMESPACE_URL, user['id'] + ':' + cid).hex
        try:
            self.store.get(sid, user['id'])
            return sid, None
        except web.HTTPNotFound:
            pass
        w = next((h for h in self.hosts() if h['id'] == data.get('worker') and h['term']), None)
        if not w:
            raise ValueError('Choose a connected host that supports live terminals.')
        harness = data.get('harness')
        if harness not in HARNESSES:
            raise ValueError('Choose claude, codex, hermes or shell.')
        if harness not in w['harnesses']:
            raise ValueError(f"{harness} is not installed on {w.get('name') or 'that host'}.")
        cwd = check_cwd(data.get('cwd'))
        task = check_task(data.get('task'))
        folder = re.split(r'[\\/]', cwd.rstrip('/\\'))[-1] or cwd
        title = str(data.get('title') or '').strip()[:160] or f'{harness} · {folder}'
        s = dict(id=sid, owner=user['id'], title=title, worker_id=w['id'],
                 worker_name=w.get('name'), band=w.get('band'), cwd=cwd,
                 model=str(data.get('model') or '')[:100], agent=harness, harness=harness,
                 persona=str(data.get('persona') or '')[:100] or None, task=task or None,
                 status='starting', thread_id=None, turn_id=None, error='',
                 created_by=actor(user), term_running=False)
        self.store.save(s)
        self.store.claim(sid, cid, actor(user))
        return sid, self.launch_terminal(request, user, sid, cid, data)

    async def close_terminal(self, s):
        if s.get('term_id') and s.get('term_running'):
            stream = self.terms.get(s['worker_id'], s['term_id'])
            try:
                result = await self.rpc(s, 'work.stream.close', {'id': s['term_id']}, timeout=20)
                code = result.get('exit_code')
            except ValueError as error:
                if 'no such terminal' not in str(error):
                    raise
                code = None
            if stream is not None:
                stream.finish(code)
            self.mark_term_done(s['id'], code)

    async def history_page(self, request):
        user = self.user(request)
        s = self.store.get(request.match_info['session'], user['id'])
        if not s.get('imported'):
            raise web.HTTPBadRequest(text='This session uses the live Work conversation.')
        try:
            offset = int(request.query.get('offset', '0'))
            content_offset = int(request.query.get('content_offset', '0'))
            if offset < 0 or content_offset < 0:
                raise ValueError('Invalid history cursor.')
            caps = self.worker(s).get('caps', [])
            if request.query.get('follow') == '1':
                cap = s['agent'] + '-history.follow'
                if cap not in caps:
                    return web.json_response({'unchanged': True, 'live': False}, headers=NO_STORE)
                page = await self.rpc(s, cap, dict(session_id=s['source_id'], offset=offset,
                    version=request.query.get('version', '')), identity=actor(user))
                version = str(page.get('version', '')).split(':')
                if len(version) == 3 and version[2].isdigit():
                    activity = int(version[2]) / 1_000_000_000
                    async with self.lock(s['id']):
                        latest = self.store.get(s['id'], user['id'])
                        if activity > (latest.get('source_updated') or 0):
                            latest['source_updated'] = activity
                            self.store.save(latest)
                return web.json_response(page, headers=NO_STORE)
            cap = s['agent'] + '-history.read_snapshot'
            args = dict(session_id=s['source_id'], offset=offset, content_offset=content_offset)
            if cap in caps:
                args['snapshot'] = request.query.get('snapshot', '')
            else:
                cap = s['agent'] + '-history.read_page'
            if cap not in caps:
                raise ValueError('Update this worker to enable transcript reads.')
            page = await self.rpc(s, cap, args, identity=actor(user))
            # Relay a single bounded page. Never persist transcript content.
            return web.json_response(page, headers=NO_STORE)
        except (ValueError, TimeoutError) as error:
            return web.json_response({'error': str(error) or 'Host request timed out.'},
                                     status=503, headers=NO_STORE)

    async def view_page(self, request):
        user = self.user(request)
        s = self.store.get(request.match_info['session'], user['id'])
        if not s.get('remote_runtime'):
            raise web.HTTPBadRequest(text='Host runtime is not available for this session yet.')
        try:
            page = await self.rpc(s, 'work.view_page', dict(session_id=s['id'],
                since=max(0, int(request.query.get('since', '0'))),
                token=request.query.get('token', ''), offset=max(0, int(request.query.get('offset', '0')))),
                identity=actor(user))
            return web.json_response(page, headers=NO_STORE)
        except (ValueError, TimeoutError) as error:
            return web.json_response({'error': str(error) or 'Host request timed out.'}, status=503, headers=NO_STORE)

    # -- merged session catalog (docs/design/sessions.md §3.6) -----------------

    async def sessions_list(self, request):
        """Every connected worker's sessions as §3.1 records, merged:
        ``GET /account/work/sessions?query=&live_only=&limit=&worker=``."""
        user = self.user(request)
        query = request.query.get('query', '').strip()[:200]
        live_only = request.query.get('live_only', '').lower() in ('1', 'true', 'yes', 'on')
        try:
            limit = max(1, min(int(request.query.get('limit', CATALOG_LIMIT)), CATALOG_LIMIT_MAX))
        except ValueError:
            return web.json_response({'error': 'limit must be a number'}, status=400, headers=NO_STORE)
        only = request.query.get('worker', '')
        targets = [w for w in self.workers(history=True)
                   if not only or only in (w['worker_id'], w.get('name'))]
        fetched = await asyncio.gather(*(self.fetch_catalog(w, query, live_only, limit, actor(user))
                                         for w in targets))
        links = self.session_links(user['id'])
        tasks = (self.store.tasks(user['id']),
                 {s['id']: s['task'] for s in self.store.all(user['id'], details=False) if s.get('task')})
        sessions, workers, errors = [], [], []
        for w, got in zip(targets, fetched):
            if got is None:
                continue
            hb = (w.get('hb') or {}).get('sessions') or {}
            workers.append({'worker_id': w['worker_id'], 'name': w.get('name', ''), 'band': w.get('band'),
                            'source': got['source'], 'count': len(got['items']), 'total': got['total'],
                            'stale': got['stale'], 'fetched': got['fetched'],
                            'harnesses': got.get('harnesses') or [],
                            'counts': {k: hb[k] for k in ('live', 'idle') if isinstance(hb.get(k), int)} or None})
            if got.get('error'):
                errors.append({'worker_id': w['worker_id'], 'worker': w.get('name', ''), 'error': got['error']})
            sessions += [self.place_record(item, w, links, tasks) for item in got['items']]
        sessions.sort(key=lambda r: (r.get('state') == 'closed', -(r.get('updated') or 0)))
        return web.json_response({'sessions': sessions, 'workers': workers, 'errors': errors,
                                  'generated': time.time()}, headers=NO_STORE)

    async def fetch_catalog(self, worker, query, live_only, limit, identity):
        """One worker's catalog through the newest cap it has: sessions.list,
        else work.sessions, else *-history.pull (records built here). None
        for a worker with no sessions at all."""
        caps = worker.get('caps', [])
        target = dict(worker_id=worker['worker_id'], band=worker.get('band'))
        if 'sessions.list' in caps:
            source = 'sessions.list'
        elif 'work.sessions' in caps:
            source = 'work.sessions'
        elif any(a + '-history.pull' in caps for a in ('claude', 'codex')):
            source = 'history'
        else:
            return None
        try:
            if source == 'sessions.list':
                result = await self.rpc(target, 'sessions.list', dict(limit=limit, query=query, live_only=live_only),
                                        timeout=CATALOG_TIMEOUT, identity=identity)
                items, total = result.get('items') or [], result.get('total') or 0
                harnesses = result.get('harnesses')
            elif source == 'work.sessions':
                result = await self.rpc(target, 'work.sessions', dict(limit=limit, query=query),
                                        timeout=CATALOG_TIMEOUT, identity=identity)
                items = legacy_records(result.get('items') or [], result.get('live') or [])
                total, harnesses = result.get('total') or 0, result.get('harnesses')
            else:
                items, total, harnesses = [], 0, []
                for agent in ('claude', 'codex'):
                    if agent + '-history.pull' not in caps:
                        continue
                    result = await self.rpc(target, agent + '-history.pull', dict(limit=limit),
                                            timeout=CATALOG_TIMEOUT, identity=identity)
                    items += legacy_records([dict(s, agent=agent, updated=s.get('last_modified'),
                                                  messages=s.get('message_count'))
                                             for s in result.get('sessions') or []], [])
                    total += result.get('total') or 0
            items = [i for i in items if isinstance(i, dict) and i.get('agent') and i.get('native_id')]
            if source != 'sessions.list':
                items = filter_records(items, query, live_only)
                if query or live_only:
                    total = len(items)
            got = dict(source=source, items=items, total=max(int(total), len(items)),
                       harnesses=harnesses, fetched=time.time(), stale=False)
            if not query and not live_only:
                self.catalogs[worker['worker_id']] = got
            return got
        except Exception as error:  # one bad host must not sink the whole list
            message = str(error) or ('Host request timed out.' if isinstance(error, TimeoutError) else type(error).__name__)
            cached = self.catalogs.get(worker['worker_id'])
            if cached is None:
                return dict(source=source, items=[], total=0, fetched=None, stale=True, error=message)
            items = filter_records(cached['items'], query, live_only)
            return dict(cached, items=items, total=len(items), stale=True, error=message)

    def session_links(self, owner):
        """What only the hub knows: the operator's Work session for a host
        session (the id the worklog view and its terminal socket use)."""
        out = {}
        for s in self.store.all(owner, details=False):
            if s.get('source_id'):
                out[(s.get('worker_id'), s.get('agent') or 'codex', str(s['source_id']).lower())] = s['id']
            if s.get('term_id'):
                out[(s.get('worker_id'), 'term', s['term_id'])] = s['id']
        return out

    @staticmethod
    def place_record(item, worker, links, tasks=({}, {})):
        """Stamp a worker's record with the hub's view of it: its key, the
        operator's Work session for it and the task it is linked to
        (``tasks`` = (by catalog key, by Work session id))."""
        rec = dict(item)
        agent, native = rec['agent'], str(rec['native_id'])
        rec.update(worker_id=worker['worker_id'], worker=worker.get('name', ''),
                   key=f"{worker['worker_id']}/{agent}/{native}")
        rec['links'] = dict(rec.get('links') or {})
        term = (rec.get('view') or {}).get('terminal')
        found = links.get((worker['worker_id'], agent, native.lower())) or (
            links.get((worker['worker_id'], 'term', term)) if term else None)
        if found and not rec['links'].get('work_session'):
            rec['links']['work_session'] = found
        task = tasks[0].get(rec['key']) or tasks[1].get(rec['links'].get('work_session'))
        if task:
            rec['links']['task'] = task
        return rec

    # -- per-session routes of the Sessions page (docs/design/sessions.md §3.6) --

    async def session_op(self, request):
        """``POST /account/work/session/<op>``: one JSON request per action on
        a host session, addressed by ``worker`` (id), ``agent`` and
        ``native_id``. Operator only, dashboard Origin, CSRF token in the body.
        Replies ``{ok: true, …}``, or ``{ok: false, error}`` with 400 (bad
        request), 404 (unknown op), 429 (too many live views) or 502 (the host
        failed or refused)."""
        user = self.user(request)
        if request.headers.get('Origin') != self.account.origin:
            raise web.HTTPForbidden(text='Origin mismatch')
        op = request.match_info['op']
        if op not in SESSION_OPS:
            raise web.HTTPNotFound()

        def fail(status, error):
            return web.json_response({'ok': False, 'error': error}, status=status, headers=NO_STORE)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return fail(400, 'Expected a JSON object.')
        try:
            self.account.csrf(request, data, user)
        except PermissionError as error:
            return fail(403, str(error))
        try:
            result = await getattr(self, 'op_' + op)(request, user, data)
        except HostError as error:
            return fail(502, str(error) or 'The host did not answer.')
        except OverflowError as error:
            return fail(429, str(error))
        except (ValueError, TypeError, KeyError) as error:
            return fail(400, str(error) or 'Invalid request.')
        except web.HTTPNotFound:
            return fail(404, 'Session not found.')
        return web.json_response({'ok': True, **result}, headers=NO_STORE)

    def session_target(self, data, cap=None):
        """The connected worker ``data['worker']`` names, holding ``cap``."""
        wid = str(data.get('worker') or '')
        w = next((w for w in self.workers(history=True) if w['worker_id'] == wid), None)
        if w is None:
            raise ValueError('That host is not connected.')
        if cap and cap not in w.get('caps', []):
            raise ValueError(f"{w.get('name') or 'That host'} has no {cap}; update its worker.")
        return w

    @staticmethod
    def session_ident(data, agents=HARNESSES):
        agent, native = data.get('agent'), str(data.get('native_id') or '')
        if agent not in agents:
            raise ValueError('Unknown agent.')
        if not 0 < len(native) <= 200:
            raise ValueError('A session id is required.')
        return agent, native

    async def host_call(self, worker, cap, args, user, timeout=20):
        try:
            return await self.rpc(dict(worker_id=worker['worker_id'], band=worker.get('band')), cap, args,
                                  timeout=timeout, identity=actor(user))
        except (ValueError, TimeoutError, asyncio.TimeoutError) as error:
            raise HostError(str(error) or 'The host did not answer in time.') from error

    async def masked(self, obj):
        """Known vault values masked, as everywhere else on the dashboard."""
        mask = getattr(self.server, '_bridge_mask', None)
        return await mask(obj) if callable(mask) else obj

    async def op_mirror(self, request, user, data):
        """Relay ``sessions.mirror``: ``{cursor, wait}`` → ``{events, cursor, done, exists}``."""
        agent, native = self.session_ident(data)
        w = self.session_target(data, 'sessions.mirror')
        cursor = max(0, int(data.get('cursor') or 0))
        wait = max(0.0, min(float(data.get('wait') or 0), MIRROR_WAIT_MAX))
        if wait and self.watching >= MIRROR_WATCHERS:
            raise OverflowError('Too many live views are open on this dashboard. Close one and retry.')
        self.watching += 1 if wait else 0
        try:
            out = await self.host_call(w, 'sessions.mirror', dict(
                agent=agent, native_id=native, cursor=cursor, wait=wait,
                max_events=max(1, min(int(data.get('max_events') or MIRROR_EVENTS_MAX), MIRROR_EVENTS_MAX))),
                user, timeout=wait + 15)
        finally:
            self.watching -= 1 if wait else 0
        events = [e for e in out.get('events') or [] if isinstance(e, dict)]
        return await self.masked({'events': events, 'cursor': int(out.get('cursor') or cursor),
                                  'done': bool(out.get('done')), 'exists': out.get('exists', bool(events))})

    async def op_follow(self, request, user, data):
        """Relay ``sessions.follow`` (older workers: ``<agent>-history.follow``)."""
        agent, native = self.session_ident(data, ('claude', 'codex'))
        w = self.session_target(data)
        offset = max(0, int(data.get('offset') or 0))
        version = str(data.get('version') or '')[:200]
        if 'sessions.follow' in w.get('caps', []):
            out = await self.host_call(w, 'sessions.follow', dict(agent=agent, native_id=native,
                                                                  offset=offset, version=version), user)
        elif agent + '-history.follow' in w.get('caps', []):
            out = await self.host_call(w, agent + '-history.follow', dict(session_id=native, offset=offset,
                                                                          version=version), user)
        else:
            raise ValueError('This host cannot show transcripts; update its worker.')
        keep = ('unchanged', 'version', 'replace_from', 'messages', 'truncated', 'next_offset',
                'next_content_offset', 'total_messages', 'activity', 'active')
        return await self.masked({k: out[k] for k in keep if k in out})

    async def op_send(self, request, user, data):
        """``sessions.send``: ``{text, command_id?}`` → ``{delivery, note, …}``."""
        agent, native = self.session_ident(data)
        text = data.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
            raise ValueError(f'Enter a message of 1–{TEXT_MAX} characters.')
        command_id = str(data.get('command_id') or uuid.uuid4().hex)[:100]
        w = self.session_target(data)
        if 'sessions.send' in w.get('caps', []):
            out = await self.host_call(w, 'sessions.send', dict(agent=agent, native_id=native, text=text,
                                                                command_id=command_id), user, timeout=40)
        elif agent + '-history.send' in w.get('caps', []):
            out = await self.host_call(w, agent + '-history.send', dict(session_id=native, text=text,
                                                                        command_id=command_id), user, timeout=40)
            out = dict(out, delivery='turn', detail=out.get('delivery'))
        else:
            raise ValueError('This host cannot take messages for its sessions; update its worker.')
        return {k: out[k] for k in ('delivery', 'note', 'detail', 'terminal', 'native_id') if k in out}

    async def op_stop(self, request, user, data):
        """``sessions.stop``; a hub Work session on that terminal is closed too
        (which revokes its MCP token)."""
        agent, native = self.session_ident(data)
        w = self.session_target(data, 'sessions.stop')
        out = await self.host_call(w, 'sessions.stop', dict(agent=agent, native_id=native), user, timeout=30)
        if out.get('stopped') == 'terminal' and out.get('terminal'):
            for s in self.store.all(user['id'], details=False):
                if s.get('worker_id') == w['worker_id'] and s.get('term_id') == out['terminal']:
                    stream = self.terms.get(w['worker_id'], out['terminal'])
                    if stream is not None:
                        stream.finish(out.get('exit_code'))
                    async with self.lock(s['id']):
                        self.mark_term_done(s['id'], out.get('exit_code'))
        return {k: out[k] for k in ('stopped', 'terminal', 'handle', 'exit_code', 'note') if k in out}

    async def started(self, sid, cid, job):
        """Wait for a launch (shielded: a closed page must not cancel it half
        way) and return the session, or raise its error."""
        if job is not None:
            await asyncio.shield(job)
        result = self.store.result(sid, cid) or {}
        if result.get('status') == 'error':
            raise HostError(result.get('error') or 'The host could not start the terminal.')
        s = self.store.get(sid)
        return {'session': sid, 'terminal': s.get('term_id'), 'title': s.get('title')}

    def command_id(self, data):
        cid = str(data.get('id') or '')
        if not 8 <= len(cid) <= 100:
            raise ValueError('A command ID is required.')
        return cid

    async def op_new(self, request, user, data):
        """New session in a Rook terminal: the Work socket's ``launch``, awaited.
        ``{id, worker, harness, cwd, model?, title?, persona?, task?, mcp?, cols?, rows?}``
        → ``{session, terminal, title}`` (``session`` opens /account/work/term/<session>)."""
        cid = self.command_id(data)
        sid, job = self.new_launch(request, user, cid, data)
        return await self.started(sid, cid, job)

    async def op_resume(self, request, user, data):
        """Resume a closed Claude/Codex session into a Rook terminal
        (``work.stream.open(resume=…)``). ``{id, worker, agent, native_id,
        cwd?, title?, mcp?}`` → ``{session, terminal, title}``."""
        if not self.v2:
            raise ValueError('Live terminals are disabled on this hub.')
        cid = self.command_id(data)
        agent, native = self.session_ident(data, ('claude', 'codex'))
        w = self.session_target(data)
        if not all(c in w.get('caps', []) for c in TERM_CAPS):
            raise ValueError(f"{w.get('name') or 'That host'} cannot run Rook terminals; resume it from the Classic view.")
        existing = next((s for s in self.store.all(user['id'], details=False)
                         if s.get('worker_id') == w['worker_id'] and (s.get('agent') or 'codex') == agent
                         and str(s.get('source_id') or '').lower() == native.lower()), None)
        if existing and existing.get('term_running'):
            return {'session': existing['id'], 'terminal': existing.get('term_id'), 'title': existing.get('title')}
        if existing and existing.get('external_handle'):
            raise ValueError('This session is already running on its host.')
        # The id history discovery gives the same session, so both views share it.
        sid = existing['id'] if existing else uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
            [user['id'], w.get('band'), w['worker_id'], agent, native])).hex
        if self.store.result(sid, cid):
            return await self.started(sid, cid, None)
        async with self.lock(sid):
            if existing:
                s = self.store.get(sid)
            else:
                s = dict(id=sid, owner=user['id'], worker_id=w['worker_id'], band=w.get('band'),
                         worker_name=w.get('name'), agent=agent, imported=True, source_id=native,
                         thread_id=None, turn_id=None, model='', error='', status='pending',
                         title=str(data.get('title') or '')[:160] or native)
            if data.get('cwd'):
                s['cwd'] = check_cwd(data['cwd'])
            s.update(harness=agent, review_status=None)
            self.store.save(s)
        self.store.claim(sid, cid, actor(user))
        return await self.started(sid, cid, self.launch_terminal(request, user, sid, cid, data))

    async def op_attach(self, request, user, data):
        """Watch a Rook terminal the hub has no Work session for (one started
        by an agent, or a session moved with /rook-move): ``{worker, agent,
        native_id, terminal}`` → ``{session}`` for /account/work/term/<session>."""
        agent, native = self.session_ident(data)
        w = self.session_target(data, 'work.stream.list')
        term = str(data.get('terminal') or '')
        if not 0 < len(term) <= 100:
            raise ValueError('A terminal id is required.')
        for s in self.store.all(user['id'], details=False):
            if s.get('worker_id') == w['worker_id'] and s.get('term_id') == term:
                return {'session': s['id']}
        listed = await self.host_call(w, 'work.stream.list', {}, user)
        t = next((t for t in listed.get('terminals') or [] if t.get('id') == term), None)
        if t is None or not t.get('running'):
            raise HostError('This terminal has ended.')
        sid = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([user['id'], w['worker_id'], 'term', term])).hex
        harness = t.get('harness') if t.get('harness') in HARNESSES else agent
        self.store.save(dict(id=sid, owner=user['id'], worker_id=w['worker_id'], band=w.get('band'),
                             worker_name=w.get('name'), title=str(t.get('title') or data.get('title') or native)[:160],
                             cwd=t.get('cwd') or '', model='', agent=harness, harness=harness, status='working',
                             thread_id=None, turn_id=None, error='', created_by=actor(user),
                             term_id=term, term_running=True, term_exit=None, term_note='',
                             term_started=time.time(), last_activity=time.time()))
        return {'session': sid}

    async def op_link(self, request, user, data):
        """Link a session to a Rook task: ``{worker, agent, native_id, task,
        work_session?}``; an empty ``task`` unlinks. Stored on the hub by the
        catalog key, and on the Work session when there is one (it outlives
        the key change when an agent reports its own id)."""
        agent, native = self.session_ident(data)
        task = check_task(data.get('task'))
        wid = str(data.get('worker') or '')
        if not 0 < len(wid) <= 200:
            raise ValueError('A host is required.')
        self.store.set_task(user['id'], f'{wid}/{agent}/{native}', task)
        if data.get('work_session'):
            sid = str(data['work_session'])
            async with self.lock(sid):
                s = self.store.get(sid, user['id'])
                s['task'] = task or None
                self.store.save(s)
        return {'links': {'task': task or None}}

    @staticmethod
    def summary(s):
        return {**{k: s.get(k) for k in ('id', 'title', 'worker_name', 'cwd',
                                     'updated', 'model', 'revision', 'error', 'agent', 'imported', 'review_status')},
                **{k: s[k] for k in ('harness', 'term_running', 'term_exit', 'term_note',
                                     'active', 'message_count', 'remote_runtime', 'persona')
                   if s.get(k) is not None and s.get(k) is not False and s.get(k) != ''},
                **({'term': True} if s.get('term_id') else {}),
                **({'external': True} if s.get('external_handle') else {}),
                **({'revoke': True} if s.get('mcp_token_revoke') and s.get('mcp_token_id') else {}),
                'status': s.get('review_status') or ('ready' if s['status'] == 'waiting' else s['status']),
                'updated': max(s.get('source_updated') or s.get('updated') or 0,
                               s.get('last_activity') or 0)}

    @staticmethod
    def snapshot(s):
        return {k: v for k, v in s.items()
                if k not in ('owner', 'cursor', 'partial', 'rpc', 'handle', 'worker_id', 'band', 'external_handle', 'external_cursor')} | {
                    'status': s.get('review_status') or ('ready' if s['status'] == 'waiting' else s['status']), 'activity': s['status'], 'external_running': bool(s.get('external_handle'))}

    async def websocket(self, request):
        self.user(request)
        if request.headers.get('Origin') != self.account.origin:
            raise web.HTTPForbidden(text='Origin mismatch')
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=65536)
        await ws.prepare(request)
        selected = None
        last_list = None
        last_revision = None
        last_revoke = -1e9
        pending_receipts = {}
        while not ws.closed:
            try:
                user = self.user(request)  # Revocation applies to already-open sockets.
                sessions = [self.summary(s) for s in self.store.all(user['id'], details=False)]
                sessions.sort(key=lambda s: (-(s.get('updated') or 0), s['id']))
                workers = [{'id': w['worker_id'], 'name': w.get('name', ''), 'band': w.get('band')}
                           for w in self.workers()]
                hosts = self.hosts() if self.v2 else []
                listing = json.dumps([sessions, workers, hosts])
                if listing != last_list:
                    await ws.send_json({'type': 'index', 'sessions': sessions, 'workers': workers,
                                        'hosts': hosts})
                    last_list = listing
                if any(s.get('revoke') for s in sessions) and time.monotonic() - last_revoke > 30:
                    last_revoke = time.monotonic()  # bounded retries if the token service is down
                    await self.revoke_session_tokens(request, user)
                for cid, sid in list(pending_receipts.items()):
                    result = self.store.result(sid, cid)
                    if not result or result.get('status') != 'accepted':
                        await ws.send_json({'type': 'ack', 'id': cid, 'result': result or {'status': 'error', 'error': 'No delivery receipt found. Check the host before retrying.'}})
                        pending_receipts.pop(cid, None)
                if selected:
                    selected_meta = self.store.get(selected, user['id'])
                    if selected_meta.get('external_handle'):
                        await self.drain_external(selected)
                    if self.store.revision(selected, user['id']) != last_revision:
                        s = self.store.get(selected, user['id'])
                        snapshot = self.snapshot(s)
                        if s.get('imported'):
                            snapshot['terminal'] = self.external_output.get(s['id'], '')
                        await ws.send_json({'type': 'session', 'session': snapshot})
                        last_revision = s['revision']
                try:
                    msg = await ws.receive(timeout=0.35)
                except asyncio.TimeoutError:
                    continue
                if msg.type != WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                self.account.csrf(request, data, user)
                if data.get('op') == 'select':
                    self.store.get(data['session'], user['id'])
                    selected, last_revision = data['session'], None
                    continue
                cid = str(data.get('id', ''))
                if not 8 <= len(cid) <= 100:
                    raise ValueError('A command ID is required.')
                if data.get('op') == 'create':
                    # Stable id makes browser resubmission of create idempotent.
                    sid = uuid.uuid5(uuid.NAMESPACE_URL, user['id'] + ':' + cid).hex
                    try:
                        self.store.get(sid, user['id'])
                    except web.HTTPNotFound:
                        w = next((w for w in self.workers() if w['worker_id'] == data.get('worker')), None)
                        if not w:
                            raise ValueError('Choose a connected host.')
                        cwd = str(data.get('cwd', '')).strip()
                        if not cwd.startswith('/') or len(cwd) > 2000:
                            raise ValueError('Enter an absolute working directory.')
                        s = dict(id=sid, owner=user['id'], title=str(data.get('title') or 'New work')[:160],
                                 worker_id=w['worker_id'], worker_name=w.get('name'), band=w.get('band'),
                                 cwd=cwd, model=str(data.get('model') or '')[:100],
                                 agent='codex', status='starting', remote_runtime=True, worker_revision=0,
                                 thread_id=None, turn_id=None, error='', created_by=actor(user))
                        self.store.save(s, {'op': 'create'})
                        self.launch(sid, {'op': 'open', 'id': cid}, actor(user))
                    selected, last_revision = sid, None
                    await ws.send_json({'type': 'selected', 'session': sid})
                elif data.get('op') == 'launch':
                    sid, _ = self.new_launch(request, user, cid, data)
                    pending_receipts[cid] = sid
                    await ws.send_json({'type': 'launched', 'session': sid, 'id': cid})
                else:
                    sid = str(data.get('session', ''))
                    self.store.get(sid, user['id'])
                    current = self.store.get(sid, user['id'])
                    pty_resume = (data.get('op') == 'resume' and data.get('pty') and self.v2
                                  and current.get('imported') and not current.get('term_running')
                                  and not current.get('external_handle') and self.term_capable(current))
                    if data.get('op') != 'receipt' and self.store.claim(sid, cid, actor(user)):
                        if pty_resume:
                            async with self.lock(sid):
                                current = self.store.get(sid)
                                current['harness'] = current.get('agent') or 'claude'
                                self.store.save(current)
                            self.launch_terminal(request, user, sid, cid, data)
                        else:
                            self.launch(sid, data, actor(user))
                    pending_receipts[cid] = sid
                    await ws.send_json({'type': 'ack', 'id': cid,
                                        'result': self.store.result(sid, cid) or {'status': 'error', 'error': 'No delivery receipt found. Check the host before retrying.'}})
            except (ValueError, KeyError, TypeError, PermissionError, json.JSONDecodeError) as e:
                await ws.send_json({'type': 'error', 'error': str(e)})
            except (web.HTTPUnauthorized, web.HTTPForbidden):
                await ws.close(code=1008, message=b'Sign in again')
            except web.HTTPNotFound:
                selected = None
                await ws.send_json({'type': 'error', 'error': 'Session not found.'})
        return ws

    def launch(self, sid, data, identity='system:work'):
        job = asyncio.create_task(self.command(sid, data, identity))
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def command(self, sid, data, identity='system:work'):
        async def rpc(*args, **kwargs):
            return await self.rpc(*args, identity=identity, **kwargs)
        async with self.lock(sid):
            s = self.store.get(sid)
            try:
                op = data['op']
                if op == 'status' and data.get('status') == 'closed':
                    op = 'close'
                launched = bool(s.get('harness')) and not s.get('imported')
                if op in ('close', 'term_close') and s.get('term_running'):
                    await self.close_terminal(s)
                    s = self.store.get(sid)
                if op == 'term_close':
                    s['error'] = ''
                elif launched:
                    # A launched terminal: interaction happens in the terminal.
                    if op == 'status':
                        value = data.get('status')
                        if value not in ('auto', 'closed', 'blocked', 'pending'):
                            raise ValueError('Choose Auto, Closed, Blocked, or Pending.')
                        s['review_status'] = None if value == 'auto' else value
                    elif op == 'close':
                        s['review_status'] = 'closed'
                    else:
                        raise ValueError('Use the terminal to interact with this session.')
                elif op == 'status':
                    value = data.get('status')
                    if value not in ('auto', 'closed', 'blocked', 'pending'):
                        raise ValueError('Choose Auto, Closed, Blocked, or Pending.')
                    s['review_status'] = None if value == 'auto' else value
                    s['error'] = ''
                elif s.get('imported') and op == 'close':
                    if s.get('external_handle'):
                        await rpc(s, 'proc.signal', {'handle': s['external_handle'], 'sig': 'TERM'})
                        s['external_handle'] = None
                    s['review_status'] = 'closed'
                elif s.get('imported') and op == 'resume':
                    if s.get('external_handle'):
                        raise ValueError('This session is already running on its host.')
                    result = await rpc(s, s['agent'] + '-history.resume', {'session_id': s['source_id']}, timeout=40)
                    if result.get('terminal'):
                        # Newer workers resume into a Rook terminal (work.stream).
                        s.update(harness=s.get('agent') or 'claude', term_id=result['terminal'],
                                 term_running=True, term_exit=None, term_note='', term_started=time.time(),
                                 review_status=None, error='', resume_note=result.get('note', ''))
                    else:
                        self.external_output[sid] = ''
                        s.update(external_handle=result['handle'], external_cursor=0,
                                 review_status=None, error='', resume_note=result.get('note', ''))
                elif s.get('imported') and op in ('terminal_input', 'message'):
                    text = str(data.get('text', ''))
                    if not text.strip() or len(text) > 24000:
                        raise ValueError('Enter a message of 1–24000 characters.')
                    s['message_note'] = ''
                    if s.get('external_handle'):
                        await rpc(s, 'proc.write', {'handle': s['external_handle'], 'data': text, 'newline': True})
                        s['message_note'] = 'Input sent to the host terminal.'
                    else:
                        result = await rpc(s, s['agent'] + '-history.send',
                            {'session_id': s['source_id'], 'text': text, 'command_id': data['id']}, timeout=30)
                        s['message_note'] = result.get('note', 'Message submitted on host.')
                    s['error'] = ''
                elif s.get('imported') and op == 'interrupt':
                    if not s.get('external_handle'):
                        raise ValueError('No running session to interrupt.')
                    await rpc(s, 'proc.signal', {'handle': s['external_handle'], 'sig': 'INT'})
                elif s.get('imported'):
                    raise ValueError('This session is running outside the web app. Continue it on its host.')
                elif s.get('legacy_runtime'):
                    raise ValueError('Waiting for the host to adopt this session. Update and connect its worker.')
                else:
                    if op == 'open':
                        result = await rpc(s, 'work.create', dict(session_id=sid,
                            command_id=data['id'], cwd=s['cwd'], title=s['title'], model=s['model']), timeout=40)
                    else:
                        result = await rpc(s, 'work.command', dict(session_id=sid,
                            command={k: v for k, v in data.items() if k not in ('csrf', 'session')}), timeout=40)
                    self.apply_metadata(s, result['session'])
                    if op == 'close':
                        s['review_status'] = 'closed'
                    elif op == 'resume':
                        s['review_status'] = None
                    if result.get('result', {}).get('status') != 'submitted':
                        raise ValueError('Command outcome is uncertain or failed. Check the host session before retrying.')
                if op in ('message', 'terminal_input'):
                    s['last_activity'] = time.time()
                self.store.save(s, {'op': op, 'command': data.get('id')})
                self.store.result(sid, data['id'], {'status': 'submitted'})
            except Exception as e:
                s['error'] = str(e) or type(e).__name__
                if s['status'] in ('starting', 'sending'):
                    s['status'] = 'uncertain'
                self.store.save(s, {'error': s['error']})
                self.store.result(sid, data['id'], {'status': 'error', 'error': s['error']})
                log.warning('Work command %s: %s', sid, e)

    async def discover(self):
        """Keep each administrator's worker history durable without a browser open."""
        while True:
            try:
                with self.account.store.db() as db:
                    owners = [r['id'] for r in db.execute('SELECT id FROM users WHERE admin=1')]
                allowed = {name.strip() for name in os.environ.get("ROOK_WORK_IMPORT_WORKERS", "").split(",") if name.strip()}
                for worker in self.workers(history=True):
                    if allowed and worker.get("name") not in allowed:
                        continue
                    for agent in ('claude', 'codex'):
                        if agent + '-history.pull' not in worker.get('caps', []):
                            continue
                        try:
                            await self.sync_history(worker, agent, owners)
                        except Exception:
                            log.exception('Work history sync failed for %s/%s', worker['worker_id'], agent)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Work discovery failed')
            await asyncio.sleep(15)

    async def sync_history(self, worker, agent, owners):
        target = dict(worker_id=worker['worker_id'], band=worker.get('band'))
        namespace = agent + '-history'
        existing_by_owner = {owner: {s.get('source_id') or s.get('thread_id'): s
            for s in self.store.all(owner, details=False)
            if s['worker_id'] == worker['worker_id'] and s.get('band') == worker.get('band')
            and s.get('agent', 'codex') == agent} for owner in owners}
        offset = 0
        while True:
            result = await self.rpc(target, namespace + '.pull', {'limit': 20, 'offset': offset})
            entries = result.get('sessions', [])
            for meta in entries:
                source = meta.get('session_id')
                if not source or meta.get('error'):
                    continue
                for owner in owners:
                    # A thread created here already has a durable web session.
                    existing = existing_by_owner[owner].get(source)
                    if existing and not existing.get('imported'):
                        continue
                    sid = existing['id'] if existing else uuid.uuid5(uuid.NAMESPACE_URL,
                        json.dumps([owner, worker.get('band'), worker['worker_id'], agent, source])).hex
                    fingerprint = [meta.get('last_modified'), meta.get('size_bytes')]
                    activity = meta.get('activity', 'pending')
                    if activity == 'working' and time.time() - (meta.get('last_modified') or 0) > 120:
                        activity = 'pending'
                    if existing and existing.get('source_version') == fingerprint and existing['status'] == activity and existing.get('active') == bool(meta.get('active')) and existing.get('messageable') == bool(meta.get('messageable')) and existing.get('title') == (meta.get('title') or source):
                        continue
                    async with self.lock(sid):
                        s = self.store.get(sid) if existing else dict(
                            id=sid, owner=owner, **target, worker_name=worker.get('name'),
                            agent=agent, imported=True, source_id=source, thread_id=None,
                            handle=None, turn_id=None, pending={}, rpc={}, cursor=0,
                            partial='', model='', error='', diff='', status='pending')
                        s.update(title=meta.get('title') or source, cwd=meta.get('cwd'),
                                 source_version=fingerprint, source_updated=meta.get('last_modified'),
                                 message_count=meta.get('message_count'), status=activity, active=bool(meta.get('active')),
                                 messageable=bool(meta.get('messageable')))
                        self.store.save(s)
                        existing_by_owner[owner][source] = s
            offset += len(entries)
            if not entries or offset >= result.get('total', offset):
                break

    @staticmethod
    def apply_metadata(s, meta):
        for key in ('thread_id', 'turn_id', 'status', 'model', 'running', 'needs_input'):
            if key in meta:
                s[key] = meta[key]
        s['worker_revision'] = meta['revision']
        if meta.get('updated') is not None:
            s['source_updated'] = meta['updated']
        s['disconnected'] = False
        s['error'] = 'The host reported an error. Open the session for details.' if meta.get('has_error') else ''

    async def migrate(self, s):
        archive = self.store.archive(s['id'])
        data = json.dumps(archive, ensure_ascii=False)
        digest = hashlib.sha256(data.encode('utf-8')).hexdigest()
        offset = 0
        for index in range(0, len(data), 6000):
            chunk = data[index:index + 6000]
            result = await self.rpc(s, 'work.adopt_page', dict(session_id=s['id'], digest=digest,
                offset=offset, data=chunk, final=index + len(chunk) == len(data)))
            offset += len(chunk.encode('utf-8'))
            if result.get('adopted'):
                break
            await asyncio.sleep(1)
        if not result.get('adopted') or result.get('digest') != digest:
            raise ValueError('Host has not acknowledged the session archive.')
        async with self.lock(s['id']):
            current = self.store.get(s['id'])
            current.update(legacy_runtime=False, remote_runtime=True, worker_revision=0)
            self.store.save(current)

    async def collect(self):
        while True:
            try:
                groups = {}
                for s in self.store.all(details=False):
                    if s.get('legacy_runtime'):
                        try:
                            if 'work.adopt_page' in self.worker(s).get('caps', []):
                                await self.migrate(s)
                        except Exception:
                            log.warning('Host migration pending for %s', s['id'])
                    elif s.get('remote_runtime'):
                        groups.setdefault((s['band'], s['worker_id']), []).append(s)
                self._ticks += 1
                if self._ticks % 3 == 0:
                    await self.sweep_terminals()
                for sessions in groups.values():
                    for offset in range(0, len(sessions), 20):
                        batch = sessions[offset:offset + 20]
                        try:
                            result = await self.rpc(batch[0], 'work.status', {'sessions': [s['id'] for s in batch]})
                            for meta in result['sessions']:
                                if meta.get('id') not in {s['id'] for s in batch} or meta.get('missing'):
                                    continue
                                async with self.lock(meta['id']):
                                    s = self.store.get(meta['id'])
                                    if s.get('worker_revision') != meta['revision'] or s.get('disconnected'):
                                        self.apply_metadata(s, meta)
                                        self.store.save(s)
                        except Exception:
                            for previous in batch:
                                async with self.lock(previous['id']):
                                    s = self.store.get(previous['id'])
                                    if not s.get('disconnected'):
                                        s['disconnected'] = True
                                        self.store.save(s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Work metadata collector failed')
            await asyncio.sleep(2)

    async def sweep_terminals(self):
        """Catch terminals that ended while nobody was watching them: one
        ``work.stream.list`` per worker that has sessions marked running."""
        by_worker = {}
        for s in self.store.all(details=False):
            if s.get('term_running') and s.get('term_id'):
                stream = self.terms.get(s['worker_id'], s['term_id'])
                if stream is not None and stream.running and stream.pull and not stream.pull.done():
                    continue  # followed live; the stream reports its own end
                by_worker.setdefault((s['band'], s['worker_id']), []).append(s)
        for sessions in by_worker.values():
            try:
                result = await self.rpc(sessions[0], 'work.stream.list', {})
            except (ValueError, TimeoutError, asyncio.TimeoutError):
                continue  # offline: keep the record until the host is back
            terms = {t['id']: t for t in result.get('terminals', [])}
            for s in sessions:
                t = terms.get(s['term_id'])
                if t is None:
                    async with self.lock(s['id']):
                        self.mark_term_done(s['id'], None, 'Terminal is gone from its host.')
                elif not t.get('running'):
                    async with self.lock(s['id']):
                        self.mark_term_done(s['id'], t.get('exit_code'))

    async def drain_external(self, sid):
        async with self.lock(sid):
            s = self.store.get(sid)
            if not s.get('external_handle'):
                return
            try:
                result = await self.rpc(s, 'proc.read', {'handle': s['external_handle'],
                    'cursor': s.get('external_cursor', 0), 'max_bytes': 8192})
                chunk = result.get('chunk', '')
                if not chunk and not result.get('eof') and not s.get('disconnected'):
                    return
                self.external_output[sid] = (self.external_output.get(sid, '') + chunk)[-200000:]
                s['external_cursor'] = result['next_cursor']
                s['disconnected'] = False
                s['error'] = ''
                if result.get('eof'):
                    s['external_handle'] = None
                self.store.save(s)
            except Exception as error:
                missing = 'no such handle' in str(error)
                if missing:
                    s['external_handle'] = None
                if missing or not s.get('disconnected'):
                    s['disconnected'] = True
                    s['error'] = str(error)
                    self.store.save(s)
