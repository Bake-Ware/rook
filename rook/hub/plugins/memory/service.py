"""Agent memory service: propose/commit, recall, digest, maintenance, ingest.

Transport-free: the plugin (``__init__.py``) turns caps into calls here and
supplies the caller, the settings and the embedder. See
docs/design/memory.md for the rules this implements.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import ingest as ingest_mod
from . import rules
from .embedder import Embedder, cosine
from .store import MemoryStore

log = logging.getLogger("rook.hub.plugins.memory")

DAY = 86400.0
SCOPE_ID = re.compile(r"^[A-Za-z0-9._@:-]{1,80}$")
#: Lexical fallback thresholds (token Jaccard) when no embedding is available.
LEX_DUPLICATE = 0.85
LEX_RELATED = 0.5
DIGEST_SECTIONS = (("profile", "Profile"), ("preference", "Preferences"),
                   ("procedure", "Procedures"), ("fact", "Facts"))


@dataclass
class Caller:
    """Who is writing or reading, for scoping and provenance."""
    identity: str = "system:rook-hub"
    actor: str | None = None
    family: str | None = None          # agent family (claude, codex, hermes, ...)
    session: str | None = None
    task: str | None = None
    kind: str = "system"


@dataclass
class Config:
    default_user: str = "operator"
    default_band: str = "default"
    commit_threshold: float = 0.6
    dedupe_similarity: float = 0.92
    supersede_similarity: float = 0.8
    secrets: str = "reject"
    max_entry_chars: int = 600
    episode_chars: int = 900
    budgets: dict = field(default_factory=lambda: dict(DEFAULT_BUDGETS))
    digest_chars: int = 1200
    half_life_days: dict = field(default_factory=lambda: dict(DEFAULT_HALF_LIFE))
    archive_below: float = 0.15
    pending_ttl_days: int = 14
    episode_keep: int = 60
    ingest_autocommit: bool = False


DEFAULT_BUDGETS = {"profile": 1500, "preference": 2500, "procedure": 4000, "fact": 6000,
                   "episode": 20000}
DEFAULT_HALF_LIFE = {"episode": 30, "fact": 180}


def family_of(label: str | None) -> str | None:
    """Agent family from a token label or identity: ``agent:claude_gpubox``
    -> ``claude``; ``hermes-assistant`` -> ``hermes``."""
    if not label:
        return None
    name = str(label).split(":", 1)[-1]
    fam = re.split(r"[_\-.]", name, 1)[0].lower()
    fam = re.sub(r"[^a-z0-9]", "", fam)
    return fam or None


class MemoryService:
    def __init__(self, store: MemoryStore, config: Callable[[], Config],
                 embedder: Callable[[], Embedder],
                 vault_values: Callable[[], list[str]] = lambda: [],
                 summarizer: Callable[[dict, list], Awaitable[str | None]] | None = None) -> None:
        self.store = store
        self._config = config
        self._embedder = embedder
        self._vault_values = vault_values
        self._summarizer = summarizer
        self._lock = asyncio.Lock()

    @property
    def cfg(self) -> Config:
        return self._config()

    @property
    def embedder(self) -> Embedder:
        return self._embedder()

    # -- scopes ----------------------------------------------------------
    def scope(self, value, caller: Caller, kind: str | None = None) -> tuple[str, str]:
        """One scope for a write. ``None`` = the kind's default scope."""
        if value in (None, ""):
            value = rules.DEFAULT_SCOPE.get(kind or "fact", "band")
        scopes = self.scopes(value, caller, write=True)
        if len(scopes) != 1:
            raise ValueError("a write takes exactly one scope (user, band, agent or kind:id)")
        return scopes[0]

    def scopes(self, value, caller: Caller, write: bool = False) -> list[tuple[str, str]]:
        """Parse ``user``, ``band:ops``, ``agent`` or a list/comma string of
        them. ``None`` (reads) = the caller's user, band and agent family."""
        cfg = self.cfg
        if value in (None, "", []):
            out = [("user", cfg.default_user), ("band", cfg.default_band)]
            if caller.family:
                out.append(("agent", caller.family))
            return out
        items = value if isinstance(value, (list, tuple)) else str(value).split(",")
        out = []
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                sk, sid = str(item[0]), str(item[1])
            else:
                sk, _, sid = str(item).strip().partition(":")
            sk = sk.strip().lower()
            if sk not in rules.SCOPE_KINDS:
                raise ValueError(f"scope must be user, band or agent (optionally :id), not {item!r}")
            sid = sid.strip() or {"user": cfg.default_user, "band": cfg.default_band,
                                  "agent": caller.family or ""}[sk]
            if not sid:
                raise ValueError("agent scope needs an agent family (agent:<family>)")
            if not SCOPE_ID.match(sid):
                raise ValueError(f"bad scope id {sid!r}")
            if (sk, sid) not in out:
                out.append((sk, sid))
        return out

    # -- strength / decay -------------------------------------------------
    def strength(self, row: dict, now: float | None = None) -> float:
        now = now or time.time()
        hl = float(self.cfg.half_life_days.get(row["kind"]) or 0)
        conf = float(row["confidence"])
        if hl <= 0 or conf >= 1.0:
            return conf
        since = max(row.get("last_used") or 0, row.get("updated") or 0, row.get("created") or 0)
        age = max(0.0, (now - since) / DAY)
        return conf * math.pow(0.5, age / hl)

    # -- similarity -------------------------------------------------------
    async def _vector(self, text: str) -> tuple[str, list[float]] | None:
        got = await self.embedder.embed([text])
        if not got:
            return None
        model, vecs = got
        return model, vecs[0]

    def _similar(self, text: str, vec, scope, kind: str) -> list[tuple[float, str, dict]]:
        """Active memories of the same scope and kind, most similar first, as
        ``(similarity, method, row)``."""
        rows = self.store.select([scope], [kind], ("active",), limit=2000)
        if not rows:
            return []
        out = []
        vecs = self.store.vectors([r["id"] for r in rows], vec[0]) if vec else {}
        for r in rows:
            if r["id"] in vecs:
                out.append((cosine(vec[1], vecs[r["id"]]), "cosine", r))
            else:
                out.append((rules.jaccard(text, r["text"]), "lexical", r))
        out.sort(key=lambda x: -x[0])
        return out

    def _is_duplicate(self, sim: float, method: str) -> bool:
        return sim >= (self.cfg.dedupe_similarity if method == "cosine" else LEX_DUPLICATE)

    def _is_related(self, sim: float, method: str, correction: bool) -> bool:
        if method == "cosine":
            return sim >= self.cfg.supersede_similarity
        return correction and sim >= LEX_RELATED

    # -- writes -----------------------------------------------------------
    async def propose(self, text: str, kind: str, caller: Caller, *, scope=None,
                      confidence: float | None = None, confirmed: bool = False,
                      supersedes: list | None = None, tags: list | None = None,
                      origin: str = "agent", source: str | None = None,
                      journal: str | None = None, session: str | None = None,
                      secrets: str | None = None, force_pending: bool = False,
                      created: float | None = None, author: str | None = None) -> dict:
        cfg = self.cfg
        kind = (kind or "").strip().lower()
        verdict = rules.screen(text, kind, origin=origin, confidence=confidence,
                               confirmed=confirmed, secrets=secrets or cfg.secrets,
                               vault_values=self._vault_values(),
                               max_chars=cfg.episode_chars if kind == "episode" else cfg.max_entry_chars)
        if verdict.reject:
            return {"verdict": "rejected", **verdict.to_dict()}
        sc = self.scope(scope, caller, kind)
        async with self._lock:
            return await self._propose(verdict, kind, sc, caller, supersedes=supersedes, tags=tags,
                                       origin=origin, source=source, journal=journal,
                                       session=session, force_pending=force_pending,
                                       created=created, author=author)

    async def _propose(self, verdict, kind, sc, caller, *, supersedes, tags, origin, source,
                       journal, session, force_pending, created, author) -> dict:
        cfg = self.cfg
        text, conf = verdict.text, verdict.confidence
        base = {"scope": f"{sc[0]}:{sc[1]}", "kind": kind, **verdict.to_dict()}
        h = rules.content_hash(text)
        # Rows the caller explicitly replaces are never "duplicates" of it
        # (e.g. an updated summary of the same session).
        explicit = {str(s) for s in (supersedes or []) if isinstance(s, str)}
        same = self.store.by_hash(sc, kind, h)
        if same is not None and same["id"] in explicit:
            same = None
        if same is not None and same["state"] == "active":
            self._reinforce(same, conf, caller)
            return {**base, "verdict": "duplicate", "id": same["id"]}
        vec = await self._vector(text)
        similar = [] if same is not None else [
            x for x in self._similar(text, vec, sc, kind) if x[2]["id"] not in explicit]
        correction = "correction" in verdict.signals
        if similar and self._is_duplicate(similar[0][0], similar[0][1]):
            sim, method, row = similar[0]
            self._reinforce(row, conf, caller)
            return {**base, "verdict": "duplicate", "id": row["id"], "similarity": round(sim, 3),
                    "method": method}
        planned = [str(s) for s in (supersedes or []) if isinstance(s, str)][:20]
        related = [(s, m, r) for s, m, r in similar if self._is_related(s, m, correction)][:3]
        for _s, _m, r in related:
            if r["id"] not in planned:
                planned.append(r["id"])
        pending = force_pending or conf < cfg.commit_threshold
        reason = None
        if pending:
            why = [f"confidence {conf:.2f} below {cfg.commit_threshold:.2f}"] if conf < cfg.commit_threshold \
                else ["held for review"]
            if verdict.warnings:
                why.append("warnings: " + ", ".join(verdict.warnings))
            reason = "; ".join(why)
        if same is not None:  # an identical pending proposal: confirm it
            if not pending:
                return {**base, **self._commit_row(same, caller, conf), "verdict": "committed"}
            self._reinforce(same, conf, caller, commit=False)
            return {**base, "verdict": "pending", "id": same["id"], "reason": same.get("reason")}
        rec = {"scope_kind": sc[0], "scope_id": sc[1], "kind": kind, "text": text, "hash": h,
               "state": "pending" if pending else "active", "confidence": round(conf, 4),
               "author": author or caller.identity, "actor": caller.actor,
               "session": session or caller.session, "journal": journal, "task": caller.task,
               "source": source or origin, "supersedes": planned,
               "tags": [str(t)[:40] for t in (tags or [])][:12], "reason": reason,
               "warnings": verdict.warnings}
        if created:
            rec["created"] = float(created)  # when it was observed; updated stays "now"
        row = self.store.insert(rec, caller.identity)
        if vec:
            self.store.put_vector(row["id"], vec[0], vec[1])
        out = {**base, "id": row["id"]}
        if pending:
            out.update(verdict="pending", reason=reason)
            if related:
                out["would_supersede"] = [{"id": r["id"], "text": r["text"][:120],
                                           "similarity": round(s, 3)} for s, _m, r in related]
            return out
        out["verdict"] = "committed"
        out.update(self._activate(row, caller))
        return out

    def _reinforce(self, row: dict, conf: float, caller: Caller, commit: bool = True) -> None:
        new = min(1.0, max(float(row["confidence"]), conf) + 0.05)
        self.store.update(row["id"], caller.identity, "reinforce", confidence=round(new, 4),
                          reinforced=int(row.get("reinforced") or 0) + 1, last_used=time.time())

    def _activate(self, row: dict, caller: Caller) -> dict:
        """After a row becomes active: apply its supersede edges and the size budget."""
        superseded = []
        for sid in row.get("supersedes") or []:
            old = self.store.get(sid)
            if old is None or old["id"] == row["id"] or old["state"] not in ("active", "pending"):
                continue
            self.store.update(sid, caller.identity, "supersede", state="superseded",
                              superseded_by=row["id"], reason=f"superseded by {row['id']}")
            superseded.append(sid)
        out: dict = {}
        if superseded:
            out["superseded"] = superseded
        archived = self._enforce_budget((row["scope_kind"], row["scope_id"]), row["kind"],
                                        caller, keep=row["id"])
        if archived:
            out["archived"] = archived
        return out

    def _commit_row(self, row: dict, caller: Caller, conf: float | None = None) -> dict:
        new = max(float(row["confidence"]), conf or 0.0, self.cfg.commit_threshold)
        self.store.update(row["id"], caller.identity, "commit", state="active",
                          confidence=round(min(1.0, new), 4), reason=None)
        row = self.store.get(row["id"])
        return {"id": row["id"], **self._activate(row, caller)}

    async def commit(self, mid: str, caller: Caller, *, reject: bool = False,
                     reason: str | None = None, text: str | None = None) -> dict:
        row = self.store.get(mid)
        if row is None:
            raise KeyError(f"no memory {mid!r}")
        if row["state"] != "pending":
            raise ValueError(f"{mid} is {row['state']}, not pending")
        async with self._lock:
            if reject:
                self.store.update(mid, caller.identity, "reject", state="rejected",
                                  reason=(reason or "rejected on review")[:300])
                return {"id": mid, "verdict": "rejected"}
            if text is not None and rules.normalize(text) != row["text"]:
                # An edited proposal is a new memory (never edit in place).
                self.store.update(mid, caller.identity, "reject", state="rejected",
                                  reason="replaced by an edited commit")
                res = await self._propose_locked(text, row, caller)
                return res
            return {**self._commit_row(row, caller), "verdict": "committed"}

    async def _propose_locked(self, text: str, row: dict, caller: Caller) -> dict:
        cfg = self.cfg
        verdict = rules.screen(text, row["kind"], confidence=max(row["confidence"], cfg.commit_threshold),
                               secrets=cfg.secrets, vault_values=self._vault_values(),
                               max_chars=cfg.episode_chars if row["kind"] == "episode" else cfg.max_entry_chars)
        if verdict.reject:
            return {"verdict": "rejected", **verdict.to_dict()}
        verdict.confidence = max(verdict.confidence, cfg.commit_threshold)
        return await self._propose(verdict, row["kind"], (row["scope_kind"], row["scope_id"]), caller,
                                   supersedes=row.get("supersedes"), tags=row.get("tags"),
                                   origin="agent", source=row.get("source"), journal=row.get("journal"),
                                   session=row.get("session"), force_pending=False, created=None,
                                   author=None)

    async def forget(self, mid: str, caller: Caller, reason: str = "", purge: bool = False) -> dict:
        row = self.store.get(mid)
        if row is None:
            raise KeyError(f"no memory {mid!r}")
        fields: dict[str, Any] = {"state": "retracted", "reason": (reason or "forgotten")[:300]}
        if purge:
            fields.update(text="[purged]", hash="purged")
            self.store.drop_vector(mid)
        self.store.update(mid, caller.identity, "purge" if purge else "retract", **fields)
        return {"id": mid, "state": "retracted", "purged": bool(purge)}

    # -- budgets / maintenance ------------------------------------------
    def _enforce_budget(self, scope, kind: str, caller: Caller, keep: str | None = None) -> list[str]:
        budget = int(self.cfg.budgets.get(kind) or 0)
        if budget <= 0:
            return []
        rows = self.store.select([scope], [kind], ("active",), limit=5000)
        total = sum(len(r["text"]) for r in rows)
        if total <= budget:
            return []
        now = time.time()
        victims = sorted((r for r in rows if r["id"] != keep and float(r["confidence"]) < 1.0),
                         key=lambda r: (self.strength(r, now), r["created"]))
        archived = []
        for r in victims:
            if total <= budget:
                break
            self.store.update(r["id"], caller.identity, "archive", state="archived",
                              reason=f"over the {kind} budget ({budget} chars)")
            total -= len(r["text"])
            archived.append(r["id"])
        return archived

    async def maintain(self, caller: Caller | None = None, index_batch: int = 32) -> dict:
        """Expire stale proposals, decay unused memories, merge near-duplicates,
        cap episodes, enforce budgets, and embed rows that lack vectors."""
        caller = caller or Caller(identity="system:memory-maintenance")
        cfg = self.cfg
        now = time.time()
        stats = {"expired": 0, "decayed": 0, "consolidated": 0, "episodes_archived": 0,
                 "budget_archived": 0, "indexed": 0}
        async with self._lock:
            stats["indexed"] = await self._index(index_batch)
            for r in self.store.select(None, None, ("pending",), limit=5000):
                if now - r["created"] > cfg.pending_ttl_days * DAY:
                    self.store.update(r["id"], caller.identity, "expire", state="rejected",
                                      reason=f"pending for more than {cfg.pending_ttl_days} days")
                    stats["expired"] += 1
            active = self.store.select(None, None, ("active",), limit=20000)
            for r in active:
                if self.strength(r, now) < cfg.archive_below:
                    self.store.update(r["id"], caller.identity, "decay", state="archived",
                                      reason="decayed: unused and low confidence")
                    stats["decayed"] += 1
            groups: dict = {}
            for r in self.store.select(None, None, ("active",), limit=20000):
                groups.setdefault((r["scope_kind"], r["scope_id"], r["kind"]), []).append(r)
            model = self.embedder.last_model
            for (sk, sid, kind), rows in groups.items():
                stats["consolidated"] += self._consolidate(rows, model, caller)
                if kind == "episode" and len(rows) > cfg.episode_keep:
                    live = [r for r in rows if self.store.get(r["id"])["state"] == "active"]
                    for r in sorted(live, key=lambda r: -r["created"])[cfg.episode_keep:]:
                        self.store.update(r["id"], caller.identity, "archive", state="archived",
                                          reason=f"more than {cfg.episode_keep} episodes")
                        stats["episodes_archived"] += 1
                stats["budget_archived"] += len(self._enforce_budget((sk, sid), kind, caller))
        return stats

    def _consolidate(self, rows: list[dict], model: str | None, caller: Caller) -> int:
        if len(rows) < 2 or len(rows) > 2000:
            return 0
        vecs = self.store.vectors([r["id"] for r in rows], model) if model else {}
        rank = sorted(rows, key=lambda r: (-float(r["confidence"]), -int(r["recalls"] or 0),
                                           -r["updated"]))
        gone: set[str] = set()
        merged = 0
        for i, keep in enumerate(rank):
            if keep["id"] in gone:
                continue
            for other in rank[i + 1:]:
                if other["id"] in gone:
                    continue
                if keep["id"] in vecs and other["id"] in vecs:
                    dup = cosine(vecs[keep["id"]], vecs[other["id"]]) >= self.cfg.dedupe_similarity
                else:
                    dup = rules.jaccard(keep["text"], other["text"]) >= LEX_DUPLICATE
                if dup:
                    self.store.update(other["id"], caller.identity, "consolidate", state="superseded",
                                      superseded_by=keep["id"], reason=f"merged into {keep['id']}")
                    self._reinforce(keep, float(other["confidence"]), caller)
                    gone.add(other["id"])
                    merged += 1
        return merged

    async def _index(self, batch: int) -> int:
        emb = self.embedder
        if not emb.available():
            return 0
        rows = self.store.missing_vectors(emb.model or emb.last_model, batch)
        if not rows:
            return 0
        got = await emb.embed([r["text"] for r in rows])
        if not got:
            return 0
        model, vecs = got
        for r, v in zip(rows, vecs):
            self.store.put_vector(r["id"], model, v)
        return len(rows)

    # -- reads ------------------------------------------------------------
    async def recall(self, query: str, caller: Caller, *, scope=None, limit: int = 5,
                     kinds=None, include_archived: bool = False) -> dict:
        scopes = self.scopes(scope, caller)
        limit = max(1, min(int(limit or 5), 50))
        states = ("active", "archived") if include_archived else ("active",)
        kinds = [k for k in (kinds if isinstance(kinds, list) else str(kinds or "").split(","))
                 if k.strip()] or None
        if kinds:
            kinds = [k.strip() for k in kinds]
            bad = [k for k in kinds if k not in rules.KINDS]
            if bad:
                raise ValueError(f"unknown kind(s) {bad}; kinds are {', '.join(rules.KINDS)}")
        scores: dict[str, float] = {}
        rows: dict[str, dict] = {}
        for n, r in enumerate(self.store.lexical(query, scopes, states, 50)):
            rows[r["id"]] = r
            scores[r["id"]] = 1 / (30 + n)
        semantic = False
        vec = await self._vector(query[:1000]) if query.strip() else None
        if vec:
            ranked = []
            for r, v in self.store.iter_vectors(scopes, vec[0], states):
                c = cosine(vec[1], v)
                if c >= 0.3:
                    ranked.append((c, r))
            ranked.sort(key=lambda x: -x[0])
            for n, (c, r) in enumerate(ranked[:50]):
                rows.setdefault(r["id"], r)
                scores[r["id"]] = scores.get(r["id"], 0) + 1 / (30 + n)
            semantic = True
        now = time.time()
        for mid, r in rows.items():
            scores[mid] *= 0.5 + 0.5 * self.strength(r, now)
        picked = [rows[m] for m, _ in sorted(scores.items(), key=lambda x: (-x[1], x[0]))
                  if not kinds or rows[m]["kind"] in kinds][:limit]
        self.store.touch([r["id"] for r in picked if r["state"] == "active"])
        return {"results": [self.compact(r, scores[r["id"]]) for r in picked],
                "scopes": [f"{a}:{b}" for a, b in scopes], "semantic": semantic}

    @staticmethod
    def compact(r: dict, score: float | None = None) -> dict:
        out = {"id": r["id"], "kind": r["kind"], "scope": f"{r['scope_kind']}:{r['scope_id']}",
               "text": r["text"][:400], "confidence": round(float(r["confidence"]), 2),
               "when": time.strftime("%Y-%m-%d", time.gmtime(r["created"]))}
        if r["state"] != "active":
            out["state"] = r["state"]
        if score is not None:
            out["score"] = round(score, 4)
        return out

    def digest(self, caller: Caller, scope=None, max_chars: int | None = None) -> str:
        """A compact, token-lean briefing for the start of a session: the
        user's profile and preferences, then procedures and facts by strength,
        then the latest episodes. Deterministic for a given store."""
        scopes = self.scopes(scope, caller)
        cap = max(200, min(int(max_chars or self.cfg.digest_chars), 8000))
        now = time.time()
        rows = self.store.select(scopes, None, ("active",), limit=5000)
        if not rows:
            return ""
        header = "Memory (" + ", ".join(f"{a}:{b}" for a, b in scopes) + ")"
        lines = [header]
        used = len(header)
        omitted = 0

        def add(line: str) -> bool:
            nonlocal used
            if used + len(line) + 1 > cap:
                return False
            lines.append(line)
            used += len(line) + 1
            return True

        for kind, title in DIGEST_SECTIONS:
            items = sorted((r for r in rows if r["kind"] == kind),
                           key=lambda r: (-self.strength(r, now), -r["created"], r["id"]))
            if not items:
                continue
            if not add(title + ":"):
                omitted += len(items)
                continue
            for i, r in enumerate(items):
                if not add("- " + _clip(r["text"], 200)):
                    omitted += len(items) - i
                    break
        eps = sorted((r for r in rows if r["kind"] == "episode"), key=lambda r: (-r["created"], r["id"]))
        if eps and add("Recent sessions:"):
            for i, r in enumerate(eps[:3]):
                day = time.strftime("%Y-%m-%d", time.gmtime(r["created"]))
                if not add(f"- {day} " + _clip(r["text"], 160)):
                    omitted += len(eps[:3]) - i
                    break
            omitted += max(0, len(eps) - 3)
        elif eps:
            omitted += len(eps)
        if omitted:
            tail = f"(+{omitted} more: memory.recall)"
            if used + len(tail) + 1 > cap and len(lines) > 1:
                lines.pop()
            lines.append(tail)
        return "\n".join(lines)

    # -- ingest -----------------------------------------------------------
    async def ingest(self, pages, caller: Caller, *, worker: str = "", agent: str = "",
                     session_id: str = "") -> dict:
        session, messages = ingest_mod.check_pages(pages)
        agent = agent or str(session.get("agent") or "agent")
        session_id = session_id or str(session.get("session_id") or "")
        if not session_id:
            raise ValueError("session_id is required (or a first page carrying session)")
        if not messages:
            return {"ingested": 0, "skipped": "no messages"}
        key = f"{worker or 'local'}/{agent}/{session_id}"
        prev = self.store.ingested(key)
        last = messages[-1]["index"]
        if prev and last <= prev["last_index"]:
            return {"ingested": 0, "skipped": "no new messages", "episode": prev.get("episode")}
        new = [m for m in messages if not prev or m["index"] > prev["last_index"]]
        summary = None
        if self._summarizer is not None:
            try:
                summary = await self._summarizer(session, messages)
            except Exception as e:  # noqa: BLE001 - fall back to the extractive summary
                log.warning("memory: summarizer failed (%s); using the extractive summary", e)
        summary = summary or ingest_mod.summarize(session, messages, self.cfg.episode_chars)
        source = f"transcript:{key}#{messages[0]['index']}-{last}"
        ep_caller = Caller(identity=caller.identity, actor=caller.actor, family=caller.family or agent,
                           session=session_id, task=caller.task, kind=caller.kind)
        created = _parse_ts(session.get("started") or messages[0].get("ts"))
        supersedes = [prev["episode"]] if prev and prev.get("episode") else None
        ep = await self.propose(summary, "episode", ep_caller, origin="summary", source=source,
                                session=session_id, secrets="mask", supersedes=supersedes,
                                tags=["transcript", agent], created=created)
        proposals = []
        for c in ingest_mod.extract(new):
            res = await self.propose(c["text"], c["kind"], ep_caller, origin="transcript",
                                     source=f"transcript:{key}#{c['index']}", session=session_id,
                                     secrets="mask", tags=["transcript", c["signal"]],
                                     force_pending=not self.cfg.ingest_autocommit)
            proposals.append({k: res.get(k) for k in ("id", "verdict", "kind", "text") if res.get(k)}
                             | {"text": c["text"][:120]})
        self.store.mark_ingested(key, last, ep.get("id"))
        return {"ingested": len(new), "episode": ep.get("id"), "episode_verdict": ep.get("verdict"),
                "proposals": proposals}

    async def import_candidates(self, cands: list[dict], caller: Caller) -> dict:
        """Bridge legacy vault post-its/notes in, idempotently (by source)."""
        stats = {"imported": 0, "existing": 0, "rejected": 0, "duplicate": 0}
        ids: dict[str, str] = {}
        for c in cands:
            prior = self.store.by_source(c["source"])
            if prior is not None:
                ids[c["source"]] = prior["id"]
                stats["existing"] += 1
                continue
            sk, sid = c["scope"]
            scope = f"{sk}:{sid}" if sid else sk
            sup = [ids[s] for s in c.get("legacy_supersedes", []) if s in ids]
            res = await self.propose(c["text"], c["kind"], caller, scope=scope,
                                     confidence=c.get("confidence"), origin="vault",
                                     source=c["source"], session=c.get("session"),
                                     secrets="mask", supersedes=sup, tags=c.get("tags"),
                                     created=c.get("created"), author=c.get("author"))
            v = res.get("verdict")
            if v == "rejected":
                stats["rejected"] += 1
                continue
            if res.get("id"):
                ids[c["source"]] = res["id"]
            stats["duplicate" if v == "duplicate" else "imported"] += 1
        return stats


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 2].rsplit(" ", 1)[0] + " …"


def _parse_ts(value) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
