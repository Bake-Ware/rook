"""Per-worker audit log — who called what, when, and how it went.

Every capability dispatch on this worker is appended here (see
:meth:`rook.worker.core.Worker._on_message`), tagged with the caller identity
carried in the request envelope (``msg["identity"]``, stamped by the band
client from the bearer-token name — see ``rook.band_mcp``). This is the
breadcrumb trail behind "who renamed this machine and when": the record lives
on the machine that was *acted upon*, so it survives even if the caller and
the site are long gone.

Storage is a size-capped JSONL ring at ``~/.rook-band-worker/audit.jsonl``.
Append-only, best-effort — a failure to record must never break a capability
call, so every write is wrapped and swallowed. The ``log`` plugin reads this
back over the band via ``log.audit`` / ``log.tail``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("rook.worker.audit")

_AUDIT_DIR = Path(os.path.expanduser("~")) / ".rook-band-worker"
_AUDIT_PATH = _AUDIT_DIR / "audit.jsonl"

# Ring-buffer sizing. We keep the file simple (append-only JSONL) and compact
# it in place once it crosses the high-water mark, dropping the oldest lines
# back down to the low-water mark. Chosen so a chatty worker can't fill a
# small disk while still keeping plenty of forensic history.
_MAX_BYTES = 4 * 1024 * 1024        # compact when the file exceeds this
_KEEP_LINES = 5000                  # lines retained after a compaction

_lock = threading.Lock()


def _summarize_args(args: dict | None, limit: int = 500) -> dict | str | None:
    """A compact, log-safe view of the call args. We keep the keys and a
    truncated repr of each value — enough to answer "what did they ask for"
    without dumping a base64 screenshot or a secret into the audit trail."""
    if not args:
        return None
    try:
        out: dict = {}
        for k, v in args.items():
            s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
            if isinstance(s, str) and len(s) > 120:
                s = s[:120] + f"…(+{len(s) - 120})"
            out[str(k)] = s
        blob = json.dumps(out)
        if len(blob) > limit:
            return blob[:limit] + "…"
        return out
    except Exception:
        return "<unserializable>"


def record(cap: str, identity: str | None, args: dict | None,
           ok: bool, msg_id: str | None = None,
           target: str | None = None, error: str | None = None) -> None:
    """Append one audit entry. Best-effort; never raises."""
    entry = {
        "ts": round(time.time(), 3),
        "identity": identity or "anonymous",
        "cap": cap,
        "ok": ok,
        "args": _summarize_args(args),
    }
    if msg_id:
        entry["id"] = msg_id
    if target:
        entry["target"] = target
    if error:
        entry["error"] = error[:300]
    line = json.dumps(entry, separators=(",", ":"))
    try:
        with _lock:
            _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _maybe_compact()
    except Exception:
        log.debug("audit record failed", exc_info=True)


def _maybe_compact() -> None:
    """Trim the oldest lines when the file grows past the high-water mark.
    Called under ``_lock``."""
    try:
        if not _AUDIT_PATH.exists() or _AUDIT_PATH.stat().st_size <= _MAX_BYTES:
            return
        with open(_AUDIT_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _KEEP_LINES:
            return
        kept = lines[-_KEEP_LINES:]
        tmp = _AUDIT_PATH.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, _AUDIT_PATH)
        log.info("audit compacted: %d -> %d lines", len(lines), len(kept))
    except Exception:
        log.debug("audit compact failed", exc_info=True)


def tail(limit: int = 50, cap_prefix: str | None = None,
         identity: str | None = None, since: float | None = None) -> list[dict]:
    """Return the most recent audit entries (newest last), optionally filtered
    by capability prefix, exact identity, or a ``since`` unix timestamp."""
    try:
        with _lock:
            if not _AUDIT_PATH.exists():
                return []
            with open(_AUDIT_PATH, encoding="utf-8") as f:
                lines = f.readlines()
    except Exception:
        return []
    out: list[dict] = []
    # Walk newest-first so `limit` bounds the scan on large files, then flip.
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if cap_prefix and not str(e.get("cap", "")).startswith(cap_prefix):
            continue
        if identity and e.get("identity") != identity:
            continue
        if since is not None and e.get("ts", 0) < since:
            continue
        out.append(e)
        if len(out) >= max(1, limit):
            break
    out.reverse()
    return out
