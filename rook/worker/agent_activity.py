"""Identify live agent sessions from exact process evidence, not shared cwd.

Linux reads ``/proc`` (open transcript files, exact resume arguments, Claude
PID markers checked against the process start tick). macOS lists processes
with ``ps`` (and ``lsof`` for open transcripts); Windows uses the Toolhelp
process snapshot, which has no command lines, so there only Claude's PID
markers count. Claude writes ``~/.claude/sessions/<pid>.json`` on every OS.
A marker is trusted only while its PID is a plausible Claude process that is
the one that wrote it: ``procStart`` must equal the process start (the
/proc start tick on Linux, the creation FILETIME on Windows); where neither
is available (macOS), the process must have started within a few minutes of
the marker's ``startedAt``.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


def active_sessions(agent, proc_root=Path('/proc'), claude_home=None):
    """Return session paths/IDs held by live Linux agent processes.

    Claude PID markers cover idle sessions whose append-only log isn't kept
    open. Exact resume arguments cover resumed terminals. Never infer a match
    solely from a process in the same repository or a recently modified log.
    Off Linux the same evidence comes from the platform process list.
    """
    if proc_root == Path('/proc') and not proc_root.is_dir():
        return _active_elsewhere(agent, claude_home)
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


# -- every platform -------------------------------------------------------------

# Process images a Claude Code CLI runs as: the native build, or node/bun
# running the npm package (Windows reports image names only).
_CLAUDE_HOSTS = ('claude', 'node', 'bun')
# How far a process start may sit from a marker's startedAt (seconds) off
# Linux, where no exact start tick is available. Guards against PID reuse.
MARKER_START_SLACK = 300
_RESUME_FLAGS = ('resume', '--resume', '--session-id')


def _stem(name):
    name = Path(str(name or '')).name.lower()
    for ext in ('.exe', '.cmd', '.js'):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def process_table(proc_root=Path('/proc')):
    """``{pid: {name, argv, ppid, started, zombie, proc_start}}`` for this host.

    ``argv`` is None where the platform hides command lines (Windows);
    ``started`` is epoch seconds or None; ``proc_start`` is what Claude
    records as ``procStart``: the /proc start tick on Linux, the creation
    FILETIME on Windows, None on macOS."""
    if proc_root.is_dir():
        return _linux_table(proc_root)
    if proc_root != Path('/proc'):
        return {}
    try:
        return _windows_table() if sys.platform == 'win32' else _ps_table()
    except Exception:
        return {}


def _linux_table(proc_root):
    try:
        tick = os.sysconf('SC_CLK_TCK')
        btime = next(int(line.split()[1]) for line in (proc_root / 'stat').read_text().splitlines()
                     if line.startswith('btime '))
    except (OSError, ValueError, StopIteration, AttributeError):
        tick = btime = None
    out = {}
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return out
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            stat = (process / 'stat').read_text().rsplit(')', 1)[1].split()
            argv = (process / 'cmdline').read_bytes().decode('utf-8', errors='replace').split('\0')
            start = stat[19] if len(stat) > 19 else None
            out[int(process.name)] = dict(
                name=(process / 'comm').read_text().strip(), argv=[a for a in argv if a] or None,
                ppid=int(stat[1]) if len(stat) > 1 and stat[1].isdigit() else None,
                zombie=stat[0] == 'Z', proc_start=start,
                started=btime + int(start) / tick if tick and btime and start and start.isdigit() else None)
        except (OSError, ValueError, IndexError):
            continue
    return out


def _etime(text):
    """Seconds from ps's ``[[dd-]hh:]mm:ss``."""
    days, _, rest = text.rpartition('-')
    parts = [int(p) for p in rest.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _ps_table(run=subprocess.run):
    """macOS and other Unixes without /proc. Arguments are split on spaces,
    which is enough to find an exact session id after a resume flag."""
    ps = shutil.which('ps') or '/bin/ps'
    out = run([ps, '-axww', '-o', 'pid=,ppid=,stat=,etime=,args='],
              capture_output=True, text=True, timeout=5).stdout
    now, table = time.time(), {}
    for line in out.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5:
            continue
        try:
            argv = fields[4].split()
            table[int(fields[0])] = dict(name=Path(argv[0]).name if argv else '', argv=argv or None,
                                         ppid=int(fields[1]), zombie=fields[2].startswith('Z'),
                                         started=now - _etime(fields[3]), proc_start=None)
        except ValueError:
            continue
    return table


def _windows_table():
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snap = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    table = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            pid, started, created = int(entry.th32ProcessID), None, None
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                times = [wintypes.FILETIME() for _ in range(4)]
                if kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                    started, created = (ticks - 116444736000000000) / 1e7, str(ticks)
                kernel.CloseHandle(handle)
            table[pid] = dict(name=entry.szExeFile, argv=None, ppid=int(entry.th32ParentProcessID),
                              zombie=False, started=started, proc_start=created)
            more = kernel.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snap)
    return table


def _is_agent(info, agent):
    return _stem(info.get('name')) == agent or agent in [_stem(a) for a in (info.get('argv') or [])[:2]]


def claude_markers(claude_home=None, table=None, proc_root=Path('/proc')):
    """Live Claude sessions from their PID markers: ``{session_id: marker}``.

    Each marker carries ``pid``, ``cwd``, ``status`` (``busy``/``idle`` in
    current Claude Code), ``startedAt`` and the process ``argv`` where the
    platform shows it. A marker counts only while its process is alive and is
    the process that wrote it."""
    home = claude_home or Path.home() / '.claude'
    table = process_table(proc_root) if table is None else table
    out = {}
    for marker in (home / 'sessions').glob('*.json'):
        try:
            data = json.loads(marker.read_text())
            pid = int(data.get('pid', marker.stem))
            sid = data.get('sessionId') or data.get('session_id')
            info = table.get(pid)
            if not (info and not info['zombie'] and isinstance(sid, str) and _UUID.fullmatch(sid)):
                continue
            if not (_is_agent(info, 'claude') or _stem(info.get('name')) in _CLAUDE_HOSTS):
                continue
            if info.get('proc_start') is not None:
                if data.get('procStart') and str(data['procStart']) != info['proc_start']:
                    continue
            elif info.get('started') and isinstance(data.get('startedAt'), (int, float)):
                if abs(info['started'] - data['startedAt'] / 1000) > MARKER_START_SLACK:
                    continue
            out[sid.lower()] = dict(data, pid=pid, argv=info.get('argv'))
        except (OSError, ValueError, TypeError):
            continue
    return out


def _session_from_path(agent, path):
    if agent == 'claude':
        stem = Path(path).stem
        return stem.lower() if _UUID.fullmatch(stem) else None
    match = _UUID.search(Path(path).name)
    return match.group().lower() if match else None


def _open_logs_lsof(pids, run=subprocess.run):
    """Open ``.jsonl`` files per PID through lsof (macOS)."""
    lsof = shutil.which('lsof')
    if not lsof or not pids:
        return {}
    out = run([lsof, '-n', '-P', '-Fpn', '-p', ','.join(str(p) for p in sorted(pids))],
              capture_output=True, text=True, timeout=5).stdout
    found, pid = {}, None
    for line in out.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith('n') and line.endswith('.jsonl') and pid is not None:
            found.setdefault(pid, set()).add(line[1:])
    return found


def _open_logs(pid, proc_root):
    logs = set()
    try:
        for fd in (proc_root / str(pid) / 'fd').iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.endswith('.jsonl'):
                logs.add(target)
    except OSError:
        pass
    return logs


def session_owners(agent, table=None, claude_home=None, proc_root=Path('/proc')):
    """``{pid: session_id}``: the processes holding live ``agent`` sessions
    (Claude PID markers, exact resume arguments, open transcripts)."""
    table = process_table(proc_root) if table is None else table
    direct = {}
    if agent == 'claude':
        for sid, marker in claude_markers(claude_home, table, proc_root).items():
            direct[marker['pid']] = sid
    agents = [pid for pid, info in table.items() if not info['zombie'] and _is_agent(info, agent)]
    if proc_root.is_dir():
        logs = {pid: _open_logs(pid, proc_root) for pid in agents}
    elif sys.platform == 'darwin':
        try:
            logs = _open_logs_lsof(agents)
        except Exception:
            logs = {}
    else:
        logs = {}
    for pid in agents:
        argv = table[pid].get('argv') or []
        for i, arg in enumerate(argv[:-1]):
            if arg in _RESUME_FLAGS and _UUID.fullmatch(argv[i + 1]):
                direct.setdefault(pid, argv[i + 1].lower())
        for path in logs.get(pid, ()):
            sid = _session_from_path(agent, path)
            if sid:
                direct.setdefault(pid, sid)
    return direct


def session_under(root_pid, owners, table, depth=8):
    """The one session held by ``root_pid`` or a process below it (a terminal's
    child: npm's ``codex`` shim runs the native binary as a child). None when
    there is none or more than one."""
    found = set()
    for pid, sid in owners.items():
        for _ in range(depth):
            if pid == root_pid:
                found.add(sid)
                break
            pid = (table.get(pid) or {}).get('ppid')
            if not pid:
                break
    return found.pop() if len(found) == 1 else None


def _active_elsewhere(agent, claude_home=None):
    """active_sessions() for macOS and Windows."""
    paths, ids = set(), set()
    table = process_table()
    agents = [pid for pid, info in table.items() if not info['zombie'] and _is_agent(info, agent)]
    for pid in agents:
        argv = table[pid].get('argv') or []
        for i, arg in enumerate(argv[:-1]):
            if arg in _RESUME_FLAGS and _UUID.fullmatch(argv[i + 1]):
                ids.add(argv[i + 1].lower())
    if sys.platform == 'darwin':
        try:
            for found in _open_logs_lsof(agents).values():
                paths.update(str(Path(p).resolve()) for p in found)
        except Exception:
            pass
    if agent == 'claude':
        ids.update(claude_markers(claude_home, table))
    return paths, ids
