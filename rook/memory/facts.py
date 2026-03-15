"""3-tier promotion-based fact store — volatile, working, concrete."""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TIER_VOLATILE = "volatile"
TIER_WORKING = "working"
TIER_CONCRETE = "concrete"
TIER_ARCHIVED = "archived"

DEFAULT_TIER_SIZE = 8000  # tokens per tier


@dataclass
class MemoryFact:
    id: str
    fact: str
    category: str = "general"
    importance: float = 0.5
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)

    @property
    def token_estimate(self) -> int:
        return max(1, int(len(self.fact) / 3.5))

    def touch(self) -> None:
        self.access_count += 1
        self.last_accessed = time.time()

    def to_row(self, tier: str) -> tuple:
        return (
            self.id, self.fact, self.category, tier,
            self.importance, self.access_count,
            self.created_at, self.last_accessed,
        )

    @staticmethod
    def from_row(row: dict) -> MemoryFact:
        return MemoryFact(
            id=row["id"],
            fact=row["fact"],
            category=row.get("category", "general"),
            importance=row.get("importance", 0.5),
            access_count=row.get("access_count", 0),
            created_at=row.get("created_at", time.time()),
            last_accessed=row.get("last_accessed", time.time()),
        )


class FactStore:
    """Manages 3 promotion tiers of MemoryFacts with SQLite persistence."""

    def __init__(
        self,
        db: sqlite3.Connection,
        tier_size: int = DEFAULT_TIER_SIZE,
        promote_threshold: int = 3,
        concrete_threshold: int = 6,
    ):
        self._db = db
        self.tier_size = tier_size
        self.promote_threshold = promote_threshold
        self.concrete_threshold = concrete_threshold

        self.volatile: list[MemoryFact] = []
        self.working: list[MemoryFact] = []
        self.concrete: list[MemoryFact] = []

        self._init_table()
        self._load_from_db()

    def _init_table(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_facts (
                id TEXT PRIMARY KEY,
                fact TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                tier TEXT NOT NULL,
                importance REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                created_at REAL,
                last_accessed REAL
            );
            CREATE TABLE IF NOT EXISTS conversation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                token_estimate INTEGER DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );
        """)
        self._db.commit()

    def _load_from_db(self) -> None:
        """Load facts from SQLite into tier lists on startup."""
        cursor = self._db.execute(
            "SELECT * FROM memory_facts WHERE tier IN (?, ?, ?)",
            (TIER_VOLATILE, TIER_WORKING, TIER_CONCRETE),
        )
        cursor.row_factory = sqlite3.Row
        rows = cursor.fetchall()

        for row in rows:
            fact = MemoryFact.from_row(dict(row))
            tier = row["tier"]
            if tier == TIER_VOLATILE:
                self.volatile.append(fact)
            elif tier == TIER_WORKING:
                self.working.append(fact)
            elif tier == TIER_CONCRETE:
                self.concrete.append(fact)

        # Sort by last_accessed (most recent last for volatile stack behavior)
        self.volatile.sort(key=lambda f: f.created_at)
        self.working.sort(key=lambda f: f.last_accessed)
        self.concrete.sort(key=lambda f: f.last_accessed)

        log.info(
            "Loaded facts from DB: volatile=%d, working=%d, concrete=%d",
            len(self.volatile), len(self.working), len(self.concrete),
        )

    # -- Tier token counts --

    def tier_tokens(self, tier: list[MemoryFact]) -> int:
        return sum(f.token_estimate for f in tier)

    def status(self) -> dict[str, Any]:
        return {
            "volatile": {"count": len(self.volatile), "tokens": self.tier_tokens(self.volatile), "max": self.tier_size},
            "working": {"count": len(self.working), "tokens": self.tier_tokens(self.working), "max": self.tier_size},
            "concrete": {"count": len(self.concrete), "tokens": self.tier_tokens(self.concrete), "max": self.tier_size},
        }

    # -- Add facts --

    def add_volatile(self, fact: str, category: str = "general", importance: float = 0.5) -> MemoryFact:
        """Push a fact onto the volatile stack. Evicts oldest if full."""
        mf = MemoryFact(
            id=str(uuid.uuid4())[:8],
            fact=fact,
            category=category,
            importance=importance,
        )
        self.volatile.append(mf)
        self._evict_volatile()
        return mf

    def add_working(self, fact: str, category: str = "general", importance: float = 0.7) -> MemoryFact:
        """Add a fact directly to working tier (e.g., explicit 'remember' calls)."""
        mf = MemoryFact(
            id=str(uuid.uuid4())[:8],
            fact=fact,
            category=category,
            importance=importance,
            access_count=self.promote_threshold,  # already "promoted"
        )
        self.working.append(mf)
        self._evict_working()
        return mf

    # -- Eviction --

    def _evict_volatile(self) -> None:
        """LIFO eviction — oldest facts fall off the bottom."""
        while self.tier_tokens(self.volatile) > self.tier_size and self.volatile:
            dropped = self.volatile.pop(0)  # oldest first
            log.debug("Volatile eviction: %s", dropped.fact[:60])

    def _evict_working(self) -> None:
        """LRU eviction — least recently accessed drops to volatile."""
        while self.tier_tokens(self.working) > self.tier_size and self.working:
            # Find LRU
            lru = min(self.working, key=lambda f: f.last_accessed)
            self.working.remove(lru)
            lru.access_count = 0  # reset on demotion
            self.volatile.append(lru)
            log.debug("Working eviction → volatile: %s", lru.fact[:60])
        self._evict_volatile()

    def _evict_concrete(self) -> None:
        """LRU eviction — least recently accessed gets archived to disk."""
        while self.tier_tokens(self.concrete) > self.tier_size and self.concrete:
            lru = min(self.concrete, key=lambda f: f.last_accessed)
            self.concrete.remove(lru)
            self._archive_fact(lru)
            log.info("Concrete eviction → archived: %s", lru.fact[:60])

    def _archive_fact(self, fact: MemoryFact) -> None:
        """Write a fact to disk as archived."""
        self._db.execute(
            """INSERT OR REPLACE INTO memory_facts
               (id, fact, category, tier, importance, access_count, created_at, last_accessed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            fact.to_row(TIER_ARCHIVED),
        )
        self._db.commit()

    # -- Promotion --

    def check_promotions(self) -> list[str]:
        """Check all facts for promotion eligibility. Returns log of actions."""
        actions = []

        # Volatile → Working
        to_promote = [f for f in self.volatile if f.access_count >= self.promote_threshold]
        for f in to_promote:
            self.volatile.remove(f)
            self.working.append(f)
            actions.append(f"Promoted to working: {f.fact[:60]}")
            log.info("Auto-promote volatile→working: %s (accesses=%d)", f.fact[:60], f.access_count)

        # Working → Concrete
        to_promote = [f for f in self.working if f.access_count >= self.concrete_threshold]
        for f in to_promote:
            self.working.remove(f)
            self.concrete.append(f)
            actions.append(f"Promoted to concrete: {f.fact[:60]}")
            log.info("Auto-promote working→concrete: %s (accesses=%d)", f.fact[:60], f.access_count)

        self._evict_working()
        self._evict_concrete()
        return actions

    def promote(self, fact_id: str | None = None, keyword: str | None = None) -> str:
        """Explicitly promote a fact by ID or keyword match."""
        fact, tier = self._find_fact(fact_id, keyword)
        if not fact:
            return "No matching fact found."

        if tier == TIER_VOLATILE:
            self.volatile.remove(fact)
            self.working.append(fact)
            fact.touch()
            self._evict_working()
            return f"Promoted to working: {fact.fact[:80]}"
        elif tier == TIER_WORKING:
            self.working.remove(fact)
            self.concrete.append(fact)
            fact.touch()
            self._evict_concrete()
            return f"Promoted to concrete: {fact.fact[:80]}"
        else:
            return f"Already in concrete tier."

    def demote(self, fact_id: str | None = None, keyword: str | None = None) -> str:
        """Explicitly demote or archive a fact."""
        fact, tier = self._find_fact(fact_id, keyword)
        if not fact:
            return "No matching fact found."

        if tier == TIER_CONCRETE:
            self.concrete.remove(fact)
            self.working.append(fact)
            fact.access_count = max(0, fact.access_count - 3)
            self._evict_working()
            return f"Demoted to working: {fact.fact[:80]}"
        elif tier == TIER_WORKING:
            self.working.remove(fact)
            self.volatile.append(fact)
            fact.access_count = 0
            self._evict_volatile()
            return f"Demoted to volatile: {fact.fact[:80]}"
        elif tier == TIER_VOLATILE:
            self.volatile.remove(fact)
            self._archive_fact(fact)
            return f"Archived: {fact.fact[:80]}"
        return "Nothing to demote."

    def _find_fact(self, fact_id: str | None, keyword: str | None) -> tuple[MemoryFact | None, str]:
        """Find a fact by ID or keyword across all tiers."""
        for tier_name, tier_list in [
            (TIER_CONCRETE, self.concrete),
            (TIER_WORKING, self.working),
            (TIER_VOLATILE, self.volatile),
        ]:
            for f in tier_list:
                if fact_id and f.id == fact_id:
                    return f, tier_name
                if keyword and keyword.lower() in f.fact.lower():
                    return f, tier_name
        return None, ""

    # -- Access tracking --

    def scan_for_references(self, text: str) -> int:
        """Scan text for references to existing facts. Increments access_count. Returns count."""
        text_lower = text.lower()
        touched = 0
        for tier_list in [self.volatile, self.working, self.concrete]:
            for f in tier_list:
                # Extract key terms from the fact (words > 4 chars)
                terms = [w for w in f.fact.lower().split() if len(w) > 4]
                if any(term in text_lower for term in terms[:5]):  # check first 5 terms
                    f.touch()
                    touched += 1
        return touched

    # -- Search --

    def search(self, query: str, include_archived: bool = True) -> list[dict]:
        """Search across all tiers + optionally archived."""
        results = []
        query_lower = query.lower()

        for tier_name, tier_list in [
            (TIER_CONCRETE, self.concrete),
            (TIER_WORKING, self.working),
            (TIER_VOLATILE, self.volatile),
        ]:
            for f in tier_list:
                if query_lower in f.fact.lower() or query_lower in f.category.lower():
                    results.append({
                        "id": f.id, "fact": f.fact, "tier": tier_name,
                        "category": f.category, "access_count": f.access_count,
                    })

        if include_archived:
            cursor = self._db.execute(
                "SELECT * FROM memory_facts WHERE tier = ? AND (fact LIKE ? OR category LIKE ?)",
                (TIER_ARCHIVED, f"%{query}%", f"%{query}%"),
            )
            cursor.row_factory = sqlite3.Row
            for row in cursor.fetchall():
                results.append({
                    "id": row["id"], "fact": row["fact"], "tier": TIER_ARCHIVED,
                    "category": row["category"], "access_count": row["access_count"],
                })

        return results

    # -- Persistence --

    def flush_to_db(self) -> None:
        """Persist all tiers to SQLite. Called every maintenance cycle."""
        for tier_name, tier_list in [
            (TIER_VOLATILE, self.volatile),
            (TIER_WORKING, self.working),
            (TIER_CONCRETE, self.concrete),
        ]:
            for f in tier_list:
                self._db.execute(
                    """INSERT OR REPLACE INTO memory_facts
                       (id, fact, category, tier, importance, access_count, created_at, last_accessed)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    f.to_row(tier_name),
                )
        # Clean up: remove facts from DB that are no longer in any active tier
        active_ids = {f.id for f in self.volatile + self.working + self.concrete}
        if active_ids:
            placeholders = ",".join("?" * len(active_ids))
            self._db.execute(
                f"DELETE FROM memory_facts WHERE tier != ? AND id NOT IN ({placeholders})",
                (TIER_ARCHIVED, *active_ids),
            )
        self._db.commit()
        log.debug("Flushed facts to DB: v=%d w=%d c=%d", len(self.volatile), len(self.working), len(self.concrete))

    def log_conversation(self, session_id: str, role: str, content: str) -> None:
        """Log a conversation turn to the conversation_log table."""
        token_est = int(len(content) / 3.5)
        self._db.execute(
            "INSERT INTO conversation_log (session_id, role, content, token_estimate, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, token_est, time.time()),
        )
        self._db.commit()

    # -- Rendering --

    def render_tier(self, tier_list: list[MemoryFact]) -> str:
        """Render a tier's facts as text for system prompt injection."""
        if not tier_list:
            return "(empty)"
        lines = []
        for f in tier_list:
            lines.append(f"  [{f.id}] ({f.category}) {f.fact}")
        return "\n".join(lines)
