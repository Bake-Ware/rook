"""codex-history.* — local Codex rollout history and PTY resume.

Matches claude-history's operations. Reads JSONL, never auth.json/config.toml.
Response messages are canonical; event messages are a fallback for event-only
logs. Reasoning records are not transcript messages. JSON export is normalized.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from ..plugin import capability
from . import claude_history as history

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _root():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "sessions"


def _expand(path):
    return Path(os.path.expandvars(path)).expanduser() if path else _root()


def _sid(path):
    match = _UUID.search(path.stem)
    if match:
        return match.group().lower()
    for rec in history._read_lines(path):
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "session_meta":
            payload = rec.get("payload") or {}
            value = payload.get("id") or payload.get("session_id")
            if isinstance(value, str) and _UUID.fullmatch(value):
                return value.lower()
    return ""


def _files(root):
    for path in history._iter_session_files(root):
        if _sid(path):
            yield path


def _resolve(root, session_id):
    # Never turn a supplied ID into a path or accept ambiguous prefixes.
    needle = str(session_id).lower()
    if not re.fullmatch(r"[0-9a-f-]{4,36}", needle):
        return None
    matches = [p for p in _files(root) if _sid(p).startswith(needle)]
    exact = [p for p in matches if _sid(p) == needle]
    matches = exact or matches
    return matches[0] if len(matches) == 1 else None


def _records(path):
    # Some clients only save event_msg messages. Avoid duplicate transcripts
    # where both event_msg and response_item versions of a message are saved.
    canonical_roles = set()
    for rec in history._read_lines(path):
        if not isinstance(rec, dict):
            continue
        p = rec.get("payload")
        if rec.get("type") == "response_item" and isinstance(p, dict) and p.get("type") == "message":
            canonical_roles.add(p.get("role"))
    for rec in history._read_lines(path):
        if not isinstance(rec, dict):
            continue
        p = rec.get("payload")
        if not isinstance(p, dict):
            continue
        kind, typ = rec.get("type"), p.get("type")
        ts = rec.get("timestamp")
        if kind == "session_meta":
            git = p.get("git") or {}
            yield {"type": "metadata", "cwd": p.get("cwd"), "timestamp": ts,
                   "gitBranch": git.get("branch") if isinstance(git, dict) else None}
        elif kind == "response_item" and typ == "message" and p.get("role") in ("user", "assistant"):
            if p.get("phase") == "analysis":
                continue
            content = p.get("content", [])
            if isinstance(content, list):
                content = [{"type": "text", "text": b.get("text", "")} for b in content
                           if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text")]
            yield {"type": p["role"], "timestamp": ts, "uuid": p.get("id"),
                   "message": {"role": p["role"], "content": content}}
        elif kind == "event_msg" and typ in ("user_message", "agent_message"):
            role = "user" if typ == "user_message" else "assistant"
            if role not in canonical_roles and p.get("phase") != "analysis":
                yield {"type": role, "timestamp": ts, "message": {"role": role, "content": p.get("message", "")}}
        elif kind == "response_item" and typ in ("function_call", "custom_tool_call"):
            yield {"type": "tool", "timestamp": ts,
                   "message": {"content": [{"type": "tool_use", "name": p.get("name")} ]}}


class CodexHistoryPlugin(history.ClaudeHistoryPlugin):
    NAMESPACE = "codex-history"
    _default_root = staticmethod(_root)
    _expand = staticmethod(_expand)
    _iter_session_files = staticmethod(_files)
    _read_lines = staticmethod(_records)
    _resolve_session = staticmethod(_resolve)
    _session_id = staticmethod(_sid)
    _session_meta = staticmethod(lambda p: history._session_meta(p, reader=_records, sid=_sid(p)))

    def __init__(self):
        super().__init__()
        self._resume_lock = asyncio.Lock()

    @capability("resume")
    async def _resume(self, session_id: str, path: str | None = None,
                      name: str | None = None, cwd: str | None = None,
                      machine: str | None = None) -> dict:
        """Resume Codex interactively in a Rook PTY without sending a prompt.

        Read/write/stop using proc.read, proc.write, proc.signal and proc.close.
        Uses the host's existing Codex login and approval settings unchanged.
        """
        async with self._resume_lock:
            if self._worker is None or not self._worker.registry.has("proc.start"):
                return {"ok": False, "error": "proc.* unavailable on this worker"}
            fp = _resolve(_expand(path), session_id)
            if fp is None:
                return {"ok": False, "error": "session not found or prefix ambiguous"}
            meta = self._session_meta(fp)
            sid = meta["session_id"]
            live = await self._resumed_list()
            for item in live["sessions"]:
                if item["session_id"] == sid and item.get("running"):
                    return {"ok": False, "error": "session is already running", "handle": item["handle"]}
            binary = shutil.which("codex") or shutil.which("codex.cmd")
            if binary is None:
                return {"ok": False, "error": "codex CLI not found on this machine"}
            workdir = cwd or meta.get("cwd")
            if workdir and not Path(workdir).is_dir():
                return {"ok": False, "error": "session cwd no longer exists", "hint": "pass cwd to choose another directory"}
            label = name or meta.get("title") or sid[:8]
            started = await self._worker.registry.call("proc.start", argv=[binary, "resume", sid, "--no-alt-screen"],
                                                       cwd=workdir, pty=True, label=f"codex: {label}"[:200])
            if not started.get("ok"):
                return started
            self._resumed_handles[sid] = started["handle"]
            return {"ok": True, "session_id": sid, "short_id": sid[:8], "title": meta.get("title"),
                    "name": label, "cwd": workdir, "handle": started["handle"], "pid": started.get("pid"),
                    "note": "Codex is starting in a Rook PTY. Use proc.read/proc.write to interact and proc.close to stop."}


PLUGIN = CodexHistoryPlugin
