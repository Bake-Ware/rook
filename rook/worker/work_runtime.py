"""Worker-local Codex session controller and durable conversation state."""
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

log = logging.getLogger(__name__)

def project(state, message):
    """Project native Codex events into a browser snapshot; retain raw events separately."""
    method = message.get('method', '')
    p = message.get('params') or {}
    if 'id' in message and method:
        state['pending'][str(message['id'])] = message
        state['status'] = 'ready'
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
        if not state['pending'] and state.get('turn_id'):
            state['status'] = 'working'


class RuntimeStore:
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
            raise ValueError('Session not found on host.')
        return json.loads(row['state'])

    def all(self, owner=None, *, details=True):
        with self.db() as db:
            table = 'work_sessions' if details else 'work_index'
            rows = db.execute('SELECT state FROM ' + table + ' ' +
                              ('WHERE owner=? ' if owner else '') + 'ORDER BY updated DESC',
                              (owner,) if owner else ()).fetchall()
        return [json.loads(r['state']) for r in rows]

    def revision(self, sid, owner):
        with self.db() as db:
            row = db.execute('SELECT state FROM work_index WHERE id=? AND owner=?', (sid, owner)).fetchone()
        if row is None:
            raise ValueError('Session not found on host.')
        return json.loads(row['state'])['revision']

    def save(self, state, event=None):
        state['updated'] = time.time()
        state['revision'] = state.get('revision', 0) + 1
        try:
            previous = self.get(state['id'])
        except ValueError:
            previous = {}
        versions = previous.get('_view_versions', {})
        for key in ('order', 'pending', 'diff', 'error'):
            if state.get(key) != previous.get(key):
                versions[key] = state['revision']
        items = dict(versions.get('items', {}))
        for key, item in state.get('items', {}).items():
            if item != previous.get('items', {}).get(key):
                items[key] = state['revision']
        versions['items'] = items
        state['_view_versions'] = versions
        state['running'] = bool(state.get('handle'))
        state['needs_input'] = bool(state.get('pending'))
        state['has_error'] = bool(state.get('error'))
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO work_sessions VALUES(?,?,?,?)',
                       (state['id'], state['owner'], state['updated'], json.dumps(state)))
            index = {k: v for k, v in state.items()
                     if k not in ('items', 'order', 'terminal', 'diff', 'rpc', 'pending', 'partial', '_view_versions')}
            db.execute('INSERT OR REPLACE INTO work_index VALUES(?,?,?,?)',
                       (state['id'], state['owner'], state['updated'], json.dumps(index)))
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


class WorkRuntime:
    def __init__(self, path, registry):
        self.store = RuntimeStore(path)
        self.registry = registry
        self.locks = {}
        self.pump = None

    def lock(self, sid):
        return self.locks.setdefault(sid, asyncio.Lock())

    async def start(self):
        self.pump = asyncio.create_task(self.collect())

    async def stop(self):
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)

    async def rpc(self, state, cap, args, timeout=12):
        result = await self.registry.call(cap, **args)
        if result.get('ok') is False:
            raise ValueError(result.get('error') or 'Host operation failed.')
        return result

    async def collect(self):
        while True:
            try:
                for s in self.store.all(details=False):
                    if s.get('handle'):
                        await self.drain(s['id'])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Host Work collector failed')
            await asyncio.sleep(.35)

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
                if op == 'status' and data.get('status') == 'closed':
                    op = 'close'
                if op == 'status':
                    value = data.get('status')
                    if value not in ('auto', 'closed', 'blocked', 'pending'):
                        raise ValueError('Choose Auto, Closed, Blocked, or Pending.')
                    s['review_status'] = None if value == 'auto' else value
                    s['error'] = ''
                elif op == 'open':
                    if s.get('handle') or s.get('thread_id'):
                        raise ValueError('Session already exists; use resume after closing it.')
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
                    if s['pending'] or s['status'] not in ('ready', 'working') or not s['thread_id']:
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
                    s['status'] = 'working' if not s['pending'] else 'ready'
                    await self.send(s, response={'id': pending['id'], 'result': answer})
                elif op == 'close':
                    if s['handle']:
                        await self.rpc(s, 'proc.signal', {'handle': s['handle'], 'sig': 'TERM'})
                    s['status'] = 'closed'
                    s['review_status'] = 'closed'
                    s['pending'] = {}
                    s['handle'] = None
                elif op == 'resume':
                    if s['handle']:
                        raise ValueError('The agent process is still attached; stop it before reopening.')
                    if not s['thread_id']:
                        raise ValueError('No saved agent thread is available; start a new session.')
                    s.update(status='starting', review_status=None, cursor=0, partial='', rpc={}, pending={}, error='')
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
