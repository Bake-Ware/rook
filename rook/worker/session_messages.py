"""Local delivery to existing agent sessions. Never launch a duplicate session.

Receipts live on the worker and contain no message bodies. A claimed command
is never automatically replayed after an uncertain outcome or worker restart.
"""
import asyncio
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from weakref import WeakValueDictionary

from . import codex_input


def claude_endpoint(session_id, home=None, proc_root=Path('/proc')):
    """Match a live process, exact session, private socket and its peer key."""
    home = home or Path.home() / '.claude'
    matches = []
    for marker in (home / 'sessions').glob('*.json'):
        try:
            data = json.loads(marker.read_text())
            if data.get('sessionId') != session_id or data.get('peerProtocol') != 1:
                continue
            pid = int(data['pid'])
            process = proc_root / str(pid)
            fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z' or (process / 'comm').read_text().strip() != 'claude':
                continue
            if not data.get('procStart') or str(data['procStart']) != fields[19]:
                continue
            path = Path(data['messagingSocketPath'])
            info = path.lstat()
            if not path.is_absolute() or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                continue
            digest = hashlib.sha256(str(path).encode()).hexdigest()
            key = home / 'sessions' / f'{pid}.{digest}.key'
            info = key.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                continue
            auth = json.loads(key.read_text())
            if str(auth.get('procStart')) != fields[19] or not re.fullmatch('[0-9a-f]{32}', auth.get('peerToken', '')):
                continue
            matches.append((path, auth['peerToken'], pid))
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
    return matches[0] if len(matches) == 1 else None


def messageable(agent, session_id):
    return codex_input.available(session_id) if agent == 'codex' else claude_endpoint(session_id) is not None


@contextmanager
def _receipt_db():
    path = Path(os.environ.get('ROOK_WORK_DB', str(Path.home() / '.rook-band-worker/work.sqlite3')))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS session_message_receipts (agent TEXT, session TEXT, command TEXT, result TEXT, updated REAL, PRIMARY KEY(agent,session,command))')
            yield db
    finally:
        db.close()


_delivery_locks = WeakValueDictionary()


async def deliver(agent, session_id, command_id, text):
    # Different operators can target the same host session. Keep paste/Enter
    # pairs and runtime state checks together, including across web entries.
    key = (agent, session_id)
    lock = _delivery_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _deliver(agent, session_id, command_id, text)


async def _deliver(agent, session_id, command_id, text):
    if not isinstance(text, str) or not text.strip() or len(text) > 24000:
        return {'ok': False, 'error': 'Enter a message of 1–24000 characters.'}
    if not isinstance(command_id, str) or not 8 <= len(command_id) <= 100:
        return {'ok': False, 'error': 'A command ID is required.'}
    key = (agent, session_id, command_id)
    uncertain = {'ok': False, 'error': 'Delivery outcome is uncertain. Check the host before sending again.'}
    with _receipt_db() as db:
        inserted = db.execute('INSERT OR IGNORE INTO session_message_receipts VALUES (?,?,?,?,?)',
                              (*key, json.dumps(uncertain), time.time())).rowcount
        if not inserted:
            return json.loads(db.execute('SELECT result FROM session_message_receipts WHERE agent=? AND session=? AND command=?', key).fetchone()[0])
    result = uncertain
    try:
        if agent == 'codex':
            result = await asyncio.wait_for(codex_input.send(session_id, text), 20)
        else:
            endpoint = claude_endpoint(session_id)
            if endpoint is None:
                raise ValueError('This Claude session has no available local messaging inbox. Resume it on the host first.')
            path, token, pid = endpoint
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 5)
            try:
                peer = writer.get_extra_info('socket')
                if hasattr(socket, 'SO_PEERCRED'):
                    import struct
                    actual_pid, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if actual_pid != pid or uid != os.getuid():
                        raise ValueError('Claude inbox owner changed. Refresh the session before sending.')
                frames = [{'type': 'auth', 'token': token}, {'type': 'user', 'session_id': session_id,
                    'message': {'role': 'user', 'content': text}, 'priority': 'next', 'from': 'Rook Work',
                    'uuid': command_id}]
                writer.write(('\n'.join(json.dumps(frame) for frame in frames) + '\n').encode())
                await asyncio.wait_for(writer.drain(), 5)
                # This protocol has no in-band acceptance acknowledgment. Report
                # transport submission, never claim the agent accepted the prompt.
                result = {'ok': True, 'delivery': 'forwarded', 'note': 'Message sent to the Claude inbox. Refresh the conversation to confirm it was accepted.'}
            finally:
                writer.close()
                await writer.wait_closed()
    except asyncio.TimeoutError:
        pass
    except (OSError, ValueError) as error:
        result = {'ok': False, 'error': str(error) if isinstance(error, ValueError) else 'Unable to reach the agent on its host.'}
    with _receipt_db() as db:
        db.execute('UPDATE session_message_receipts SET result=?, updated=? WHERE agent=? AND session=? AND command=?',
                   (json.dumps(result), time.time(), *key))
    return result
