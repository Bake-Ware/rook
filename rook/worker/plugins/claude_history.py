"""claude-history.* — read Claude Code session histories on the local machine.

Claude Code stores conversation sessions as JSONL files at
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` (Windows uses
``%USERPROFILE%\\.claude\\projects\\``). Each line is one record — most are
``user``/``assistant`` turns, with sidebands like ``queue-operation``,
``ai-title``, ``last-prompt``.

The ``machine`` argument from the SDD is accepted for caller-side ergonomics
but ignored here: the worker is already running on the target machine, so
cross-host routing is the orchestrator's responsibility.

``resume`` is the write half: it relaunches a stored session on this machine
with Remote Control enabled, so a conversation that was closed on the PC
becomes reachable again from claude.ai without anyone being at the keyboard.
Where the worker has live terminals (``work.stream.*``) the session reopens
in a Rook terminal, so it streams to the Sessions page like any other
(docs/design/sessions.md §3.2); the result names the terminal. Older or
pty-less workers run it through ``proc.*`` (pty-backed — Remote Control needs
an interactive session, which needs a tty), so the relaunched session is a
normal worker process: visible in ``proc.list``, signalable, and pumped into a
console room the band can watch.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Iterable

from ..plugin import Plugin, capability
from ..agent_activity import active_sessions
from ..session_messages import deliver, messageable


def _default_root() -> Path:
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    else:
        home = Path.home()
    return home / ".claude" / "projects"


def _expand(path: str | None) -> Path:
    if not path:
        return _default_root()
    return Path(os.path.expanduser(os.path.expandvars(path)))


def _iter_session_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    if root.is_file():
        if root.suffix == ".jsonl":
            yield root
        return
    yield from root.rglob("*.jsonl")


def _read_lines(p: Path) -> Iterable[dict]:
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _claude_bin() -> str | None:
    """Locate the Claude Code CLI. On Windows npm installs it as a .cmd shim,
    which shutil.which misses unless asked by that name."""
    import shutil
    if sys.platform == "win32":
        found = shutil.which("claude") or shutil.which("claude.cmd")
        if found:
            return found
        for c in (r"C:\nvm4w\nodejs\claude.cmd",
                  str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"),
                  r"C:\Program Files\nodejs\claude.cmd"):
            if os.path.exists(c):
                return c
        return None
    from ..shim import which_real     # never the session shim itself
    return which_real("claude")


def _short_id(sid: str) -> str:
    return sid.split("-", 1)[0] if sid else ""


def _resolve_session(root: Path, session_id: str) -> Path | None:
    """Find a session file by full UUID or short prefix anywhere under root."""
    if not session_id:
        return None
    needle = session_id.lower()
    exact: list[Path] = []
    prefix: list[Path] = []
    for p in _iter_session_files(root):
        stem = p.stem.lower()
        if stem == needle:
            exact.append(p)
        elif stem.startswith(needle):
            prefix.append(p)
    if exact:
        return exact[0]
    if len(prefix) == 1:
        return prefix[0]
    if prefix:
        prefix.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return prefix[0]
    return None


def _message_text(record: dict) -> str:
    """Best-effort extraction of human-readable text from a record."""
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if content is None:
        content = record.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if "text" in block and isinstance(block["text"], str):
                    parts.append(block["text"])
                elif btype == "tool_use":
                    parts.append(f"[tool_use: {block.get('name', '')}]")
                    inp = block.get("input")
                    if isinstance(inp, (dict, list)):
                        try:
                            parts.append(json.dumps(inp)[:2000])
                        except (TypeError, ValueError):
                            pass
                elif btype == "tool_result":
                    res = block.get("content")
                    if isinstance(res, str):
                        parts.append(res)
                    elif isinstance(res, list):
                        for r in res:
                            if isinstance(r, dict) and isinstance(r.get("text"), str):
                                parts.append(r["text"])
        return "\n".join(parts)
    try:
        return json.dumps(content)
    except (TypeError, ValueError):
        return ""


def _record_role(record: dict) -> str:
    rtype = record.get("type", "")
    if rtype in ("user", "assistant"):
        return rtype
    msg = record.get("message")
    if isinstance(msg, dict):
        role = msg.get("role")
        if isinstance(role, str):
            return role
    return rtype or "unknown"


def _title_candidate(text):
    """Ignore injected session setup when choosing a human-readable subject."""
    text = text.strip()
    if text.lower().startswith('# agents.md instructions'):
        return None
    while text:
        match = re.match(r'<(environment_context|user_instructions|permissions|environment|turn_aborted|system-reminder|local-command-caveat)(?:\s[^>]*)?>', text, re.I)
        if not match:
            break
        end = re.search(r'</' + re.escape(match[1]) + r'\s*>', text, re.I)
        if not end:
            return None
        text = text[end.end():].strip()
    return text.splitlines()[0][:120] if text else None


class _MetaAcc:
    """What a session's metadata is built from, record by record, so an
    append-only log can be read on from where the last scan stopped."""
    __slots__ = ("first_ts", "last_ts", "msg_count", "title", "ai_title", "cwd", "git_branch")

    def __init__(self) -> None:
        self.first_ts = self.last_ts = self.title = self.ai_title = self.cwd = self.git_branch = None
        self.msg_count = 0

    def copy(self) -> "_MetaAcc":
        out = _MetaAcc()
        for name in self.__slots__:
            setattr(out, name, getattr(self, name))
        return out

    def add(self, rec: dict) -> None:
        ts = rec.get("timestamp")
        if isinstance(ts, str):
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts
        rtype = rec.get("type")
        if rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
            self.ai_title = _title_candidate(rec["aiTitle"])
        elif rtype in ("user", "assistant"):
            self.msg_count += 1
            if self.title is None and rtype == "user":
                self.title = _title_candidate(_message_text(rec))
        if self.cwd is None and isinstance(rec.get("cwd"), str):
            self.cwd = rec["cwd"]
        if self.git_branch is None and isinstance(rec.get("gitBranch"), str):
            self.git_branch = rec["gitBranch"]

    def result(self, p: Path, sid: str, st) -> dict:
        return {
            "session_id": sid,
            "short_id": _short_id(sid),
            "path": str(p),
            "title": self.ai_title or self.title or "(empty)",
            "first_timestamp": self.first_ts,
            "last_timestamp": self.last_ts,
            "last_modified": st.st_mtime,
            "message_count": self.msg_count,
            "project": p.parent.name,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "size_bytes": st.st_size,
        }


def _session_meta(p: Path, reader=_read_lines, sid: str | None = None) -> dict:
    sid = sid or p.stem
    try:
        st = p.stat()
    except OSError as e:
        return {"session_id": sid, "path": str(p), "error": str(e)}
    acc = _MetaAcc()
    for rec in reader(p):
        if isinstance(rec, dict):
            acc.add(rec)
    return acc.result(p, sid, st)


# A log written to within this many seconds may belong to a running agent
# even when no process evidence names it (docs/design/sessions.md §3.1).
RECENT_SECS = 120
# follow(tail=N): at most this many messages, in one page of TAIL_CHARS with
# each message clipped to TAIL_CLIP characters.
TAIL_MAX = 200
TAIL_CHARS = 64000
TAIL_CLIP = 4000


def _activity_step(codex: bool, rec: dict, activity: str) -> str:
    """One record's effect on a session's activity (working/ready/pending)."""
    if codex:
        payload = rec.get('payload') or {}
        if not isinstance(payload, dict):
            return activity
        if rec.get('type') == 'event_msg':
            kind = payload.get('type')
            if kind in ('task_started', 'user_message'):
                return 'working'
            if kind in ('task_complete', 'task_completed', 'turn_aborted'):
                return 'ready'
        elif rec.get('type') == 'response_item':
            if payload.get('phase') == 'final' or (
                payload.get('type') == 'function_call' and
                str(payload.get('name', '')).split('.')[-1] in ('request_user_input', 'request_user_input_async')
            ):
                return 'ready'
            if payload.get('type') in ('function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'):
                return 'working'
        return activity
    msg = rec.get('message') or {}
    if rec.get('type') == 'user':
        return 'working'
    if rec.get('type') == 'assistant' and isinstance(msg, dict):
        if msg.get('stop_reason') in ('end_turn', 'stop_sequence'):
            return 'ready'
        if msg.get('stop_reason') == 'tool_use':
            blocks = msg.get('content')
            needs_input = isinstance(blocks, list) and any(
                isinstance(b, dict) and b.get('name') == 'AskUserQuestion' for b in blocks)
            return 'ready' if needs_input else 'working'
    return activity


def _activity_now(activity: str, mtime) -> str:
    """A log still 'working' that has been quiet for two minutes is pending."""
    if activity == 'working' and mtime is not None and time.time() - mtime > RECENT_SECS:
        return 'pending'
    return activity


def _parse_line(raw: bytes):
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _complete_lines(p: Path, pos: int):
    """``(record, end offset)`` for each whole line from byte ``pos``, then
    ``(record, None)`` for a last line with no newline yet (a write in
    progress, or a file that simply does not end in one)."""
    with open(p, "rb") as f:
        f.seek(pos)
        for raw in f:
            if not raw.endswith(b"\n"):
                yield _parse_line(raw), None
                return
            pos += len(raw)
            yield _parse_line(raw), pos


def _read_on(p: Path, pos: int, add) -> tuple[int, object]:
    """Feed every whole line from ``pos`` to ``add``; returns the new
    position and the parsed last line without a newline (or None)."""
    partial = None
    try:
        for rec, end in _complete_lines(p, pos):
            if end is None:
                partial = rec
                break
            add(rec)
            pos = end
    except OSError:
        pass
    return pos, partial


# (path, codex) -> what the last scan of that log found (see scan_session)
_SCANS: dict = {}
_SCAN_LOCK = threading.Lock()
SCAN_CACHE_MAX = 4096


def scan_session(p: Path, codex: bool = False, meta=None) -> tuple[dict, str]:
    """``(metadata, activity)`` of one transcript, cached by file version.

    The catalog asks for the same few dozen logs every few seconds, and
    reading each whole log again (twice: once for the metadata, once for the
    activity) is what made ``sessions.list`` slow on a host with hundreds of
    long sessions. Claude logs are append-only, so a log that grew is read
    on from where the last scan stopped; Codex logs (whose reader looks at
    the whole file) are read again only when they change. The activity is
    the raw one: callers apply :func:`_activity_now`."""
    try:
        st = p.stat()
    except OSError as e:
        return {"session_id": p.stem, "path": str(p), "error": str(e)}, "pending"
    key = (str(p), codex)
    ver = (st.st_ino, st.st_size, st.st_mtime_ns)
    with _SCAN_LOCK:
        prior = _SCANS.get(key)
    if prior and prior["ver"] == ver:
        return dict(prior["meta"]), prior["activity"]
    if codex:
        found = (meta or _session_meta)(p)
        activity = "pending"
        for rec in _read_lines(p):
            if isinstance(rec, dict):
                activity = _activity_step(True, rec, activity)
        state = dict(ver=ver, meta=found, activity=activity)
    else:
        resume = bool(prior) and prior.get("ino") == st.st_ino and st.st_size >= prior["pos"]
        acc = prior["acc"].copy() if resume else _MetaAcc()
        done = [prior["activity_done"] if resume else "pending"]

        def add(rec):
            if isinstance(rec, dict):
                acc.add(rec)
                done[0] = _activity_step(False, rec, done[0])
        pos, partial = _read_on(p, prior["pos"] if resume else 0, add)
        shown, activity = acc, done[0]
        if isinstance(partial, dict):
            # Counted now, and read again next time (it may still grow).
            shown = acc.copy()
            shown.add(partial)
            activity = _activity_step(False, partial, activity)
        found = shown.result(p, p.stem, st)
        state = dict(ver=ver, ino=st.st_ino, pos=pos, acc=acc, activity_done=done[0],
                     meta=found, activity=activity)
    with _SCAN_LOCK:
        _SCANS.pop(key, None)
        while len(_SCANS) >= SCAN_CACHE_MAX:
            _SCANS.pop(next(iter(_SCANS)))
        _SCANS[key] = state
    return dict(state["meta"]), state["activity"]


def _tool_result_only(rec: dict) -> bool:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return isinstance(content, list) and bool(content) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _row(rec: dict) -> dict:
    """One transcript message as follow/snapshot pages carry it. ``kind``
    marks a user record that only carries tool results (``tool_result``,
    plus ``error`` when one failed), so a viewer need not show it as a
    prompt."""
    row = {"role": _record_role(rec), "content": _message_text(rec)}
    if row["role"] == "user" and _tool_result_only(rec):
        row["kind"] = "tool_result"
        if any(b.get("is_error") for b in rec["message"]["content"]):
            row["error"] = True
    return row


class ClaudeHistoryPlugin(Plugin):
    NAMESPACE = "claude-history"

    # Format adapters let other local coding agents share the history operations.
    _default_root = staticmethod(_default_root)
    _expand = staticmethod(_expand)
    _resolve_session = staticmethod(_resolve_session)
    _session_meta = staticmethod(_session_meta)
    _iter_session_files = staticmethod(_iter_session_files)
    _read_lines = staticmethod(_read_lines)
    _session_id = staticmethod(lambda p: p.stem)

    def __init__(self) -> None:
        super().__init__()
        self._worker = None
        self._resume_lock = asyncio.Lock()
        self._history_snapshots = {}
        self._history_lock = threading.RLock()
        self._row_cache: dict = {}      # path -> messages of the last read (see _rows)
        # session_id -> proc handle, for sessions this plugin relaunched. Used
        # to refuse a second resume of a conversation that is already live —
        # two `claude --resume` processes on one session id would both write
        # the same transcript.
        self._resumed_handles: dict[str, str] = {}
        # session_id -> Rook terminal id, for resumes through work.stream.open.
        self._resumed_terminals: dict[str, str] = {}

    def bind_worker(self, worker) -> None:
        """Grab a handle to the Worker so resume can drive the proc.* caps
        instead of growing its own copy of process management."""
        self._worker = worker

    def available(self) -> bool:
        # Only where Claude Code history actually lives on this host.
        return self._default_root().is_dir()

    async def _resume_in_terminal(self, sid: str, workdir: str | None, label: str,
                                  remote_control: bool = False) -> dict | None:
        """Reopen ``sid`` in a Rook terminal (work.stream.open) so it streams
        like any other; None when this worker has no live terminals."""
        if self._worker is None or not self._worker.registry.has("work.stream.open"):
            return None
        agent = self.NAMESPACE.split("-")[0]
        args = dict(harness=agent, resume=sid, cwd=workdir or "", title=f"{agent}: {label}"[:160])
        if remote_control and agent == "claude":
            args["remote_control"] = label[:80]
        try:
            opened = await self._worker.registry.call("work.stream.open", **args)
        except ValueError as error:
            return {"ok": False, "error": str(error), "session_id": sid}
        self._resumed_terminals[sid] = opened["id"]
        return {"ok": True, "session_id": sid, "short_id": _short_id(sid), "name": label,
                "cwd": opened.get("cwd") or workdir, "terminal": opened["id"], "pid": opened.get("pid"),
                "remote_control": bool(args.get("remote_control")),
                "note": (f"{agent} is starting in Rook terminal {opened['id']}: follow it with "
                         "work.stream.read, type with work.stream.write, stop it with "
                         "work.stream.close (or sessions.stop).")}

    def _scan(self, path):
        """``(metadata, raw activity)`` of one log, cached (scan_session)."""
        return scan_session(path, codex=self.NAMESPACE == "codex-history", meta=self._session_meta)

    def _rows(self, sp):
        """The conversation's messages (user/assistant rows), cached per log;
        a Claude log that grew is read on from where the last read stopped."""
        st = sp.stat()
        key, ver = str(sp), (st.st_ino, st.st_size, st.st_mtime_ns)
        cache = self._row_cache
        prior = cache.get(key)
        if prior and prior["ver"] == ver:
            return prior["rows"]
        claude = self.NAMESPACE == "claude-history"
        if claude:
            resume = bool(prior) and prior["ino"] == st.st_ino and st.st_size >= prior["pos"]
            rows = list(prior["rows_done"]) if resume else []

            def add(rec):
                if isinstance(rec, dict) and rec.get("type") in ("user", "assistant"):
                    rows.append(_row(rec))
            pos, partial = _read_on(sp, prior["pos"] if resume else 0, add)
            done = list(rows)
            if isinstance(partial, dict) and partial.get("type") in ("user", "assistant"):
                rows.append(_row(partial))
            entry = dict(ver=ver, ino=st.st_ino, pos=pos, rows_done=done, rows=rows)
        else:
            rows = [_row(rec) for rec in self._read_lines(sp)
                    if isinstance(rec, dict) and rec.get("type") in ("user", "assistant")]
            entry = dict(ver=ver, rows=rows)
        cache.pop(key, None)
        while len(cache) >= 8:
            cache.pop(next(iter(cache)))
        cache[key] = entry
        return rows

    def _is_active(self, path, processes=None):
        paths, ids = processes if processes is not None else active_sessions(self.NAMESPACE.split('-')[0])
        return str(path.resolve()) in paths or self._session_id(path).lower() in ids

    @capability("send")
    async def _send(self, session_id: str, text: str, command_id: str) -> dict:
        """Send to this exact existing session; transcripts stay on this host."""
        path = self._resolve_session(self._default_root(), session_id)
        if path is None or self._session_id(path) != session_id:
            return {"ok": False, "error": "Exact session ID not found on this host."}
        agent = self.NAMESPACE.split('-')[0]
        if not self._is_active(path):
            return {"ok": False, "error": "Session is no longer active. Refresh and resume it on the host first."}
        return await deliver(agent, session_id, command_id, text)

    @capability("resume")
    async def _resume(self, session_id: str, path: str | None = None,
                      name: str | None = None, cwd: str | None = None,
                      remote_control: bool = True,
                      machine: str | None = None) -> dict:
        """Relaunch a stored Claude Code session on this machine.

        For picking a conversation back up when it was closed on the PC and you
        are somewhere else: the session restarts here with Remote Control on, so
        it reappears in claude.ai and you carry on from your phone or laptop.
        Nothing is sent to the model — this only puts the session back online.

        Runs in the session's own recorded ``cwd`` (override with ``cwd``) under
        a pty, because Remote Control starts an *interactive* session. ``name``
        labels it in the Remote Control list, defaulting to the conversation's
        own title. Returns the proc ``handle``, so the caller can watch it come
        up and stop it with ``proc.signal``/``proc.close``.
        """
        async with self._resume_lock:
            if self._worker is None or not (self._worker.registry.has("proc.start")
                                            or self._worker.registry.has("work.stream.open")):
                return {"ok": False, "error": "proc.* capability unavailable on this "
                                              "worker; update it to resume sessions"}
            root = self._expand(path)
            sp = self._resolve_session(root, session_id)
            if sp is None:
                return {"ok": False, "error": "session not found",
                        "session_id": session_id, "root": str(root)}
            meta = self._session_meta(sp)
            full_id = meta["session_id"]
            if self._is_active(sp):
                return {"ok": False, "error": "session is already active on this host", "session_id": full_id}

            # Already live? Relaunching would fork the transcript in place.
            existing = self._resumed_handles.get(full_id)
            if existing:
                live = await self._worker.registry.call("proc.list")
                for s in live.get("sessions", []):
                    if s.get("handle") == existing and s.get("running"):
                        return {"ok": False, "error": "session is already running",
                                "session_id": full_id, "handle": existing,
                                "hint": "stop it with proc.close before resuming again"}
                self._resumed_handles.pop(full_id, None)

            binary = _claude_bin()
            if binary is None:
                return {"ok": False, "error": "claude CLI not found on this machine"}

            workdir = cwd or meta.get("cwd")
            if workdir and not os.path.isdir(workdir):
                # The recorded cwd can be gone (repo moved, worktree removed).
                # Say so rather than letting the spawn fail with a bare ENOENT.
                return {"ok": False, "error": f"session cwd no longer exists: {workdir}",
                        "session_id": full_id,
                        "hint": "pass cwd= to resume it somewhere else"}

            label = name or meta.get("title") or _short_id(full_id)
            opened = await self._resume_in_terminal(full_id, workdir, label, remote_control)
            if opened is not None:
                return dict(opened, title=meta.get("title")) if opened.get("ok") else opened

            argv = [binary, "--resume", full_id]
            if remote_control:
                argv += ["--remote-control", label[:80]]

            started = await self._worker.registry.call(
                "proc.start", argv=argv, cwd=workdir, pty=True,
                label=f"claude: {label}"[:200])
            if not started.get("ok"):
                return {"ok": False, "error": started.get("error", "spawn failed"),
                        "session_id": full_id}
            self._resumed_handles[full_id] = started["handle"]
            return {"ok": True, "session_id": full_id, "short_id": _short_id(full_id),
                    "title": meta.get("title"), "name": label, "cwd": workdir,
                    "remote_control": bool(remote_control),
                    "handle": started["handle"], "pid": started.get("pid"),
                    "note": ("Session is starting with Remote Control enabled — it "
                             "should appear in claude.ai shortly. Read its output "
                             "with proc.read(handle) if it doesn't."
                             if remote_control else
                             "Session is starting; it is local-only (no Remote Control).")}

    @capability("resumed")
    async def _resumed_list(self) -> dict:
        """Sessions this worker relaunched and whether they're still up.
        Entries carry ``terminal`` (a Rook terminal) or ``handle`` (proc.*)."""
        out = []
        if self._worker is not None and self._resumed_terminals and self._worker.registry.has("work.stream.list"):
            terms = await self._worker.registry.call("work.stream.list")
            by_id = {t.get("id"): t for t in terms.get("terminals", [])}
            for sid, tid in list(self._resumed_terminals.items()):
                t = by_id.get(tid)
                if t is None:
                    self._resumed_terminals.pop(sid, None)
                    continue
                out.append({"session_id": sid, "short_id": _short_id(sid), "terminal": tid,
                            "running": t.get("running"), "exit_code": t.get("exit_code"),
                            "label": t.get("title"),
                            "age_secs": round(time.time() - (t.get("started") or time.time()))})
        if self._worker is None or not self._worker.registry.has("proc.list"):
            return {"ok": True, "sessions": out}
        live = await self._worker.registry.call("proc.list")
        by_handle = {s.get("handle"): s for s in live.get("sessions", [])}
        for sid, handle in list(self._resumed_handles.items()):
            s = by_handle.get(handle)
            if s is None:
                self._resumed_handles.pop(sid, None)
                continue
            out.append({"session_id": sid, "short_id": _short_id(sid),
                        "handle": handle, "running": s.get("running"),
                        "exit_code": s.get("exit_code"),
                        "label": s.get("label"),
                        "age_secs": s.get("age_secs")})
        return {"ok": True, "sessions": out}

    @capability("pull")
    def _pull(self, machine: str | None = None, path: str | None = None,
              limit: int = 50, offset: int = 0) -> dict:
        """List session metadata under ``path`` (default ``~/.claude/projects``).

        Returns the most-recently-modified ``limit`` sessions first.
        """
        root = self._expand(path)
        if not root.exists():
            return {"ok": False, "error": f"{self.NAMESPACE} directory not found",
                    "path": str(root)}
        files = list(self._iter_session_files(root))
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
        total = len(files)
        files = files[max(int(offset), 0):max(int(offset), 0) + max(int(limit), 0)]
        processes = active_sessions(self.NAMESPACE.split("-")[0])
        sessions = []
        for p in files:
            meta, activity = self._scan(p)
            sessions.append(dict(meta, activity=_activity_now(activity, meta.get("last_modified")),
                                 active=self._is_active(p, processes)))
        for entry in sessions:
            entry['messageable'] = bool(entry['active'] and messageable(self.NAMESPACE.split('-')[0], entry['session_id']))
        return {"ok": True, "root": str(root), "sessions": sessions,
                "count": len(sessions), "total": total}

    @capability("read")
    def _read(self, session_id: str, path: str | None = None,
              machine: str | None = None, max_messages: int = 1000, offset: int = 0) -> dict:
        """Read a session transcript. Accepts full UUID or short prefix."""
        root = self._expand(path)
        sp = self._resolve_session(root, session_id)
        if sp is None:
            return {"ok": False, "error": "session not found",
                    "session_id": session_id, "root": str(root)}
        transcript: list[dict] = []
        truncated = False
        skipped = 0
        for rec in self._read_lines(sp):
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            if skipped < max(0, int(offset)):
                skipped += 1
                continue
            if len(transcript) >= max(1, int(max_messages)):
                truncated = True
                break
            transcript.append({
                "uuid": rec.get("uuid"),
                "parent_uuid": rec.get("parentUuid"),
                "role": _record_role(rec),
                "timestamp": rec.get("timestamp"),
                "content": _message_text(rec),
            })
        out = {
            "ok": True,
            "session_id": self._session_id(sp),
            "path": str(sp),
            "messages": transcript,
            "count": len(transcript),
            "activity": self._activity(sp),
        }
        if truncated:
            out["truncated"] = True
            out["next_offset"] = max(0, int(offset)) + len(transcript)
        return out

    @capability("read_page")
    def _read_page(self, session_id: str, path: str | None = None,
                   offset: int = 0, content_offset: int = 0, max_chars: int = 6000,
                   snapshot: str | None = None) -> dict:
        """Read a bounded transcript page, including partial large messages.

        Continue with next_offset and next_content_offset. Message fragments
        carry their original index and character offset for lossless assembly.
        """
        if snapshot is not None:
            return self._snapshot_page(session_id, path, offset, content_offset, max_chars, snapshot)
        root = self._expand(path)
        sp = self._resolve_session(root, session_id)
        if sp is None:
            return {"ok": False, "error": "session not found"}
        offset, content_offset = max(0, int(offset)), max(0, int(content_offset))
        budget = max(1, min(int(max_chars), 6000))
        activity_meta = {"active": self._is_active(sp)} if offset == 0 and content_offset == 0 else {}
        messages = []
        index = 0
        for rec in self._read_lines(sp):
            if not isinstance(rec, dict) or rec.get('type') not in ('user', 'assistant'):
                continue
            if index < offset:
                index += 1
                continue
            if budget <= 0 or len(messages) >= 20:
                return dict(ok=True, **activity_meta, messages=messages, truncated=True,
                            next_offset=index, next_content_offset=0)
            text = _message_text(rec)
            start = content_offset if index == offset else 0
            chunk = text[start:start + budget]
            messages.append(dict(index=index, content_offset=start,
                                 role=_record_role(rec), content=chunk))
            budget -= len(chunk)
            if start + len(chunk) < len(text):
                return dict(ok=True, **activity_meta, messages=messages, truncated=True,
                            next_offset=index, next_content_offset=start + len(chunk))
            index += 1
        return dict(ok=True, **activity_meta, messages=messages, truncated=False, activity=self._activity(sp))

    @capability("transcript", risk="read")
    def _transcript(self, session_id: str, offset: int = 0, max_chars: int = 6000,
                    path: str | None = None) -> dict:
        """A transcript page in the stable ``rook.transcript/1`` export format.

        ``messages`` are whole records ``{index, role, ts, text}`` (role is
        user, assistant or tool); a single record longer than 50,000
        characters is clipped and carries ``clipped`` (characters dropped).
        Continue with ``next_offset`` until it is null. See docs/web/worklog.md.
        """
        root = self._expand(path)
        sp = self._resolve_session(root, session_id)
        if sp is None:
            return {"ok": False, "error": "session not found"}
        offset = max(0, int(offset))
        budget = max(500, min(int(max_chars), 50000))
        agent = self.NAMESPACE.split("-")[0]
        messages: list[dict] = []
        index = 0
        more = False
        for rec in self._read_lines(sp):
            if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant", "tool"):
                continue
            if index < offset:
                index += 1
                continue
            text = _message_text(rec)
            if messages and len(text) > budget:
                more = True
                break
            entry = {"index": index, "role": "tool" if rec.get("type") == "tool" else _record_role(rec),
                     "ts": rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None,
                     "text": text[:50000]}
            if len(text) > 50000:
                entry["clipped"] = len(text) - 50000
            messages.append(entry)
            budget -= len(entry["text"])
            index += 1
            if budget <= 0:
                more = True
                break
        out = {"ok": True, "format": "rook.transcript/1", "messages": messages,
               "next_offset": index if more else None}
        if offset == 0:
            meta = self._session_meta(sp)
            out["session"] = {"agent": agent, "session_id": meta.get("session_id"),
                              "title": meta.get("title"), "cwd": meta.get("cwd"),
                              "git_branch": meta.get("git_branch"),
                              "started": meta.get("first_timestamp"),
                              "updated": meta.get("last_timestamp"),
                              "message_count": meta.get("message_count")}
        return out

    @capability("read_snapshot")
    def _read_snapshot(self, session_id: str, path: str | None = None, offset: int = 0,
                       content_offset: int = 0, snapshot: str = "") -> dict:
        """Read a bounded page of a stable, worker-owned conversation snapshot."""
        return self._snapshot_page(session_id, path, offset, content_offset, 6000, snapshot)

    @capability("follow")
    def _follow(self, session_id: str, offset: int = 0, version: str = "", tail: int = 0) -> dict:
        """Check the selected log and return only its changed tail, in stable pages.

        ``tail`` (1-200) starts at the last ``tail`` messages instead of
        ``offset``, in one page of up to 64,000 characters with each message
        clipped to 4,000 (``clipped`` = characters left out); the reply's
        ``tail_from`` is the first index it holds. Follow on from there with
        ``offset`` and ``version`` as usual."""
        with self._history_lock:
            sp = self._resolve_session(self._default_root(), session_id)
            if sp is None:
                return {'ok': False, 'error': 'session not found'}
            stat = sp.stat()
            current = f'{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}'
            if version == current:
                return {'ok': True, 'unchanged': True, 'version': current}
            first = self._snapshot_page(session_id, None, 0, 0, 6000, '')
            if not first.get('ok'):
                return first
            total = first['total_messages']
            tail = max(0, min(int(tail or 0), TAIL_MAX))
            if tail:
                start = max(0, total - tail)
                page = self._snapshot_page(session_id, None, start, 0, TAIL_CHARS, first['snapshot'],
                                           clip=TAIL_CLIP, max_messages=tail)
                return dict(page, version=current, replace_from=start, tail_from=start)
            # If the source was truncated or rewritten, replace the whole view.
            offset = max(0, int(offset))
            previous = version.split(':')
            replaced = (len(previous) == 3 and (previous[0] != str(stat.st_ino) or
                        stat.st_size <= int(previous[1])))
            if replaced or offset > total:
                offset = 0
            page = self._snapshot_page(session_id, None, offset, 0, 6000, first['snapshot'])
            return dict(page, version=current, replace_from=offset)

    def _snapshot_page(self, session_id, path, offset, content_offset, max_chars, token,
                       clip=0, max_messages=20):
        """Freeze the conversation once on its owner; page it without rereading logs.

        ``clip`` > 0 cuts each message to that many characters (the page
        moves on to the next message; ``clipped`` says how many were left
        out) and allows pages of up to 64,000 characters."""
        with self._history_lock:
            now = time.monotonic()
            self._history_snapshots = {k: v for k, v in self._history_snapshots.items() if now - v['used'] < 180}
            if not token:
                if offset or content_offset:
                    return {'ok': False, 'error': 'A new snapshot must start at the beginning.'}
                sp = self._resolve_session(self._expand(path), session_id)
                if sp is None:
                    return {'ok': False, 'error': 'session not found'}
                stat = sp.stat()
                version = f'{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}'
                messages = self._rows(sp)
                while len(self._history_snapshots) >= 4:
                    oldest = min(self._history_snapshots, key=lambda k: self._history_snapshots[k]['used'])
                    del self._history_snapshots[oldest]
                token = uuid.uuid4().hex
                self._history_snapshots[token] = dict(session_id=session_id, path=path, used=now,
                    messages=messages, activity=self._activity(sp), active=self._is_active(sp), version=version)
            saved = self._history_snapshots.get(token)
            if not saved or saved['session_id'] != session_id or saved['path'] != path:
                return {'ok': False, 'error': 'Conversation snapshot expired. Refresh from host.'}
            saved['used'] = now
            rows = saved['messages']
            if offset < 0 or offset > len(rows) or content_offset < 0:
                return {'ok': False, 'error': 'Invalid history cursor.'}
            clip = max(0, int(clip or 0))
            budget = max(1, min(int(max_chars), TAIL_CHARS if clip else 6000))
            limit = max(1, min(int(max_messages or 20), TAIL_MAX))
            messages = []
            index, start = offset, content_offset
            while index < len(rows) and budget > 0 and len(messages) < limit:
                row = rows[index]
                if start > len(row['content']):
                    return {'ok': False, 'error': 'Invalid content cursor.'}
                if clip and messages and min(len(row['content']) - start, clip) > budget:
                    break       # whole messages only: this one starts the next page
                chunk = row['content'][start:start + (min(budget, clip) if clip else budget)]
                message = dict(index=index, content_offset=start, role=row['role'], content=chunk)
                for extra in ('kind', 'error'):
                    if row.get(extra):
                        message[extra] = row[extra]
                messages.append(message)
                budget -= len(chunk)
                start += len(chunk)
                if start < len(row['content']):
                    if not clip:
                        break
                    message['clipped'] = len(row['content']) - start
                index, start = index + 1, 0
            return dict(ok=True, snapshot=token, messages=messages, truncated=index < len(rows),
                        next_offset=index, next_content_offset=start, activity=saved['activity'],
                        active=saved['active'], total_messages=len(rows), version=saved['version'])

    def _activity(self, path):
        """Use explicit completion markers; stale/incomplete logs stay pending."""
        _meta, activity = self._scan(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        return _activity_now(activity, mtime)

    @capability("search")
    def _search(self, query: str, path: str | None = None,
                machine: str | None = None, limit: int = 20,
                ignore_case: bool = True) -> dict:
        """Regex-search across all session messages. Returns per-session hits
        with ``snippet`` (first match context) and ``match_count``.
        """
        try:
            rx = re.compile(query, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            return {"ok": False, "error": f"invalid regex: {e}"}
        root = self._expand(path)
        if not root.exists():
            return {"ok": False, "error": f"{self.NAMESPACE} directory not found",
                    "path": str(root)}
        hits: list[dict] = []
        for fp in self._iter_session_files(root):
            match_count = 0
            snippet: str | None = None
            title: str | None = None
            ai_title: str | None = None
            for rec in self._read_lines(fp):
                rtype = rec.get("type")
                if rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
                    ai_title = _title_candidate(rec["aiTitle"])
                    continue
                if rtype not in ("user", "assistant"):
                    continue
                text = _message_text(rec)
                if not text:
                    continue
                if title is None and rtype == "user":
                    title = _title_candidate(text)
                for m in rx.finditer(text):
                    match_count += 1
                    if snippet is None:
                        s = max(m.start() - 80, 0)
                        e = min(m.end() + 80, len(text))
                        snippet = text[s:e].replace("\n", " ")
            if match_count:
                hits.append({
                    "session_id": self._session_id(fp),
                    "short_id": _short_id(self._session_id(fp)),
                    "project": fp.parent.name,
                    "title": ai_title or title or "(empty)",
                    "snippet": snippet,
                    "match_count": match_count,
                })
        hits.sort(key=lambda h: h["match_count"], reverse=True)
        hits = hits[: max(int(limit), 0)]
        return {"ok": True, "query": query, "hits": hits, "count": len(hits)}

    @capability("analyze")
    def _analyze(self, pattern: str = "tool_usage", path: str | None = None,
                 machine: str | None = None, limit: int = 20) -> dict:
        """Extract a knowledge pattern across all sessions.

        Supported patterns: ``tool_usage``, ``architectural_decisions``,
        ``error_patterns``, ``code_patterns``.
        """
        root = self._expand(path)
        if not root.exists():
            return {"ok": False, "error": f"{self.NAMESPACE} directory not found",
                    "path": str(root)}
        pat = pattern.lower().strip()
        if pat == "tool_usage":
            return self._analyze_tools(root, int(limit))
        if pat == "architectural_decisions":
            return self._analyze_keywords(
                root, pat,
                terms=("architecture", "design decision", "trade-off", "tradeoff",
                       "we should", "refactor", "rewrite", "schema",
                       "abstraction", "interface", "contract"),
                limit=int(limit))
        if pat == "error_patterns":
            return self._analyze_keywords(
                root, pat,
                terms=("traceback", "exception", "error:", "failed",
                       "ImportError", "TypeError", "ValueError",
                       "AttributeError", "panic", "stack trace"),
                limit=int(limit))
        if pat == "code_patterns":
            return self._analyze_code(root, int(limit))
        return {"ok": False, "error": f"unknown pattern: {pattern}"}

    def _analyze_tools(self, root: Path, limit: int) -> dict:
        names: Counter[str] = Counter()
        sessions_with_tools = 0
        for fp in self._iter_session_files(root):
            local: Counter[str] = Counter()
            for rec in self._read_lines(fp):
                msg = rec.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        local[str(block.get("name") or "(unknown)")] += 1
            if local:
                sessions_with_tools += 1
                names.update(local)
        return {
            "ok": True,
            "pattern": "tool_usage",
            "top_tools": names.most_common(limit),
            "session_count": sessions_with_tools,
            "total_calls": sum(names.values()),
        }

    def _analyze_keywords(self, root: Path, pattern: str,
                          terms: tuple[str, ...], limit: int) -> dict:
        rx = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
        hits: list[dict] = []
        for fp in self._iter_session_files(root):
            count = 0
            sample: str | None = None
            for rec in self._read_lines(fp):
                if rec.get("type") not in ("user", "assistant"):
                    continue
                text = _message_text(rec)
                if not text:
                    continue
                m = rx.search(text)
                if not m:
                    continue
                count += len(rx.findall(text))
                if sample is None:
                    s = max(m.start() - 100, 0)
                    e = min(m.end() + 100, len(text))
                    sample = text[s:e].replace("\n", " ")
            if count:
                hits.append({
                    "session_id": self._session_id(fp),
                    "short_id": _short_id(self._session_id(fp)),
                    "project": fp.parent.name,
                    "match_count": count,
                    "sample": sample,
                })
        hits.sort(key=lambda h: h["match_count"], reverse=True)
        return {
            "ok": True,
            "pattern": pattern,
            "hits": hits[:limit],
            "session_count": len(hits),
        }

    def _analyze_code(self, root: Path, limit: int) -> dict:
        fence = re.compile(r"```([A-Za-z0-9_+.\-]*)\s*\n(.*?)```", re.DOTALL)
        langs: Counter[str] = Counter()
        blocks = 0
        for fp in self._iter_session_files(root):
            for rec in self._read_lines(fp):
                if rec.get("type") not in ("user", "assistant"):
                    continue
                text = _message_text(rec)
                if not text or "```" not in text:
                    continue
                for m in fence.finditer(text):
                    lang = (m.group(1) or "plain").lower()
                    langs[lang] += 1
                    blocks += 1
        return {
            "ok": True,
            "pattern": "code_patterns",
            "top_languages": langs.most_common(limit),
            "total_blocks": blocks,
        }

    @capability("export")
    def _export(self, session_id: str, format: str = "markdown",
                path: str | None = None, machine: str | None = None) -> dict:
        """Export a session as ``markdown``, ``json``, or ``html``."""
        root = self._expand(path)
        sp = self._resolve_session(root, session_id)
        if sp is None:
            return {"ok": False, "error": "session not found",
                    "session_id": session_id}
        fmt = format.lower().strip()
        if fmt == "json":
            records = list(self._read_lines(sp))
            return {"ok": True, "format": "json", "session_id": self._session_id(sp),
                    "content": json.dumps(records, indent=2)}
        msgs: list[tuple[str, str, str | None]] = []
        for rec in self._read_lines(sp):
            if rec.get("type") not in ("user", "assistant"):
                continue
            ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
            msgs.append((_record_role(rec), _message_text(rec), ts))
        if fmt == "markdown":
            lines = [f"# Session {self._session_id(sp)}", ""]
            for role, text, ts in msgs:
                head = f"## {role}"
                if ts:
                    head += f"  _(at {ts})_"
                lines += [head, "", text, ""]
            return {"ok": True, "format": "markdown", "session_id": self._session_id(sp),
                    "content": "\n".join(lines)}
        if fmt == "html":
            from html import escape
            parts = [f"<h1>Session {escape(self._session_id(sp))}</h1>"]
            for role, text, ts in msgs:
                meta = f" <small>({escape(ts)})</small>" if ts else ""
                parts.append(f"<h2>{escape(role)}{meta}</h2>")
                parts.append(f"<pre>{escape(text)}</pre>")
            return {"ok": True, "format": "html", "session_id": self._session_id(sp),
                    "content": "\n".join(parts)}
        return {"ok": False, "error": f"unsupported format: {format}"}


PLUGIN = ClaudeHistoryPlugin
