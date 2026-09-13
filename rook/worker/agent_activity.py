"""Identify live agent sessions from exact process evidence, not shared cwd."""
import json
import os
import re
from pathlib import Path

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


def active_sessions(agent, proc_root=Path('/proc'), claude_home=None):
    """Return session paths/IDs held by live Linux agent processes.

    Claude PID markers cover idle sessions whose append-only log isn't kept
    open. Exact resume arguments cover resumed terminals. Never infer a match
    solely from a process in the same repository or a recently modified log.
    Other platforms currently return no process evidence.
    """
    paths, ids, pids = set(), set(), {}
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return paths, ids
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            comm = (process / 'comm').read_text().strip().lower()
            argv = (process / 'cmdline').read_bytes().decode('utf-8', errors='replace').split('\0')
            names = [Path(arg).name.lower() for arg in argv[:2]]
            if not (comm == agent or any(name in (agent, agent + '.exe', agent + '.js') for name in names)):
                continue
            # Don't treat zombie processes or stale PID marker files as active.
            stat = (process / 'stat').read_text().rsplit(')', 1)[1].split()
            if stat[0] == 'Z':
                continue
            pids[int(process.name)] = stat[19] if len(stat) > 19 else None
            for i, arg in enumerate(argv[:-1]):
                if arg in ('resume', '--resume', '--session-id') and _UUID.fullmatch(argv[i + 1]):
                    ids.add(argv[i + 1].lower())
            try:
                for fd in (process / 'fd').iterdir():
                    try:
                        target = os.readlink(fd)
                        if target.endswith('.jsonl'):
                            paths.add(str(Path(target).resolve()))
                    except OSError:
                        continue
            except OSError:
                continue
        except (OSError, ValueError, IndexError):
            continue
    if agent == 'claude':
        home = claude_home or Path.home() / '.claude'
        for marker in (home / 'sessions').glob('*.json'):
            try:
                data = json.loads(marker.read_text())
                pid = int(data.get('pid', marker.stem))
                sid = data.get('sessionId') or data.get('session_id')
                if (pid in pids and (not data.get('procStart') or str(data['procStart']) == pids[pid])
                        and isinstance(sid, str) and _UUID.fullmatch(sid)):
                    ids.add(sid.lower())
            except (OSError, ValueError, TypeError):
                continue
    return paths, ids
