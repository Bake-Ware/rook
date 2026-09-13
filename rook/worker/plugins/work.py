"""work.* — host-owned sessions initiated by the web UI."""
import hashlib
import json
import os
import re
import shutil
import time
import threading
import uuid
from pathlib import Path

from ..plugin import Plugin, capability
from ..work_runtime import WorkRuntime


class WorkPlugin(Plugin):
    NAMESPACE = 'work'

    def __init__(self):
        super().__init__()
        self.runtime = None
        self.views = {}
        self.view_lock = threading.RLock()
        self.adopt_lock = threading.RLock()

    def available(self):
        path = Path(os.environ.get('ROOK_WORK_DB', '~/.rook-band-worker/work.sqlite3')).expanduser()
        return bool(shutil.which('codex')) or path.exists()

    def bind_worker(self, worker):
        path = Path(os.environ.get('ROOK_WORK_DB', '~/.rook-band-worker/work.sqlite3')).expanduser()
        self.runtime = WorkRuntime(path, worker.registry)

    async def start(self):
        await self.runtime.start()

    async def stop(self):
        await self.runtime.stop()
        self.views.clear()

    @staticmethod
    def session_id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
            raise ValueError('Invalid session ID.')
        return value

    @staticmethod
    def metadata(s):
        return {k: s.get(k) for k in ('id', 'title', 'cwd', 'model', 'thread_id',
                'turn_id', 'status', 'revision', 'updated')} | {
                    'running': s.get('running', bool(s.get('handle'))), 'needs_input': s.get('needs_input', bool(s.get('pending'))),
                    'has_error': s.get('has_error', bool(s.get('error')))}

    @capability('create')
    async def create(self, session_id: str, command_id: str, cwd: str, title: str = '', model: str = ''):
        sid = self.session_id(session_id)
        if not Path(cwd).is_absolute() or len(cwd) > 2000:
            raise ValueError('An absolute working directory is required.')
        try:
            state = self.runtime.store.get(sid)
        except ValueError:
            state = dict(id=sid, owner='host', title=title[:160], cwd=cwd, model=model[:100],
                         agent='codex', status='starting', items={}, order=[], pending={}, rpc={},
                         cursor=0, partial='', handle=None, thread_id=None, turn_id=None, error='', diff='')
            self.runtime.store.save(state)
        # Both process creation and subsequent commands are deduplicated on the host.
        return await self.command(sid, dict(op='open', id=command_id))

    @capability('command')
    async def command(self, session_id: str, command: dict):
        sid = self.session_id(session_id)
        self.runtime.store.get(sid)
        cid = str(command.get('id', ''))
        if not 8 <= len(cid) <= 100:
            raise ValueError('A command ID is required.')
        if self.runtime.store.claim(sid, cid):
            await self.runtime.command(sid, command)
        result = self.runtime.store.result(sid, cid)
        return {'ok': True, 'session': self.metadata(self.runtime.store.get(sid)),
                'result': {'status': result['status']}}

    @capability('status')
    def status(self, sessions: list[str]):
        if len(sessions) > 20:
            raise ValueError('At most 20 sessions per status request.')
        ids = [self.session_id(sid) for sid in sessions]
        with self.runtime.store.db() as db:
            rows = db.execute('SELECT id,state FROM work_index WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
        found = {row['id']: self.metadata(json.loads(row['state'])) for row in rows}
        out = [found.get(sid, {'id': sid, 'missing': True}) for sid in ids]
        return {'ok': True, 'sessions': out}

    @capability('view_page')
    def view_page(self, session_id: str, since: int = 0, token: str = '', offset: int = 0):
        """A stable, bounded page of a view delta; content never enters web storage."""
        with self.view_lock:
            return self._view_page(session_id, since, token, offset)

    def _view_page(self, session_id, since, token, offset):
        sid = self.session_id(session_id)
        now = time.monotonic()
        self.views = {k: v for k, v in self.views.items() if now - v['used'] < 120}
        if not token:
            if offset != 0 or since < 0:
                raise ValueError('Invalid view cursor.')
            s = self.runtime.store.get(sid)
            versions = s.get('_view_versions', {})
            view = {k: s.get(k) for k in ('order', 'pending', 'diff', 'error')
                    if not since or versions.get(k, 0) > since}
            view['items'] = {k: v for k, v in s['items'].items()
                             if not since or versions.get('items', {}).get(k, 0) > since}
            token = uuid.uuid4().hex
            # A bounded number of worker-local views; readers can retry if evicted.
            while len(self.views) >= 8:
                del self.views[next(iter(self.views))]
            self.views[token] = dict(sid=sid, used=now, revision=s['revision'],
                                    data=json.dumps(view, ensure_ascii=False))
        snapshot = self.views.get(token)
        if not snapshot or snapshot['sid'] != sid:
            raise ValueError('View expired. Refresh from host.')
        text = snapshot['data']
        if offset < 0 or offset > len(text):
            raise ValueError('Invalid view cursor.')
        snapshot['used'] = now
        chunk = text[offset:offset + 6000]
        end = offset + len(chunk)
        return dict(ok=True, token=token, data=chunk, next_offset=end,
                    truncated=end < len(text), revision=snapshot['revision'])

    @capability('adopt_page')
    def adopt_page(self, session_id: str, digest: str, offset: int, data: str, final: bool = False):
        """Archive and adopt legacy web state before the web removes its copy."""
        with self.adopt_lock:
            return self._adopt_page(session_id, digest, offset, data, final)

    def _adopt_page(self, session_id, digest, offset, data, final):
        sid = self.session_id(session_id)
        if not re.fullmatch(r'[a-f0-9]{64}', digest) or len(data) > 6000 or offset < 0:
            raise ValueError('Invalid migration page.')
        root = self.runtime.store.path.parent / 'work-migrations'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / (sid + '-' + digest + '.json')
        part = path.with_suffix('.part')
        if path.exists():
            return dict(ok=True, adopted=True, digest=digest)
        raw = data.encode('utf-8')
        if offset == 0:
            part.write_bytes(b'')
            part.chmod(0o600)
        with part.open('r+b') as stream:
            stream.seek(0, 2)
            if stream.tell() != offset:
                raise ValueError('Migration cursor mismatch.')
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if not final:
            return dict(ok=True, adopted=False, next_offset=offset + len(raw))
        payload = part.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError('Migration checksum mismatch.')
        archive = json.loads(payload)
        state = archive['state']
        if state['id'] != sid:
            raise ValueError('Migration session mismatch.')
        try:
            existing = self.runtime.store.get(sid)
        except ValueError:
            existing = None
        if existing is not None and not existing.get('_migration_digest'):
            raise ValueError('A different host session already uses this ID. Migration was not applied.')
        if existing is None:
            state.setdefault('items', {})
            state.setdefault('order', [])
            state['_migration_digest'] = digest
            self.runtime.store.save(state)
        with self.runtime.store.db() as db:
            for command in archive.get('commands', []):
                db.execute('INSERT OR IGNORE INTO work_commands VALUES(?,?,?)',
                           (sid, command['id'], command['result']))
        # Archive includes raw events too. Rename only after durable runtime adoption.
        part.replace(path)
        return dict(ok=True, adopted=True, digest=digest)


PLUGIN = WorkPlugin
