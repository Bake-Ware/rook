"""memory.* — a shared, band-wide memory vault ("go ask sojourn", as a cap).

One host on the band runs this (whichever worker has ``ROOK_MEMORY_VAULT`` set —
intended to be the always-on hub box). Every agent on the band, Claude or
hermes, reaches the *same* memories through it: read-open across the band,
write-owned to your own namespace, with a shared/ space anyone can write.

Two layers live in the vault dir:

  * **Markdown notes** — an Obsidian-compatible tree. Folders are namespaces
    (``claude/``, ``sojourn/``, ``shared/``, ``entities/``). ``[[wikilinks]]``
    are preserved. Point the real Obsidian app at the dir any time to browse
    the graph. This is durable human-facing memory.
  * **Post-its** — atomic, append-only, immutable factlets in sqlite (FTS5),
    the unit of "factbuilding". Each is one dated claim about some entities,
    with a kind (decision/fact/change/question/capstone) and optional
    ``supersedes`` edges. ``memory.search`` returns a temporally-ordered *pile*
    (not a smoothed summary) so contradictions show as adjacent, disagreeing
    lines for the calling agent to reconcile with a human — whose ruling comes
    back as a ``capstone`` that supersedes the conflict.

Design: docs/DESIGN-band-services.md §5.

Caps:
    memory.search(query, ...)            -> {pile:[post-its], notes:[hits], flags}
    memory.get(path)                     -> {ok, path, text}
    memory.put(path, text, ...)          -> {ok, path}   (write-owned namespace)
    memory.note(claim, subjects, kind..) -> {ok, id}     (drop a post-it)
    memory.entities(name?)               -> {ok, entities:[...]}
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from ..plugin import Plugin, capability
from ..context import current_identity

_VAULT_ENV = "ROOK_MEMORY_VAULT"
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_KINDS = ("decision", "fact", "change", "question", "capstone")
# A pile that spans this long with no supersede chain connecting its notes is
# flagged "currency unverified" — the cheap graph heuristic that marks possible
# stale/contradicting facts without any inference (design §5, read path).
_STALE_GAP_SECS = 14 * 86400


def _vault_dir() -> Path | None:
    v = os.environ.get(_VAULT_ENV)
    return Path(os.path.expanduser(v)) if v else None


def _identity_namespace() -> str:
    """Map the caller identity to a writable namespace folder. ``agent:claude``
    -> ``claude``; ``agent:hermes_sojourn`` -> ``sojourn`` (the agent family,
    before the first underscore, so per-host tokens share one namespace);
    unknown -> ``shared``."""
    ident = current_identity() or ""
    name = ident.split(":", 1)[1] if ":" in ident else ident
    name = name.strip() or "shared"
    if name in ("static", "anonymous", ""):
        return "shared"
    # agent family: hermes_sojourn -> hermes, claude_kaiju -> claude
    fam = name.split("_", 1)[0]
    return _safe_seg(fam or name)


def _safe_seg(seg: str) -> str:
    """A single path segment sanitized to a safe folder/file name."""
    seg = str(seg).strip().replace(" ", "-")
    seg = "".join(c for c in seg if c.isalnum() or c in "-_.")
    seg = seg.lstrip(".") or "untitled"      # no hidden/.. segments
    return seg[:80]


class MemoryPlugin(Plugin):
    NAMESPACE = "memory"

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._db = None
        self._vault: Path | None = None

    def available(self) -> bool:
        """Load only where a vault is configured — so this runs on the one hub
        box with ROOK_MEMORY_VAULT set, not on every worker in the fleet."""
        return _vault_dir() is not None

    async def start(self) -> None:
        self._vault = _vault_dir()
        if self._vault is None:
            return
        for sub in ("shared", "entities"):
            (self._vault / sub).mkdir(parents=True, exist_ok=True)
        db_path = self._vault / ".rook-postits.db"
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS postits (
                id         TEXT PRIMARY KEY,
                ts         REAL,
                author     TEXT,
                kind       TEXT,
                claim      TEXT,
                subjects   TEXT,      -- JSON array of entity names
                supersedes TEXT,      -- JSON array of post-it ids
                thread_id  TEXT,
                provenance TEXT
            )
        """)
        # FTS mirror over claim + subjects for search.
        self._db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS postits_fts
            USING fts5(claim, subjects, content='postits', content_rowid='rowid')
        """)
        # Keep FTS in sync via triggers.
        self._db.executescript("""
            CREATE TRIGGER IF NOT EXISTS postits_ai AFTER INSERT ON postits BEGIN
                INSERT INTO postits_fts(rowid, claim, subjects)
                VALUES (new.rowid, new.claim, new.subjects);
            END;
            CREATE TRIGGER IF NOT EXISTS postits_ad AFTER DELETE ON postits BEGIN
                INSERT INTO postits_fts(postits_fts, rowid, claim, subjects)
                VALUES('delete', old.rowid, old.claim, old.subjects);
            END;
        """)
        self._db.commit()

    async def stop(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass

    # -- markdown notes ------------------------------------------------------

    def _resolve(self, path: str, for_write: bool = False) -> tuple[Path | None, str | None]:
        """Resolve a vault-relative note path, sandboxed to the vault. On write,
        force the path into the caller's own namespace (or shared/) unless it
        already targets an allowed namespace."""
        if self._vault is None:
            return None, "memory vault not configured on this worker"
        parts = [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]
        parts = [_safe_seg(p) for p in parts if p != ".."]
        if not parts:
            return None, "empty path"
        if not parts[-1].endswith(".md"):
            parts[-1] = parts[-1] + ".md"
        ns = parts[0]
        if for_write:
            own = _identity_namespace()
            # Writable: your own namespace, shared/, or entities/ (curated state).
            if ns not in (own, "shared", "entities"):
                parts = [own] + parts        # redirect into your namespace
        full = (self._vault / Path(*parts)).resolve()
        try:
            full.relative_to(self._vault.resolve())
        except ValueError:
            return None, "path escapes vault"
        return full, None

    @capability("get")
    def _get(self, path: str) -> dict:
        """Read a markdown note from the vault (read-open across the band).
        ``path`` is vault-relative, e.g. ``entities/calendar`` or
        ``sojourn/bakenetca-creds`` (``.md`` optional)."""
        full, err = self._resolve(path, for_write=False)
        if err:
            return {"ok": False, "error": err}
        if not full.exists():
            return {"ok": False, "error": f"no such note: {path}"}
        try:
            return {"ok": True, "path": str(full.relative_to(self._vault)),
                    "text": full.read_text(encoding="utf-8", errors="replace")}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("put")
    def _put(self, path: str, text: str, append: bool = False) -> dict:
        """Write (or append to) a markdown note. Writes are forced into your own
        namespace, ``shared/``, or ``entities/`` — a path aimed elsewhere is
        redirected under your namespace, never another agent's. Use
        ``entities/<thing>`` for current-state notes (amended in place) and your
        own namespace for working notes. ``[[wikilinks]]`` are preserved."""
        full, err = self._resolve(path, for_write=True)
        if err:
            return {"ok": False, "error": err}
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            if append and full.exists():
                prev = full.read_text(encoding="utf-8", errors="replace")
                text = prev + ("\n" if not prev.endswith("\n") else "") + str(text)
            full.write_text(str(text), encoding="utf-8")
            return {"ok": True, "path": str(full.relative_to(self._vault)),
                    "namespace": _identity_namespace()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # -- post-its ------------------------------------------------------------

    @capability("note")
    def _note(self, claim: str, subjects: list | str | None = None,
              kind: str = "fact", supersedes: list | str | None = None,
              thread_id: str | None = None, provenance: str | None = None) -> dict:
        """Drop an atomic post-it — one dated claim about some entities.

        ``kind`` ∈ {decision, fact, change, question, capstone}. ``subjects``
        are the entities it's about (list or comma string; ``[[wikilinks]]`` in
        the claim are auto-added). ``supersedes`` lists post-it ids this
        overrides — a ``capstone`` that resolves a contradiction supersedes the
        conflicting notes. Post-its are immutable; correct by superseding, never
        editing."""
        if self._db is None:
            return {"ok": False, "error": "memory vault not configured"}
        claim = str(claim or "").strip()
        if not claim:
            return {"ok": False, "error": "empty claim"}
        if kind not in _KINDS:
            return {"ok": False, "error": f"kind must be one of {_KINDS}"}
        subj = self._norm_list(subjects)
        subj += [s for s in _WIKILINK.findall(claim) if s not in subj]
        sup = self._norm_list(supersedes)
        pid = uuid.uuid4().hex[:16]
        row = (pid, round(time.time(), 3), _identity_namespace(), kind, claim,
               json.dumps(subj), json.dumps(sup), thread_id, provenance)
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO postits (id, ts, author, kind, claim, subjects, "
                    "supersedes, thread_id, provenance) VALUES (?,?,?,?,?,?,?,?,?)", row)
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "id": pid, "kind": kind, "author": row[2],
                "subjects": subj, "supersedes": sup}

    @capability("search")
    def _search(self, query: str, limit: int = 20,
                include_notes: bool = True) -> dict:
        """Search the vault and return a *pile* of relevant post-its in temporal
        order (oldest first) plus matching note excerpts.

        The pile is deliberately not summarized — read it and reconcile it
        yourself, surfacing contradictions to the human. Each post-it that has
        been superseded is marked ``superseded_by``. If the pile's post-its
        share subjects across a long time gap with no supersede chain linking
        them, ``flags`` includes ``currency_unverified`` — treat the newest as
        provisional and consider asking the user which is current."""
        if self._db is None:
            return {"ok": False, "error": "memory vault not configured"}
        q = str(query or "").strip()
        pile: list[dict] = []
        try:
            with self._lock:
                if q:
                    fts = q.replace('"', '""')
                    rows = self._db.execute(
                        "SELECT p.id,p.ts,p.author,p.kind,p.claim,p.subjects,"
                        "p.supersedes,p.thread_id,p.provenance FROM postits p "
                        "JOIN postits_fts f ON p.rowid=f.rowid "
                        "WHERE postits_fts MATCH ? ORDER BY p.ts LIMIT ?",
                        (f'"{fts}"' if fts else fts, max(1, min(limit, 200)))).fetchall()
                else:
                    rows = self._db.execute(
                        "SELECT id,ts,author,kind,claim,subjects,supersedes,"
                        "thread_id,provenance FROM postits ORDER BY ts DESC LIMIT ?",
                        (max(1, min(limit, 200)),)).fetchall()
                    rows = list(reversed(rows))
                # Build supersede reverse-map over the whole table (cheap) so we
                # can mark which pile members are overridden.
                superseded_by: dict[str, list[str]] = {}
                for (sid, sup_json) in self._db.execute(
                        "SELECT id, supersedes FROM postits WHERE supersedes != '[]'"):
                    for victim in json.loads(sup_json or "[]"):
                        superseded_by.setdefault(victim, []).append(sid)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        for (pid, ts, author, kind, claim, subj, sup, thread, prov) in rows:
            e = {"id": pid, "ts": ts, "when": _ago(ts), "author": author,
                 "kind": kind, "claim": claim, "subjects": json.loads(subj or "[]")}
            if sup and sup != "[]":
                e["supersedes"] = json.loads(sup)
            if pid in superseded_by:
                e["superseded_by"] = superseded_by[pid]
            pile.append(e)

        flags = self._currency_flags(pile)
        out = {"ok": True, "count": len(pile), "pile": pile, "flags": flags}
        if include_notes:
            out["notes"] = self._grep_notes(q, limit=8) if q else []
        return out

    @capability("entities")
    def _entities(self, name: str | None = None) -> dict:
        """List current-state entity notes (``entities/*.md``), or read one by
        name. Entity notes are the durable 'state of the world'; sessions and
        post-its are the history behind them."""
        if self._vault is None:
            return {"ok": False, "error": "memory vault not configured"}
        edir = self._vault / "entities"
        if name:
            return self._get(f"entities/{_safe_seg(name)}")
        out = []
        if edir.exists():
            for f in sorted(edir.glob("*.md")):
                try:
                    txt = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                out.append({"name": f.stem, "chars": len(txt),
                            "preview": txt.strip().replace("\n", " ")[:160]})
        return {"ok": True, "entities": out}

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _norm_list(v) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        if isinstance(v, (list, tuple)):
            return [str(s).strip() for s in v if str(s).strip()]
        return [str(v).strip()]

    def _currency_flags(self, pile: list[dict]) -> list[str]:
        """Cheap graph heuristic: if pile members share a subject across a big
        time gap and none supersede the others, currency is unverified."""
        flags: list[str] = []
        by_subject: dict[str, list[dict]] = {}
        for e in pile:
            for s in e.get("subjects", []):
                by_subject.setdefault(s.lower(), []).append(e)
        for subj, es in by_subject.items():
            if len(es) < 2:
                continue
            ts = [e["ts"] for e in es]
            if max(ts) - min(ts) < _STALE_GAP_SECS:
                continue
            linked = any(e.get("supersedes") or e.get("superseded_by") for e in es)
            if not linked:
                flags.append("currency_unverified")
                break
        return flags

    def _grep_notes(self, query: str, limit: int = 8) -> list[dict]:
        """Naive full-text scan of markdown notes for query terms (the FTS
        table only covers post-its; notes are grepped directly). Returns short
        excerpts around the first match."""
        if self._vault is None or not query:
            return []
        terms = [t.lower() for t in re.split(r"\s+", query) if len(t) > 2]
        if not terms:
            return []
        out = []
        for f in self._vault.rglob("*.md"):
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            low = txt.lower()
            if not all(t in low for t in terms):
                continue
            idx = min((low.find(t) for t in terms if t in low), default=0)
            start = max(0, idx - 80)
            out.append({"path": str(f.relative_to(self._vault)),
                        "excerpt": txt[start:start + 240].replace("\n", " ").strip()})
            if len(out) >= limit:
                break
        return out


def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


PLUGIN = MemoryPlugin
