"""A role's memory, kept in its home directory (docs/design/huddles.md 3.3).

Memory scope ``role:<id>`` lives in ``<role home>/memory/``: one Markdown file
per memory (a small header, then the text) and a generated ``MEMORY.md``
index, so a person can read the folder directly and it moves with the home.

Two ways in:

* :class:`MemoryDir`, used in process by the ``agenthost`` worker plugin (the
  turn's memory digest, the ``agenthost.role_memory*`` caps);
* ``python -m rook.worker.rolemem <home> [--read-only]``: a stdio MCP server
  the role's harness starts from the config in its home, with the tools
  ``memory_recall``, ``memory_remember`` and ``memory_forget`` (the last two
  only without ``--read-only``).

Writes pass the memory plugin's write rules (secrets, prompt-injection
phrases and code are refused) when that module can be imported.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

KINDS = ("fact", "procedure", "episode")
MAX_CHARS = 1000
MAX_ITEMS = 500
DIGEST_CHARS = 4000
_ID = re.compile(r"^m_[a-z0-9]{8,16}$")
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset("the and for that this with from are was were has have had you your not but "
                  "what when where which who how why can will would should could about into "
                  "than then them they there their our out its it's".split())
PROTOCOL = "2025-06-18"


class MemoryError_(ValueError):
    """A refused memory write or a bad request."""


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP}


def _screen(text: str, kind: str) -> str:
    """The memory plugin's write rules; returns the text to store or raises."""
    try:
        from ..hub.plugins.memory import rules
    except Exception:  # noqa: BLE001 - a worker without the hub package: basic checks only
        text = " ".join(str(text or "").split())
        if not text:
            raise MemoryError_("nothing to remember") from None
        return text[:MAX_CHARS]
    v = rules.screen(text, kind, max_chars=MAX_CHARS)
    if v.reject:
        raise MemoryError_(v.reject)
    return v.text


class MemoryDir:
    """The ``memory/`` directory of one role home."""

    def __init__(self, home: str | os.PathLike, role: str = "") -> None:
        self.home = Path(home)
        self.dir = self.home / "memory"
        self.role = role or self.home.name
        self._lock = threading.Lock()

    # -- reading -------------------------------------------------------------
    def _parse(self, path: Path) -> dict | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        head, body = {}, raw
        if raw.startswith("---\n"):
            end = raw.find("\n---\n", 4)
            if end > 0:
                for line in raw[4:end].splitlines():
                    k, _, v = line.partition(":")
                    head[k.strip()] = v.strip()
                body = raw[end + 5:]
        mid = head.get("id") or path.stem
        if not _ID.match(mid):
            return None
        return {"id": mid, "kind": head.get("kind") or "fact", "text": body.strip(),
                "created": head.get("created") or "", "by": head.get("by") or ""}

    def items(self) -> list[dict]:
        """Every memory, newest first."""
        if not self.dir.is_dir():
            return []
        out = [m for m in (self._parse(p) for p in self.dir.glob("m_*.md")) if m]
        return sorted(out, key=lambda m: (m["created"], m["id"]), reverse=True)

    def recall(self, query: str = "", limit: int = 10) -> list[dict]:
        """Memories sharing words with ``query`` (best first), else the newest."""
        items = self.items()
        limit = max(1, min(int(limit or 10), 100))
        q = _words(query)
        if not q:
            return items[:limit]
        scored = [(len(q & _words(m["text"])), i, m) for i, m in enumerate(items)]
        return [m for score, _i, m in sorted(scored, key=lambda t: (-t[0], t[1])) if score][:limit]

    def digest(self, prompt: str = "", budget: int = DIGEST_CHARS) -> str:
        """The section appended to a turn's system prompt ('' when empty)."""
        items = self.items()
        if not items:
            return ""
        q = _words(prompt)
        ranked = sorted(enumerate(items), key=lambda t: (-len(q & _words(t[1]["text"])), t[0]))
        lines, used = [], 0
        for _i, m in ranked:
            line = f"- ({m['kind']}, {m['id']}) {m['text']}"
            if used + len(line) > budget:
                continue
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        return (f"Your memory (role:{self.role}; save with memory_remember, drop with "
                "memory_forget):\n" + "\n".join(lines))

    # -- writing -------------------------------------------------------------
    def remember(self, text: str, kind: str = "fact", by: str = "") -> dict:
        kind = str(kind or "fact")
        if kind not in KINDS:
            raise MemoryError_(f"kind must be one of {', '.join(KINDS)}")
        clean = _screen(str(text or ""), kind)
        with self._lock:
            items = self.items()
            same = next((m for m in items if m["text"] == clean), None)
            if same is not None:
                return {"ok": True, "id": same["id"], "unchanged": True}
            if len(items) >= MAX_ITEMS:
                raise MemoryError_(f"this role already keeps {MAX_ITEMS} memories; forget some "
                                   "first")
            self.dir.mkdir(parents=True, exist_ok=True)
            mid = "m_" + secrets.token_hex(5)
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            by = re.sub(r"[\r\n]", " ", str(by or f"role:{self.role}"))[:80]
            body = f"---\nid: {mid}\nkind: {kind}\ncreated: {created}\nby: {by}\n---\n{clean}\n"
            tmp = self.dir / f".{mid}.tmp"
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self.dir / f"{mid}.md")
            self._index()
        return {"ok": True, "id": mid, "kind": kind, "text": clean}

    def forget(self, mid: str) -> dict:
        mid = str(mid or "")
        if not _ID.match(mid):
            raise MemoryError_("id: a memory id such as m_0a1b2c3d4e")
        with self._lock:
            path = self.dir / f"{mid}.md"
            if not path.is_file():
                return {"ok": True, "id": mid, "forgotten": False}
            path.unlink()
            self._index()
        return {"ok": True, "id": mid, "forgotten": True}

    def _index(self) -> None:
        lines = [f"# Memory of role:{self.role}", "",
                 "Generated by Rook; edit the memories through the role, not this file.", ""]
        for m in self.items():
            first = m["text"].splitlines()[0] if m["text"] else ""
            lines.append(f"- [{m['id']}]({m['id']}.md) {m['kind']}: {first[:120]}")
        (self.dir / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# -- the stdio MCP server --------------------------------------------------------------

def tools(read_only: bool) -> list[dict]:
    out = [{"name": "memory_recall",
            "description": "Search your own memory (this role's). Empty query: the newest.",
            "inputSchema": {"type": "object", "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"}}}}]
    if not read_only:
        out += [{"name": "memory_remember",
                 "description": "Keep one durable thing you learned, in a sentence: a fact, a "
                                "procedure that worked, or what happened (episode). Never "
                                "secrets or code.",
                 "inputSchema": {"type": "object", "required": ["text"], "properties": {
                     "text": {"type": "string"},
                     "kind": {"type": "string", "enum": list(KINDS)}}}},
                {"name": "memory_forget",
                 "description": "Drop one of your memories by id (wrong or out of date).",
                 "inputSchema": {"type": "object", "required": ["id"], "properties": {
                     "id": {"type": "string"}}}}]
    return out


def handle(mem: MemoryDir, msg: dict, read_only: bool) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:
        return None

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "rook-role-memory", "version": "1"}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tools(read_only)})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            if name == "memory_recall":
                res: Any = {"memories": mem.recall(str(args.get("query") or ""),
                                                   int(args.get("limit") or 10))}
            elif name == "memory_remember" and not read_only:
                res = mem.remember(str(args.get("text") or ""), str(args.get("kind") or "fact"))
            elif name == "memory_forget" and not read_only:
                res = mem.forget(str(args.get("id") or ""))
            else:
                raise MemoryError_(f"unknown tool {name!r}")
            return ok({"content": [{"type": "text", "text": json.dumps(res)}], "isError": False})
        except (MemoryError_, ValueError, OSError) as e:
            return ok({"content": [{"type": "text", "text": json.dumps({"error": str(e)[:300]})}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(home: str, read_only: bool = False, stdin=None, stdout=None) -> None:
    """Newline-delimited JSON-RPC on stdin/stdout until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    mem = MemoryDir(home)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        batch = msg if isinstance(msg, list) else [msg]
        out = [r for r in (handle(mem, m, read_only) for m in batch if isinstance(m, dict)) if r]
        for r in out:
            stdout.write(json.dumps(r) + "\n")
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    read_only = "--read-only" in args
    rest = [a for a in args if a != "--read-only"]
    if len(rest) != 1:
        print("usage: python -m rook.worker.rolemem <role home> [--read-only]", file=sys.stderr)
        return 2
    serve(rest[0], read_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
