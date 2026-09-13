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
It runs the session through ``proc.*`` (pty-backed — Remote Control needs an
interactive session, which needs a tty), so the relaunched session is a normal
worker process: visible in ``proc.list``, signalable, and pumped into a console
room the band can watch.
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
    return shutil.which("claude")


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


def _session_meta(p: Path, reader=_read_lines, sid: str | None = None) -> dict:
    sid = sid or p.stem
    try:
        st = p.stat()
    except OSError as e:
        return {"session_id": sid, "path": str(p), "error": str(e)}
    first_ts: str | None = None
    last_ts: str | None = None
    msg_count = 0
    title: str | None = None
    ai_title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    for rec in reader(p):
        if not isinstance(rec, dict):
            continue
        ts = rec.get("timestamp")
        if isinstance(ts, str):
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        rtype = rec.get("type")
        if rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
            ai_title = _title_candidate(rec["aiTitle"])
        elif rtype in ("user", "assistant"):
            msg_count += 1
            if title is None and rtype == "user":
                title = _title_candidate(_message_text(rec))
        if cwd is None and isinstance(rec.get("cwd"), str):
            cwd = rec["cwd"]
        if git_branch is None and isinstance(rec.get("gitBranch"), str):
            git_branch = rec["gitBranch"]
    return {
        "session_id": sid,
        "short_id": _short_id(sid),
        "path": str(p),
        "title": ai_title or title or "(empty)",
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "last_modified": st.st_mtime,
        "message_count": msg_count,
        "project": p.parent.name,
        "cwd": cwd,
        "git_branch": git_branch,
        "size_bytes": st.st_size,
    }


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
        # session_id -> proc handle, for sessions this plugin relaunched. Used
        # to refuse a second resume of a conversation that is already live —
        # two `claude --resume` processes on one session id would both write
        # the same transcript.
        self._resumed_handles: dict[str, str] = {}

    def bind_worker(self, worker) -> None:
        """Grab a handle to the Worker so resume can drive the proc.* caps
        instead of growing its own copy of process management."""
        self._worker = worker

    def available(self) -> bool:
        # Only where Claude Code history actually lives on this host.
        return self._default_root().is_dir()

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
            if self._worker is None or not self._worker.registry.has("proc.start"):
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

            argv = [binary, "--resume", full_id]
            label = name or meta.get("title") or _short_id(full_id)
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
        """Sessions this worker relaunched and whether they're still up."""
        if self._worker is None or not self._worker.registry.has("proc.list"):
            return {"ok": True, "sessions": []}
        live = await self._worker.registry.call("proc.list")
        by_handle = {s.get("handle"): s for s in live.get("sessions", [])}
        out = []
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
        sessions = [dict(self._session_meta(p), activity=self._activity(p), active=self._is_active(p, processes)) for p in files]
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

    @capability("read_snapshot")
    def _read_snapshot(self, session_id: str, path: str | None = None, offset: int = 0,
                       content_offset: int = 0, snapshot: str = "") -> dict:
        """Read a bounded page of a stable, worker-owned conversation snapshot."""
        return self._snapshot_page(session_id, path, offset, content_offset, 6000, snapshot)

    @capability("follow")
    def _follow(self, session_id: str, offset: int = 0, version: str = "") -> dict:
        """Check the selected log and return only its changed tail, in stable pages."""
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
            # If the source was truncated or rewritten, replace the whole view.
            offset = max(0, int(offset))
            previous = version.split(':')
            replaced = (len(previous) == 3 and (previous[0] != str(stat.st_ino) or
                        stat.st_size <= int(previous[1])))
            if replaced or offset > first['total_messages']:
                offset = 0
            page = self._snapshot_page(session_id, None, offset, 0, 6000, first['snapshot'])
            return dict(page, version=current, replace_from=offset)

    def _snapshot_page(self, session_id, path, offset, content_offset, max_chars, token):
        """Freeze the conversation once on its owner; page it without rereading logs."""
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
                messages = [{'role': _record_role(rec), 'content': _message_text(rec)}
                            for rec in self._read_lines(sp)
                            if isinstance(rec, dict) and rec.get('type') in ('user', 'assistant')]
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
            budget = max(1, min(int(max_chars), 6000))
            messages = []
            index, start = offset, content_offset
            while index < len(rows) and budget > 0 and len(messages) < 20:
                row = rows[index]
                if start > len(row['content']):
                    return {'ok': False, 'error': 'Invalid content cursor.'}
                chunk = row['content'][start:start + budget]
                messages.append(dict(index=index, content_offset=start, role=row['role'], content=chunk))
                budget -= len(chunk)
                start += len(chunk)
                if start < len(row['content']):
                    break
                index, start = index + 1, 0
            return dict(ok=True, snapshot=token, messages=messages, truncated=index < len(rows),
                        next_offset=index, next_content_offset=start, activity=saved['activity'],
                        active=saved['active'], total_messages=len(rows), version=saved['version'])

    def _activity(self, path):
        """Use explicit completion markers; stale/incomplete logs stay pending."""
        activity = 'pending'
        for rec in _read_lines(path):
            if not isinstance(rec, dict):
                continue
            if self.NAMESPACE == 'codex-history':
                payload = rec.get('payload') or {}
                if not isinstance(payload, dict):
                    continue
                if rec.get('type') == 'event_msg':
                    kind = payload.get('type')
                    if kind in ('task_started', 'user_message'):
                        activity = 'working'
                    elif kind in ('task_complete', 'task_completed', 'turn_aborted'):
                        activity = 'ready'
                elif rec.get('type') == 'response_item':
                    if payload.get('phase') == 'final' or (
                        payload.get('type') == 'function_call' and
                        str(payload.get('name', '')).split('.')[-1] in ('request_user_input', 'request_user_input_async')
                    ):
                        activity = 'ready'
                    elif payload.get('type') in ('function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'):
                        activity = 'working'
            else:
                msg = rec.get('message') or {}
                if rec.get('type') == 'user':
                    activity = 'working'
                elif rec.get('type') == 'assistant' and isinstance(msg, dict):
                    if msg.get('stop_reason') in ('end_turn', 'stop_sequence'):
                        activity = 'ready'
                    elif msg.get('stop_reason') == 'tool_use':
                        blocks = msg.get('content')
                        needs_input = isinstance(blocks, list) and any(
                            isinstance(b, dict) and b.get('name') == 'AskUserQuestion' for b in blocks)
                        activity = 'ready' if needs_input else 'working'
        if activity == 'working' and time.time() - path.stat().st_mtime > 120:
            return 'pending'
        return activity

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
