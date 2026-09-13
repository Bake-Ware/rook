"""Direct input to an existing Codex runtime. Never queue or resume a copy."""
import asyncio
import os
import re
import shutil
import stat
from pathlib import Path

import aiohttp


def control_socket():
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    path = Path(os.environ.get('ROOK_CODEX_CONTROL_SOCKET', str(home / 'app-server-control/app-server-control.sock')))
    try:
        info = path.lstat()
        if path.is_absolute() and stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
            return path
    except OSError:
        pass


def terminal_endpoint(session_id, proc_root=Path('/proc')):
    """Only the native process holding this exact rollout, on its original TTY."""
    if not re.fullmatch(r'[0-9a-f-]{36}', session_id):
        return None
    binary = shutil.which('qdbus6') or shutil.which('qdbus')
    if not binary:
        return None
    matches = []
    for process in proc_root.glob('[0-9]*'):
        try:
            if process.stat().st_uid != os.getuid() or (process / 'comm').read_text().strip() != 'codex':
                continue
            fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                continue
            logs = [os.readlink(fd) for fd in (process / 'fd').iterdir()]
            if not any(Path(p).name.endswith('-' + session_id + '.jsonl') for p in logs):
                continue
            tty = os.readlink(process / 'fd/0')
            if not re.fullmatch(r'/dev/pts/\d+', tty) or os.readlink(process / 'fd/1') != tty:
                continue
            env = {}
            for value in (process / 'environ').read_bytes().split(b'\0'):
                key, _, val = value.partition(b'=')
                if key in (b'KONSOLE_DBUS_SERVICE', b'KONSOLE_DBUS_SESSION'):
                    env[key.decode()] = val.decode()
            service, path = env.get('KONSOLE_DBUS_SERVICE', ''), env.get('KONSOLE_DBUS_SESSION', '')
            if not re.fullmatch(r':\d+\.\d+', service) or not re.fullmatch(r'/Sessions/\d+', path):
                continue
            matches.append(dict(binary=binary, service=service, path=path, pid=int(process.name),
                                start=fields[19], group=int(fields[2]), tty=tty))
        except (OSError, ValueError, IndexError):
            continue
    return matches[0] if len(matches) == 1 else None


def available(session_id):
    # Discovery is read-only. Revalidate live ownership and readiness on send.
    return control_socket() is not None or terminal_endpoint(session_id) is not None


async def _dbus(endpoint, method, *args, bus=False, literal=False):
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
    target = ['org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus.' + method] if bus else [
        endpoint['service'], endpoint['path'], 'org.kde.konsole.Session.' + method]
    proc = await asyncio.create_subprocess_exec(endpoint['binary'], *(['--literal'] if literal else []),
        *target, *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(proc.communicate(), 3)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        if b'org.freedesktop.DBus.Error.AccessDenied' in output + error:
            raise ValueError('Konsole blocks remote input: its Security sensitive D-Bus API setting is disabled on the host.')
        raise ValueError('The host terminal could not confirm delivery. Check it before retrying.')
    return output.decode('utf-8', errors='replace').strip()


def empty_composer(screen):
    # Fail closed on local drafts, wrapped input, dialogs, or an unfamiliar UI.
    lines = screen.rstrip().splitlines()
    prompts = [i for i, line in enumerate(lines) if line.lstrip().startswith('›')]
    if not prompts:
        return False
    i = prompts[-1]
    return (lines[i].strip() == '› Ask Codex to do anything'
            and len(lines) - i <= 4 and any('gpt-' in line and ' · ' in line for line in lines[i+1:])
            and all(not line.strip() or ('gpt-' in line and ' · ' in line) for line in lines[i+1:]))


async def _check_terminal(session_id, endpoint):
    if terminal_endpoint(session_id) != endpoint:
        raise ValueError('The active Codex terminal changed. Refresh before sending.')
    owner = int(await _dbus(endpoint, 'GetConnectionUnixProcessID', endpoint['service'], bus=True))
    # The D-Bus owner must be an ancestor of the exact native Codex process.
    pid, ancestors = endpoint['pid'], set()
    for _ in range(32):
        if pid <= 1 or pid in ancestors:
            break
        ancestors.add(pid)
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        pid = int(fields[1])
    if owner not in ancestors or int(await _dbus(endpoint, 'foregroundProcessId')) != endpoint['group']:
        raise ValueError('Codex is not the foreground process in its host terminal.')
    for method in ('copyingSessions', 'feederSessions'):
        if await _dbus(endpoint, method, literal=True) != '[Argument: ai {}]':
            raise ValueError('Turn off shared terminal input on the host before sending.')


async def _terminal_send(session_id, text, endpoint):
    if any(ord(c) < 32 and c not in '\n\t' or 127 <= ord(c) < 160 for c in text):
        raise ValueError('Terminal messages cannot contain control characters.')
    if text.lstrip().startswith(('/', '!')):
        raise ValueError('Run terminal commands on the host; Work sends conversation messages.')
    await _check_terminal(session_id, endpoint)
    if not empty_composer(await _dbus(endpoint, 'getAllDisplayedText')):
        raise ValueError('The host terminal has a draft or dialog open. Clear it on the host before sending.')
    # Bracketed paste preserves literal newlines and prevents text becoming keys.
    await _dbus(endpoint, 'sendText', '\x1b[200~' + text + '\x1b[201~')
    await asyncio.sleep(.15)
    await _check_terminal(session_id, endpoint)
    pasted = await _dbus(endpoint, 'getAllDisplayedText')
    if (empty_composer(pasted) or not pasted.splitlines()
            or not re.search(r'gpt-[^\n]+ · ', pasted.splitlines()[-1])
            or not any(line.lstrip().startswith('›') for line in pasted.splitlines())):
        raise ValueError('The terminal did not show a ready composer with the pasted message. Check the host before retrying.')
    await _dbus(endpoint, 'sendText', '\r')
    return dict(ok=True, delivery='forwarded', note='Message sent directly to the open Codex terminal. Check the conversation for acceptance.')


async def _app_send(path, session_id, text):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(path)),
                                    timeout=aiohttp.ClientTimeout(total=12)) as client:
        async with client.ws_connect('http://localhost/', max_msg_size=2**20) as ws:
            seq = 0
            async def request(method, params):
                nonlocal seq
                seq += 1
                await ws.send_json(dict(id=seq, method=method, params=params))
                async with asyncio.timeout(5):
                    async for frame in ws:
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            raise ValueError('Codex disconnected. Check the host before retrying.')
                        data = frame.json()
                        if data.get('id') == seq and ('result' in data or 'error' in data):
                            if 'error' in data:
                                raise ValueError('Codex rejected direct input. Refresh the session before retrying.')
                            return data['result']
                        if 'id' in data and 'method' in data:
                            raise ValueError('Codex needs an answer on its host before more messages can be sent.')
                raise ValueError('Codex disconnected. Check the host before retrying.')
            await request('initialize', dict(clientInfo=dict(name='rook_work', version='1.0'),
                                             capabilities=dict(experimentalApi=True)))
            await ws.send_json(dict(method='initialized'))
            cursor, found = None, False
            for _ in range(20):
                loaded = await request('thread/loaded/list', dict(limit=100, cursor=cursor))
                if session_id in loaded['data']:
                    found = True
                    break
                cursor = loaded.get('nextCursor')
                if not cursor:
                    break
            if not found:
                return None  # No write occurred; an exact terminal may own it.
            thread = (await request('thread/read', dict(threadId=session_id, includeTurns=False)))['thread']
            status = thread.get('status', {})
            if thread.get('canAcceptDirectInput') is False or status.get('activeFlags'):
                raise ValueError('Codex needs attention on its host before receiving more messages.')
            params = dict(threadId=session_id, input=[dict(type='text', text=text)])
            method = 'turn/start'
            if status.get('type') == 'active':
                turns = await request('thread/turns/list', dict(threadId=session_id, limit=1,
                                      sortDirection='desc', itemsView='notLoaded'))
                turn = next((t for t in turns['data'] if t.get('status') == 'inProgress'), None)
                if not turn:
                    raise ValueError('The active turn changed. Refresh before sending.')
                method, params['expectedTurnId'] = 'turn/steer', turn['id']
            elif status.get('type') != 'idle':
                raise ValueError('Codex is not ready for direct input. Check the host session.')
            await request(method, params)
            return dict(ok=True, delivery='steered' if method == 'turn/steer' else 'started',
                        note='Message delivered to the active turn.' if method == 'turn/steer' else 'Message accepted by Codex.')


async def send(session_id, text):
    path = control_socket()
    if path:
        # Errors after a possible write never fall back to another transport.
        try:
            result = await _app_send(path, session_id, text)
        except aiohttp.ClientError:
            raise ValueError('Codex connection failed. Check the host before retrying.') from None
        if result is not None:
            return result
    endpoint = terminal_endpoint(session_id)
    if endpoint:
        return await _terminal_send(session_id, text, endpoint)
    raise ValueError('This active Codex session has no direct input connection. Open it through a Codex app-server or a supported Konsole terminal.')
