"""Durable work review, worker history discovery, and agent process transport."""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

from aiohttp import web, WSMsgType
from .account_web import NO_STORE

log = logging.getLogger(__name__)


# Kept as an import for callers of the former web projection helper.
from ..worker.work_runtime import project

METADATA_KEYS = frozenset(('id', 'owner', 'title', 'cwd', 'model', 'worker_id',
    'worker_name', 'band', 'agent', 'imported', 'source_id', 'source_version',
    'source_updated', 'last_activity', 'message_count', 'thread_id', 'turn_id', 'status',
    'review_status', 'revision', 'updated', 'error', 'disconnected',
    'external_handle', 'external_cursor', 'resume_note', 'remote_runtime',
    'worker_revision', 'running', 'needs_input', 'legacy_runtime', 'active', 'messageable', 'message_note',
    'created_by'))


def actor(user):
    """Audit identity for a signed-in account, stamped on band calls."""
    return 'human:' + str(user.get('username') or user['id'])


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

    def lock(self, sid):
        return self.locks.setdefault(sid, asyncio.Lock())

    def install(self, app):
        app.router.add_get('/account/work/bootstrap', self.bootstrap)
        app.router.add_get('/account/work/history/{session}', self.history_page)
        app.router.add_get('/account/work/view/{session}', self.view_page)
        app.router.add_get('/account/work/ws', self.websocket)
        app.router.add_get('/account/work/assets/{name}', self.asset)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)

    async def start(self, app):
        self.pump = asyncio.create_task(self.collect())
        self.discovery = asyncio.create_task(self.discover())

    async def stop(self, app):
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
        if name not in ('work.js', 'work.css'):
            raise web.HTTPNotFound()
        return web.Response(text=(Path(__file__).parents[1] / 'web' / name).read_text(),
                            content_type='text/css' if name.endswith('css') else 'application/javascript',
                            headers=NO_STORE)

    async def bootstrap(self, request):
        user = self.user(request)
        return web.json_response({'csrf': user['csrf']}, headers=NO_STORE)

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

    @staticmethod
    def summary(s):
        return {**{k: s.get(k) for k in ('id', 'title', 'worker_name', 'cwd',
                                     'updated', 'model', 'revision', 'error', 'agent', 'imported', 'review_status')},
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
        pending_receipts = {}
        while not ws.closed:
            try:
                user = self.user(request)  # Revocation applies to already-open sockets.
                sessions = [self.summary(s) for s in self.store.all(user['id'], details=False)]
                sessions.sort(key=lambda s: (-(s.get('updated') or 0), s['id']))
                workers = [{'id': w['worker_id'], 'name': w.get('name', ''), 'band': w.get('band')}
                           for w in self.workers()]
                listing = json.dumps([sessions, workers])
                if listing != last_list:
                    await ws.send_json({'type': 'index', 'sessions': sessions, 'workers': workers})
                    last_list = listing
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
                else:
                    sid = str(data.get('session', ''))
                    self.store.get(sid, user['id'])
                    if data.get('op') != 'receipt' and self.store.claim(sid, cid, actor(user)):
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
                if op == 'status':
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
