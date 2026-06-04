"""claude-history.* — read Claude Code session histories on the local machine.

Claude Code stores conversation sessions as JSONL files at
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` (Windows uses
``%USERPROFILE%\\.claude\\projects\\``). Each line is one record — most are
``user``/``assistant`` turns, with sidebands like ``queue-operation``,
``ai-title``, ``last-prompt``.

The ``machine`` argument from the SDD is accepted for caller-side ergonomics
but ignored here: the worker is already running on the target machine, so
cross-host routing is the orchestrator's responsibility.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

from ..plugin import Plugin, capability


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


def _session_meta(p: Path) -> dict:
    sid = p.stem
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
    for rec in _read_lines(p):
        ts = rec.get("timestamp")
        if isinstance(ts, str):
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        rtype = rec.get("type")
        if rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
            ai_title = rec["aiTitle"]
        elif rtype in ("user", "assistant"):
            msg_count += 1
            if title is None and rtype == "user":
                text = _message_text(rec).strip()
                if text:
                    title = text.splitlines()[0][:120]
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

    @capability("pull")
    def _pull(self, machine: str | None = None, path: str | None = None,
              limit: int = 50) -> dict:
        """List session metadata under ``path`` (default ``~/.claude/projects``).

        Returns the most-recently-modified ``limit`` sessions first.
        """
        root = _expand(path)
        if not root.exists():
            return {"ok": False, "error": "claude projects directory not found",
                    "path": str(root)}
        files = list(_iter_session_files(root))
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
        files = files[: max(int(limit), 0)]
        sessions = [_session_meta(p) for p in files]
        return {"ok": True, "root": str(root), "sessions": sessions,
                "count": len(sessions)}

    @capability("read")
    def _read(self, session_id: str, path: str | None = None,
              machine: str | None = None, max_messages: int = 1000) -> dict:
        """Read a session transcript. Accepts full UUID or short prefix."""
        root = _expand(path)
        sp = _resolve_session(root, session_id)
        if sp is None:
            return {"ok": False, "error": "session not found",
                    "session_id": session_id, "root": str(root)}
        transcript: list[dict] = []
        truncated = False
        for rec in _read_lines(sp):
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            transcript.append({
                "uuid": rec.get("uuid"),
                "parent_uuid": rec.get("parentUuid"),
                "role": _record_role(rec),
                "timestamp": rec.get("timestamp"),
                "content": _message_text(rec),
            })
            if len(transcript) >= int(max_messages):
                truncated = True
                break
        out = {
            "ok": True,
            "session_id": sp.stem,
            "path": str(sp),
            "messages": transcript,
            "count": len(transcript),
        }
        if truncated:
            out["truncated"] = True
        return out

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
        root = _expand(path)
        if not root.exists():
            return {"ok": False, "error": "claude projects directory not found",
                    "path": str(root)}
        hits: list[dict] = []
        for fp in _iter_session_files(root):
            match_count = 0
            snippet: str | None = None
            title: str | None = None
            ai_title: str | None = None
            for rec in _read_lines(fp):
                rtype = rec.get("type")
                if rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
                    ai_title = rec["aiTitle"]
                    continue
                if rtype not in ("user", "assistant"):
                    continue
                text = _message_text(rec)
                if not text:
                    continue
                if title is None and rtype == "user":
                    first = text.strip().splitlines()
                    if first:
                        title = first[0][:120]
                for m in rx.finditer(text):
                    match_count += 1
                    if snippet is None:
                        s = max(m.start() - 80, 0)
                        e = min(m.end() + 80, len(text))
                        snippet = text[s:e].replace("\n", " ")
            if match_count:
                hits.append({
                    "session_id": fp.stem,
                    "short_id": _short_id(fp.stem),
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
        root = _expand(path)
        if not root.exists():
            return {"ok": False, "error": "claude projects directory not found",
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
        for fp in _iter_session_files(root):
            local: Counter[str] = Counter()
            for rec in _read_lines(fp):
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
        for fp in _iter_session_files(root):
            count = 0
            sample: str | None = None
            for rec in _read_lines(fp):
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
                    "session_id": fp.stem,
                    "short_id": _short_id(fp.stem),
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
        for fp in _iter_session_files(root):
            for rec in _read_lines(fp):
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
        root = _expand(path)
        sp = _resolve_session(root, session_id)
        if sp is None:
            return {"ok": False, "error": "session not found",
                    "session_id": session_id}
        fmt = format.lower().strip()
        if fmt == "json":
            records = list(_read_lines(sp))
            return {"ok": True, "format": "json", "session_id": sp.stem,
                    "content": json.dumps(records, indent=2)}
        msgs: list[tuple[str, str, str | None]] = []
        for rec in _read_lines(sp):
            if rec.get("type") not in ("user", "assistant"):
                continue
            ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
            msgs.append((_record_role(rec), _message_text(rec), ts))
        if fmt == "markdown":
            lines = [f"# Session {sp.stem}", ""]
            for role, text, ts in msgs:
                head = f"## {role}"
                if ts:
                    head += f"  _(at {ts})_"
                lines += [head, "", text, ""]
            return {"ok": True, "format": "markdown", "session_id": sp.stem,
                    "content": "\n".join(lines)}
        if fmt == "html":
            from html import escape
            parts = [f"<h1>Session {escape(sp.stem)}</h1>"]
            for role, text, ts in msgs:
                meta = f" <small>({escape(ts)})</small>" if ts else ""
                parts.append(f"<h2>{escape(role)}{meta}</h2>")
                parts.append(f"<pre>{escape(text)}</pre>")
            return {"ok": True, "format": "html", "session_id": sp.stem,
                    "content": "\n".join(parts)}
        return {"ok": False, "error": f"unsupported format: {format}"}


PLUGIN = ClaudeHistoryPlugin
