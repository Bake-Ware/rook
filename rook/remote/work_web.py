"""Durable server-owned work sessions. The worker only transports Codex stdio."""
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

from aiohttp import web, WSMsgType
from .account_web import NO_STORE

log = logging.getLogger(__name__)


def project(state, message):
    """Project native Codex events into a browser snapshot; retain raw events separately."""
    method = message.get('method', '')
    p = message.get('params') or {}
    if 'id' in message and method:
        state['pending'][str(message['id'])] = message
        state['status'] = 'waiting'
    if method == 'thread/started':
        state['thread_id'] = p['thread']['id']
    elif method == 'turn/started':
        state['turn_id'] = p['turn']['id']
        state['status'] = 'working'
        state['diff'] = ''
    elif method == 'turn/completed':
        turn = p.get('turn', {})
        state['turn_id'] = None
        state['status'] = 'ready'
        state['pending'] = {}
        if turn.get('error'):
            state['error'] = str(turn['error'])
    elif method == 'turn/diff/updated':
        state['diff'] = p.get('diff', '')
    elif method in ('item/started', 'item/completed'):
        item = p.get('item', {})
        key = item.get('id')
        if key:
            previous = state['items'].get(key, {})
            state['items'][key] = {**previous, **item}
            if key not in state['order']:
                state['order'].append(key)
    elif method in ('item/agentMessage/delta', 'item/commandExecution/outputDelta',
                    'item/reasoning/summaryTextDelta', 'item/reasoning/textDelta'):
        key = p.get('itemId')
        if key:
            kind = 'commandExecution' if 'commandExecution' in method else ('reasoning' if '/reasoning/' in method else 'agentMessage')
            item = state['items'].setdefault(key, {'id': key, 'type': kind})
            field = 'aggregatedOutput' if kind == 'commandExecution' else 'text'
            item[field] = (item.get(field, '') + p.get('delta', ''))[-200000:]
            if key not in state['order']:
                state['order'].append(key)
    elif method == 'error':
        state['error'] = str(p.get('error', p))
    elif method == 'serverRequest/resolved':
        state['pending'].pop(str(p.get('requestId')), None)


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
                CREATE TABLE IF NOT EXISTS work_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                    created REAL NOT NULL, event TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS work_events_session ON work_events(session,seq);
                CREATE TABLE IF NOT EXISTS work_commands(
                    session TEXT NOT NULL, id TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(session,id));
            """)
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
            row = db.execute('SELECT * FROM work_sessions WHERE id=?', (sid,)).fetchone()
        if row is None or (owner is not None and row['owner'] != owner):
            raise web.HTTPNotFound()
        return json.loads(row['state'])

    def all(self, owner=None):
        with self.db() as db:
            rows = db.execute('SELECT state FROM work_sessions ' +
                              ('WHERE owner=? ' if owner else '') + 'ORDER BY updated DESC',
                              (owner,) if owner else ()).fetchall()
        return [json.loads(r['state']) for r in rows]

    def save(self, state, event=None):
        state['updated'] = time.time()
        state['revision'] = state.get('revision', 0) + 1
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO work_sessions VALUES(?,?,?,?)',
                       (state['id'], state['owner'], state['updated'], json.dumps(state)))
            if event is not None:
                db.execute('INSERT INTO work_events(session,created,event) VALUES(?,?,?)',
                           (state['id'], time.time(), json.dumps(event)))

    def claim(self, sid, cid):
        with self.db() as db:
            cur = db.execute('INSERT OR IGNORE INTO work_commands VALUES(?,?,?)',
                             (sid, cid, json.dumps({'status': 'accepted'})))
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

    def lock(self, sid):
        return self.locks.setdefault(sid, asyncio.Lock())

    def install(self, app):
        app.router.add_get('/account/work/bootstrap', self.bootstrap)
        app.router.add_get('/account/work/ws', self.websocket)
        app.router.add_get('/account/work/assets/{name}', self.asset)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)

    async def start(self, app):
        self.pump = asyncio.create_task(self.collect())

    async def stop(self, app):
        if self.pump:
            self.pump.cancel()
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*(list(self.jobs) + ([self.pump] if self.pump else [])),
                             return_exceptions=True)

    def user(self, request):
        user = self.account.current(request)
        if not user:
            raise web.HTTPUnauthorized()
        if not user['admin']:
            raise web.HTTPForbidden()
        return user

    def workers(self):
        band = self.server._band
        if band is None:
            return []
        return [w for w in band.workers.values()
                if all(c in w.get('caps', []) for c in ('proc.start', 'proc.read', 'proc.write'))
                and time.time() - w.get('last_seen', 0) < 90
                and not self.server._ban_match(w.get('name'), w['worker_id'])]

    def worker(self, state):
        found = next((w for w in self.workers() if w['worker_id'] == state['worker_id']
                      and w.get('band') == state['band']), None)
        if found is None:
            raise ValueError('Host disconnected. Session history is safe on the server.')
        return found

    async def rpc(self, state, cap, args):
        self.worker(state)
        reply = await self.server._band.call(cap=cap, args=args, target=state['worker_id'], timeout=12)
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

    @staticmethod
    def summary(s):
        return {k: s.get(k) for k in ('id', 'title', 'worker_name', 'cwd', 'status',
                                     'updated', 'model', 'revision', 'error')}

    @staticmethod
    def snapshot(s):
        return {k: v for k, v in s.items()
                if k not in ('owner', 'cursor', 'partial', 'rpc', 'handle', 'worker_id', 'band')}

    async def websocket(self, request):
        self.user(request)
        if request.headers.get('Origin') != self.account.origin:
            raise web.HTTPForbidden(text='Origin mismatch')
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=65536)
        await ws.prepare(request)
        selected = None
        last_list = None
        last_revision = None
        while not ws.closed:
            try:
                user = self.user(request)  # Revocation applies to already-open sockets.
                sessions = [self.summary(s) for s in self.store.all(user['id'])]
                workers = [{'id': w['worker_id'], 'name': w.get('name', ''), 'band': w.get('band')}
                           for w in self.workers()]
                listing = json.dumps([sessions, workers])
                if listing != last_list:
                    await ws.send_json({'type': 'index', 'sessions': sessions, 'workers': workers})
                    last_list = listing
                if selected:
                    s = self.store.get(selected, user['id'])
                    if s['revision'] != last_revision:
                        await ws.send_json({'type': 'session', 'session': self.snapshot(s)})
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
                                 status='starting', items={}, order=[], pending={}, rpc={}, cursor=0,
                                 partial='', handle=None, thread_id=None, turn_id=None, error='', diff='')
                        self.store.save(s, {'op': 'create'})
                        self.launch(sid, {'op': 'open', 'id': cid})
                    selected, last_revision = sid, None
                    await ws.send_json({'type': 'selected', 'session': sid})
                else:
                    sid = str(data.get('session', ''))
                    self.store.get(sid, user['id'])
                    if self.store.claim(sid, cid):
                        self.launch(sid, data)
                    await ws.send_json({'type': 'ack', 'id': cid,
                                        'result': self.store.result(sid, cid)})
            except (ValueError, KeyError, TypeError, PermissionError, json.JSONDecodeError) as e:
                await ws.send_json({'type': 'error', 'error': str(e)})
            except (web.HTTPUnauthorized, web.HTTPForbidden):
                await ws.close(code=1008, message=b'Sign in again')
            except web.HTTPNotFound:
                selected = None
                await ws.send_json({'type': 'error', 'error': 'Session not found.'})
        return ws

    def launch(self, sid, data):
        job = asyncio.create_task(self.command(sid, data))
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def send(self, s, method=None, params=None, response=None):
        if response is not None:
            payload = response
        else:
            rid = uuid.uuid4().hex
            payload = {'id': rid, 'method': method, 'params': params or {}}
            s['rpc'][rid] = method
        # Persist intent before sending. Never blindly retry a possibly delivered turn.
        self.store.save(s, {'direction': 'out', 'message': payload})
        await self.rpc(s, 'proc.write', {'handle': s['handle'], 'data': json.dumps(payload), 'newline': True})

    async def command(self, sid, data):
        async with self.lock(sid):
            s = self.store.get(sid)
            try:
                op = data['op']
                if op == 'open':
                    result = await self.rpc(s, 'proc.start', {
                        'argv': ['codex', 'app-server', '--listen', 'stdio://'],
                        'cwd': s['cwd'], 'label': 'Rook Work ' + sid, 'buffer_bytes': 4194304})
                    s['handle'] = result['handle']
                    self.store.save(s)
                    await self.send(s, 'initialize', {'clientInfo': {'name': 'rook_work', 'version': '0.1.0'},
                                    'capabilities': {'experimentalApi': True}})
                elif op == 'message':
                    text = str(data.get('text', '')).strip()
                    if not text or len(text) > 24000:
                        raise ValueError('Enter a message of 1–24000 characters.')
                    if s['status'] not in ('ready', 'working') or not s['thread_id']:
                        raise ValueError('Wait for the agent to be ready or answer its pending request.')
                    params = {'threadId': s['thread_id'], 'input': [{'type': 'text', 'text': text}]}
                    method = 'turn/start'
                    if s['turn_id']:
                        method = 'turn/steer'
                        params['expectedTurnId'] = s['turn_id']
                    else:
                        s['status'] = 'sending'
                    s['error'] = ''
                    await self.send(s, method, params)
                elif op == 'interrupt':
                    if not s['turn_id']:
                        raise ValueError('No active turn.')
                    await self.send(s, 'turn/interrupt', {'threadId': s['thread_id'], 'turnId': s['turn_id']})
                elif op == 'answer':
                    key = str(data.get('request'))
                    pending = s['pending'].get(key)
                    if not pending:
                        raise ValueError('That request has already been answered.')
                    method = pending['method']
                    if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                                  'item/fileRead/requestApproval'):
                        decision = data.get('decision')
                        if decision not in ('accept', 'decline', 'cancel'):
                            raise ValueError('Invalid approval decision.')
                        answer = {'decision': decision}
                    elif method == 'item/tool/requestUserInput':
                        answers = data.get('answers')
                        if not isinstance(answers, dict):
                            raise ValueError('Answers are required.')
                        answer = {'answers': {str(k): {'answers': [str(v)]} for k, v in answers.items()}}
                    else:
                        raise ValueError('Unsupported request type. Stop this turn to cancel it.')
                    # Consume before delivery so multiple browsers cannot answer twice.
                    del s['pending'][key]
                    s['status'] = 'working' if not s['pending'] else 'waiting'
                    await self.send(s, response={'id': pending['id'], 'result': answer})
                elif op == 'close':
                    if s['handle']:
                        await self.rpc(s, 'proc.signal', {'handle': s['handle'], 'sig': 'TERM'})
                    s['status'] = 'closed'
                    s['pending'] = {}
                    s['handle'] = None
                elif op == 'resume':
                    if s['handle']:
                        raise ValueError('The agent process is still attached; stop it before reopening.')
                    if not s['thread_id']:
                        raise ValueError('No saved agent thread is available; start a new session.')
                    s.update(status='starting', cursor=0, partial='', rpc={}, pending={}, error='')
                    result = await self.rpc(s, 'proc.start', {
                        'argv': ['codex', 'app-server', '--listen', 'stdio://'],
                        'cwd': s['cwd'], 'label': 'Rook Work ' + sid, 'buffer_bytes': 4194304})
                    s['handle'] = result['handle']
                    await self.send(s, 'initialize', {'clientInfo': {'name': 'rook_work', 'version': '0.1.0'},
                                    'capabilities': {'experimentalApi': True}})
                else:
                    raise ValueError('Unknown action.')
                self.store.save(s, {'op': op, 'command': data.get('id')})
                self.store.result(sid, data['id'], {'status': 'submitted'})
            except Exception as e:
                s['error'] = str(e) or type(e).__name__
                if s['status'] in ('starting', 'sending'):
                    s['status'] = 'uncertain'
                self.store.save(s, {'error': s['error']})
                self.store.result(sid, data['id'], {'status': 'error', 'error': s['error']})
                log.warning('Work command %s: %s', sid, e)

    async def collect(self):
        while True:
            try:
                sessions = [s for s in self.store.all() if s.get('handle')]
                await asyncio.gather(*(self.drain(s['id']) for s in sessions), return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Work collector failed')
            await asyncio.sleep(0.35)

    async def drain(self, sid):
        async with self.lock(sid):
            s = self.store.get(sid)
            if not s.get('handle'):
                return
            try:
                r = await self.rpc(s, 'proc.read', {'handle': s['handle'], 'cursor': s['cursor'], 'max_bytes': 8192})
                if r.get('dropped'):
                    s['status'] = 'error'
                    s['error'] = 'Host output buffer overflowed. Stop and reopen the saved thread to recover.'
                    self.store.save(s)
                    return
                chunk = r.get('chunk', '')
                if not chunk and not r.get('eof'):
                    if s.pop('disconnected', False):
                        s['error'] = ''
                        self.store.save(s)
                    return
                if s.get('disconnected'):
                    s['error'] = ''
                s['disconnected'] = False
                s['cursor'] = r['next_cursor']
                lines = (s['partial'] + chunk).split('\n')
                s['partial'] = lines.pop()
                followups = []
                for line in lines:
                    try:
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            continue
                    except (ValueError, TypeError):
                        continue  # Codex stderr shares the pipe; it is not protocol data.
                    project(s, message)
                    if 'id' in message and 'method' not in message:
                        method = s['rpc'].pop(str(message['id']), None)
                        if 'error' in message:
                            s['error'] = str(message['error'])
                            s['status'] = 'ready' if s['thread_id'] else 'error'
                        elif method == 'initialize':
                            followups.append(('initialized', {}))
                        elif method in ('thread/start', 'thread/resume'):
                            result = message.get('result', {})
                            s['thread_id'] = result['thread']['id']
                            s['model'] = result.get('model', s['model'])
                            s['status'] = 'ready'
                            s['turn_id'] = None
                            # Resume restores the provider's saved history when available.
                            for turn in result.get('thread', {}).get('turns', []):
                                for item in turn.get('items', []):
                                    project(s, {'method': 'item/completed', 'params': {'item': item}})
                        elif method == 'turn/start':
                            turn = message.get('result', {}).get('turn', {})
                            if turn.get('status') == 'inProgress':
                                s['turn_id'] = turn.get('id')
                                s['status'] = 'working'
                    # Raw event and projected state/cursor commit together after the entire batch.
                    followups.append(('event', message))
                if r.get('eof'):
                    s['handle'] = None
                    s['status'] = 'closed'
                    s['pending'] = {}
                    s['error'] = 'Agent exited (code ' + str(r.get('exit_code')) + '). Reopen to continue its saved thread.'
                self.store.save(s, {'direction': 'in', 'events': [p for m, p in followups if m == 'event']})
                for method, params in followups:
                    if method == 'initialized':
                        await self.send(s, response={'method': 'initialized', 'params': {}})
                        params = {'cwd': s['cwd'], 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write',
                                  'approvalsReviewer': 'user'}
                        if s['model']:
                            params['model'] = s['model']
                        if s['thread_id']:
                            params['threadId'] = s['thread_id']
                        await self.send(s, 'thread/resume' if s['thread_id'] else 'thread/start', params)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if 'no such handle' in str(e):
                    s['handle'] = None
                    s['status'] = 'closed'
                    s['pending'] = {}
                    s['error'] = 'Host process ended. Reopen to continue the saved agent thread.'
                    self.store.save(s)
                elif not s.get('disconnected'):
                    s['disconnected'] = True
                    s['error'] = str(e) or 'Host did not answer.'
                    self.store.save(s)
