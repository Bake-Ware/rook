"""Ingest for agent memory: work-session transcripts and the legacy vault.

**Transcripts.** Work v2 exports historical Claude/Codex sessions as
``rook.transcript/1`` pages (docs/web/worklog.md). A session becomes one
``episode`` memory (a short summary) plus candidate preferences and
corrections pulled from what the user said, which are held as *pending*
proposals for an agent or operator to commit. The default summarizer is
extractive and uses no model, so the result is deterministic; a band cap or
HTTP service can replace it (setting ``summarizer``).

**Legacy vault.** The worker ``memory.*`` plugin keeps post-its in
``<vault>/.rook-postits.db`` and markdown notes. :func:`read_vault` turns
post-its (and ``entities/*.md`` current-state notes) into memory candidates;
:func:`pile_candidates` does the same for a ``memory.search`` reply fetched
over the band, so the bridge works whether or not the vault is on the hub.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

FORMAT = "rook.transcript/1"

# What a user says that is worth remembering. Kept conservative: every match
# becomes a *pending* proposal, never a committed memory on its own.
_PREFERENCE = [
    re.compile(r"(?i)\bI\s+(?:prefer|like|want|need|use)\s+(?!to\s+know\b)(.{8,200})"),
    re.compile(r"(?i)\bmy\s+(?:favorite|preferred|default|usual)\s+\w+\s+is\s+(.{3,160})"),
    re.compile(r"(?i)\bI\s+(?:always|never|usually)\s+(.{8,200})"),
    re.compile(r"(?i)\b(?:please\s+)?(?:always|never)\s+(.{8,200})"),
    re.compile(r"(?i)\bfrom now on,?\s+(.{8,200})"),
    re.compile(r"(?i)\bremember\s+(?:that\s+)?(.{8,200})"),
]
_CORRECTION = [
    re.compile(r"(?i)^(?:no|nope|wrong)[,.!]\s+(.{8,200})"),
    re.compile(r"(?i)\b(?:don't|do not|stop)\s+(.{8,200})"),
    re.compile(r"(?i)\bthat's\s+(?:wrong|not right|not what I)\b[,.]?\s*(.{0,200})"),
    re.compile(r"(?i)\binstead,?\s+(.{8,200})"),
]
_DECISION = [
    re.compile(r"(?i)\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.{8,200})"),
    re.compile(r"(?i)\bthe\s+project\s+(?:uses|needs|requires)\s+(.{8,200})"),
]


def _sentence(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m and len(m.group(1)) >= 20 else text
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + " …"
    return s


def check_pages(pages) -> tuple[dict, list[dict]]:
    """Validate ``rook.transcript/1`` pages; returns (session, messages sorted
    by index, de-duplicated)."""
    if isinstance(pages, dict):
        pages = [pages]
    if not isinstance(pages, list) or not pages:
        raise ValueError("transcript must be a rook.transcript/1 page or a list of pages")
    session: dict = {}
    seen: dict[int, dict] = {}
    for p in pages:
        if not isinstance(p, dict) or p.get("format") != FORMAT:
            raise ValueError(f"unsupported transcript format (want {FORMAT})")
        if isinstance(p.get("session"), dict):
            session = {**p["session"], **session}
        for m in p.get("messages") or []:
            if isinstance(m, dict) and isinstance(m.get("index"), int):
                seen.setdefault(m["index"], m)
    return session, [seen[i] for i in sorted(seen)]


def summarize(session: dict, messages: list[dict], max_chars: int = 900) -> str:
    """Extractive episode summary: what was asked, where, and how it ended."""
    users = [m for m in messages if m.get("role") == "user" and (m.get("text") or "").strip()
             and not str(m.get("text")).lstrip().startswith(("[tool", "<"))]
    assistants = [m for m in messages if m.get("role") == "assistant" and (m.get("text") or "").strip()]
    title = " ".join(str(session.get("title") or "").split())[:120]
    where = Path(str(session.get("cwd") or "")).name
    agent = session.get("agent") or "agent"
    date = str(session.get("started") or (messages[0].get("ts") if messages else "") or "")[:10]
    head = f"Session{' ' + repr(title) if title else ''} ({agent}"
    head += f", {where}" if where else ""
    head += f", {date}" if date else ""
    head += f", {len(messages)} messages)"
    parts = [head + "."]
    if users:
        parts.append("Asked: " + _sentence(users[0]["text"], 240))
        if len(users) > 1:
            parts.append("Later: " + _sentence(users[-1]["text"], 160))
    if assistants:
        parts.append("Ended with: " + _sentence(assistants[-1]["text"], 240))
    out = " ".join(parts)
    return out if len(out) <= max_chars else out[:max_chars - 2].rsplit(" ", 1)[0] + " …"


def extract(messages: list[dict], limit: int = 8) -> list[dict]:
    """Candidate preferences, corrections and decisions from user turns,
    as ``{kind, text, signal, index}``, at most ``limit``, in order."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        text = " ".join(str(m.get("text") or "").split())
        if not text or text.startswith(("[tool", "<")) or len(text) > 4000:
            continue
        for signal, pats, kind in (("correction", _CORRECTION, "preference"),
                                   ("preference", _PREFERENCE, "preference"),
                                   ("decision", _DECISION, "fact")):
            for pat in pats:
                hit = pat.search(text)
                if not hit:
                    continue
                sent = _sentence(text[hit.start():], 220)
                key = sent.lower()
                if key in seen or len(sent) < 12:
                    continue
                seen.add(key)
                out.append({"kind": kind, "text": sent, "signal": signal, "index": m.get("index")})
                break
            else:
                continue
            break
        if len(out) >= limit:
            break
    return out


# -- legacy vault ------------------------------------------------------------

_POSTIT_KIND = {"decision": "fact", "fact": "fact", "change": "fact", "capstone": "fact"}


def _vault_scope(author: str) -> tuple[str, str]:
    author = (author or "shared").strip() or "shared"
    return ("band", "") if author in ("shared", "entities") else ("agent", author)


def postit_candidate(p: dict) -> dict | None:
    kind = _POSTIT_KIND.get(p.get("kind") or "fact")
    claim = " ".join(str(p.get("claim") or "").split())
    if kind is None or not claim:
        return None  # questions are not memories
    subjects = p.get("subjects") or []
    if isinstance(subjects, str):
        try:
            subjects = json.loads(subjects)
        except ValueError:
            subjects = [subjects]
    sup = p.get("supersedes") or []
    if isinstance(sup, str):
        try:
            sup = json.loads(sup)
        except ValueError:
            sup = []
    return {"kind": kind, "text": claim, "scope": _vault_scope(p.get("author")),
            "source": f"vault:postit:{p.get('id')}", "created": p.get("ts"),
            "tags": ["vault", p.get("kind") or "fact"] + [str(s)[:40] for s in subjects][:6],
            "confidence": 0.95 if p.get("kind") == "capstone" else None,
            "legacy_supersedes": [f"vault:postit:{x}" for x in sup],
            "session": p.get("thread_id"), "author": f"vault:{p.get('author') or 'shared'}"}


def read_vault(path: str | Path, limit: int = 1000) -> list[dict]:
    """Candidates from a legacy vault directory (read-only)."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {path}")
    out: list[dict] = []
    db_path = root / ".rook-postits.db"
    if db_path.exists():
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute("SELECT * FROM postits ORDER BY ts LIMIT ?", (limit,)).fetchall()
        finally:
            db.close()
        for r in rows:
            c = postit_candidate(dict(r))
            if c:
                out.append(c)
    edir = root / "entities"
    if edir.is_dir():
        for f in sorted(edir.glob("*.md"))[:200]:
            if f.is_symlink() or f.stat().st_size > 20000:
                continue
            body = " ".join(f.read_text(encoding="utf-8", errors="replace").split())
            if body:
                out.append({"kind": "fact", "text": f"{f.stem}: {body}", "scope": ("band", ""),
                            "source": f"vault:entity:{f.stem}", "created": f.stat().st_mtime,
                            "tags": ["vault", "entity", f.stem[:40]], "confidence": None,
                            "legacy_supersedes": [], "session": None, "author": "vault:entities"})
    return out[:limit]


def pile_candidates(reply) -> list[dict]:
    """Candidates from a worker ``memory.search`` reply (``{pile: [...]}``)."""
    if isinstance(reply, dict) and "result" in reply and "pile" not in reply:
        reply = reply["result"]
    pile = reply.get("pile") if isinstance(reply, dict) else None
    if not isinstance(pile, list):
        raise ValueError("expected a memory.search reply with a pile")
    return [c for c in (postit_candidate(p) for p in pile if isinstance(p, dict)) if c]
