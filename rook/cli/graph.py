"""Rook Knowledge Graph — Dewey Decimal for your brain.

Not a knowledge graph. A knowledge MAP. Stores WHERE information lives
and how concepts relate, not the information itself.

Node types:
  Project   — living, mutable context (DROGA, Rook, LISA). Has status, events, phases.
  Concept   — keyword/topic (orthogonal_transforms, mode_collapse, mamba3)
  Source    — pointer to content (conversation, file, url, machine)

Edge types:
  MENTIONS       — concept/project appears in a source
  RELATED_TO     — concept↔concept or project↔concept
  LOCATED_ON     — source lives on a machine
  PART_OF        — source belongs to a project

Flat tables (SQLite, not graph):
  cli_log        — command sequences with context and outcomes
  web_cache      — past web searches with results
  search_log     — queries already performed (dedup)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

import kuzu

log = logging.getLogger("rook.graph")

GRAPH_DIR = Path.home() / ".rook" / "graph"
GRAPH_DB = GRAPH_DIR / "rook.kuzu"
FLAT_DB = GRAPH_DIR / "lookup.db"


def _ensure_dirs():
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)


# ── KuzuDB Schema ───────────────────────────────────────────────────────────

_SCHEMA_APPLIED_FLAG = GRAPH_DIR / ".schema_v1"


def init_graph() -> tuple[kuzu.Database, kuzu.Connection]:
    """Initialize graph DB and apply schema if needed."""
    _ensure_dirs()
    db = kuzu.Database(str(GRAPH_DB))
    conn = kuzu.Connection(db)

    if not _SCHEMA_APPLIED_FLAG.exists():
        _apply_schema(conn)
        _SCHEMA_APPLIED_FLAG.write_text("v1")
    return db, conn


def _apply_schema(conn: kuzu.Connection):
    """Create all node and edge tables."""
    stmts = [
        # Nodes
        """CREATE NODE TABLE IF NOT EXISTS Project(
            id STRING,
            name STRING,
            status STRING DEFAULT 'active',
            description STRING DEFAULT '',
            last_updated INT64 DEFAULT 0,
            PRIMARY KEY(id)
        )""",
        """CREATE NODE TABLE IF NOT EXISTS Concept(
            id STRING,
            name STRING,
            category STRING DEFAULT 'general',
            PRIMARY KEY(id)
        )""",
        """CREATE NODE TABLE IF NOT EXISTS Source(
            id STRING,
            type STRING,
            location STRING,
            title STRING DEFAULT '',
            machine STRING DEFAULT '',
            timestamp INT64 DEFAULT 0,
            PRIMARY KEY(id)
        )""",
        # Edges
        "CREATE REL TABLE IF NOT EXISTS MENTIONS(FROM Concept TO Source, turn_ids STRING DEFAULT '', weight FLOAT DEFAULT 1.0)",
        "CREATE REL TABLE IF NOT EXISTS RELATED_TO(FROM Concept TO Concept, weight FLOAT DEFAULT 1.0)",
        "CREATE REL TABLE IF NOT EXISTS PROJECT_MENTIONS(FROM Project TO Source, context STRING DEFAULT '')",
        "CREATE REL TABLE IF NOT EXISTS PROJECT_CONCEPT(FROM Project TO Concept)",
        "CREATE REL TABLE IF NOT EXISTS PART_OF(FROM Source TO Project)",
    ]
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except Exception as e:
            log.debug("Schema stmt: %s", e)


# ── Flat lookup tables (SQLite) ──────────────────────────────────────────────

def init_flat_db() -> sqlite3.Connection:
    _ensure_dirs()
    db = sqlite3.connect(str(FLAT_DB))
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS cli_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            commands TEXT NOT NULL,
            context TEXT DEFAULT '',
            outcome TEXT DEFAULT '',
            resolution TEXT DEFAULT '',
            cost_hint TEXT DEFAULT 'low',
            timestamp REAL,
            session_id TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS web_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            url TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            timestamp REAL,
            UNIQUE(query, url)
        );

        CREATE TABLE IF NOT EXISTS search_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            source TEXT DEFAULT '',
            result_count INTEGER DEFAULT 0,
            result_summary TEXT DEFAULT '',
            timestamp REAL
        );

        CREATE TABLE IF NOT EXISTS project_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            event_type TEXT DEFAULT 'update',
            summary TEXT NOT NULL,
            details TEXT DEFAULT '',
            source_id TEXT DEFAULT '',
            timestamp REAL
        );
        CREATE INDEX IF NOT EXISTS idx_events_project ON project_events(project_id, timestamp DESC);
    """)
    db.commit()
    return db


# ── Graph Operations ─────────────────────────────────────────────────────────

def _normalize_id(text: str) -> str:
    """Normalize text to a graph-safe ID."""
    return re.sub(r'[^a-z0-9_]', '_', text.lower().strip()).strip('_')[:80]


def _escape(s: str) -> str:
    """Escape string for Cypher."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


class _GraphSession:
    """Context manager that opens KuzuDB, yields a connection, and closes on exit."""
    def __enter__(self):
        self._db, self._conn = init_graph()
        return self._conn

    def __exit__(self, *args):
        del self._conn
        del self._db


def _graph_session() -> _GraphSession:
    return _GraphSession()


class RookGraph:
    """High-level interface to the knowledge graph.

    Opens KuzuDB per-operation via context manager to avoid holding file locks.
    The flat SQLite DB stays open (SQLite handles concurrent reads fine).
    """

    def __init__(self):
        self._flat = init_flat_db()
        # Schema is applied on first _graph_session() call, not here
        # This avoids holding the lock at init time

    def close(self):
        pass

    # ── Lookup (the main read path) ──────────────────────────────────────

    def _run_graph(self, cypher: str) -> list[list]:
        """Execute a Cypher query, return all rows. Opens/closes DB per call."""
        rows = []
        with _graph_session() as conn:
            try:
                r = conn.execute(cypher)
                while r.has_next():
                    rows.append(r.get_next())
            except Exception as e:
                log.debug("Cypher: %s — %s", cypher[:80], e)
        return rows

    def _run_graph_write(self, cypher: str):
        """Execute a write Cypher statement."""
        with _graph_session() as conn:
            try:
                conn.execute(cypher)
            except Exception as e:
                log.debug("Cypher write: %s — %s", cypher[:80], e)

    def lookup(self, query: str, max_hops: int = 2, limit: int = 20) -> dict:
        """Look up a query in the graph. Returns cascading related context.

        This is THE primary function — called before any research.
        """
        query_id = _normalize_id(query)
        query_lower = query.lower()
        results = {
            "projects": [],
            "concepts": [],
            "sources": [],
            "cli_history": [],
            "web_cache": [],
            "past_searches": [],
        }

        # 1. Direct match on concepts
        for row in self._run_graph(f"""
            MATCH (c:Concept)
            WHERE c.id = '{_escape(query_id)}' OR c.name CONTAINS '{_escape(query_lower)}'
            RETURN c.id, c.name, c.category LIMIT {limit}
        """):
            results["concepts"].append({"id": row[0], "name": row[1], "category": row[2]})

        # 2. Direct match on projects
        for row in self._run_graph(f"""
            MATCH (p:Project)
            WHERE p.id = '{_escape(query_id)}' OR p.name CONTAINS '{_escape(query_lower)}'
            RETURN p.id, p.name, p.status, p.description LIMIT {limit}
        """):
            proj = {"id": row[0], "name": row[1], "status": row[2], "description": row[3]}
            events = self._flat.execute(
                "SELECT summary, event_type, timestamp FROM project_events WHERE project_id=? ORDER BY timestamp DESC LIMIT 10",
                (row[0],)
            ).fetchall()
            proj["recent_events"] = [dict(e) for e in events]
            results["projects"].append(proj)

        # 3. Cascade — sources mentioned by matched concepts (1-2 hops)
        concept_ids = [c["id"] for c in results["concepts"]]

        for cid in concept_ids[:5]:
            for row in self._run_graph(f"""
                MATCH (c:Concept {{id: '{_escape(cid)}'}})-[m:MENTIONS]->(s:Source)
                RETURN s.id, s.type, s.location, s.title, s.machine, m.turn_ids, m.weight
                ORDER BY m.weight DESC LIMIT {limit}
            """):
                results["sources"].append({
                    "id": row[0], "type": row[1], "location": row[2],
                    "title": row[3], "machine": row[4],
                    "turn_ids": row[5], "weight": row[6],
                    "via_concept": cid,
                })

            if max_hops >= 2:
                for row in self._run_graph(f"""
                    MATCH (c:Concept {{id: '{_escape(cid)}'}})-[r:RELATED_TO]->(c2:Concept)
                    RETURN c2.id, c2.name, c2.category, r.weight
                    ORDER BY r.weight DESC LIMIT 5
                """):
                    if row[0] not in concept_ids:
                        results["concepts"].append({
                            "id": row[0], "name": row[1], "category": row[2],
                            "via_relation": cid, "weight": row[3],
                        })

        # 4. Flat lookups
        # CLI history
        rows = self._flat.execute(
            "SELECT commands, context, outcome, resolution, cost_hint, timestamp FROM cli_log WHERE context LIKE ? OR commands LIKE ? ORDER BY timestamp DESC LIMIT 5",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        results["cli_history"] = [dict(r) for r in rows]

        # Web cache
        rows = self._flat.execute(
            "SELECT query, url, summary, timestamp FROM web_cache WHERE query LIKE ? OR summary LIKE ? ORDER BY timestamp DESC LIMIT 5",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        results["web_cache"] = [dict(r) for r in rows]

        # Past searches
        rows = self._flat.execute(
            "SELECT query, source, result_count, result_summary, timestamp FROM search_log WHERE query LIKE ? ORDER BY timestamp DESC LIMIT 5",
            (f"%{query}%",),
        ).fetchall()
        results["past_searches"] = [dict(r) for r in rows]

        return results

    # ── Index (the main write path) ──────────────────────────────────────

    def index_concept(self, name: str, category: str = "general") -> str:
        cid = _normalize_id(name)
        self._run_graph_write(f"""
            MERGE (c:Concept {{id: '{_escape(cid)}'}})
            SET c.name = '{_escape(name.lower())}', c.category = '{_escape(category)}'
        """)
        return cid

    def index_source(self, source_type: str, location: str, title: str = "",
                     machine: str = "kaiju") -> str:
        sid = _normalize_id(f"{source_type}:{location[:60]}")
        self._run_graph_write(f"""
            MERGE (s:Source {{id: '{_escape(sid)}'}})
            SET s.type = '{_escape(source_type)}', s.location = '{_escape(location)}',
                s.title = '{_escape(title)}', s.machine = '{_escape(machine)}',
                s.timestamp = {int(time.time())}
        """)
        return sid

    def index_project(self, name: str, status: str = "active", description: str = "") -> str:
        pid = _normalize_id(name)
        self._run_graph_write(f"""
            MERGE (p:Project {{id: '{_escape(pid)}'}})
            SET p.name = '{_escape(name)}', p.status = '{_escape(status)}',
                p.description = '{_escape(description)}', p.last_updated = {int(time.time())}
        """)
        return pid

    def link_concept_source(self, concept_id: str, source_id: str,
                            turn_ids: str = "", weight: float = 1.0):
        self._run_graph_write(f"""
            MATCH (c:Concept {{id: '{_escape(concept_id)}'}}), (s:Source {{id: '{_escape(source_id)}'}})
            MERGE (c)-[m:MENTIONS]->(s)
            SET m.turn_ids = '{_escape(turn_ids)}', m.weight = {weight}
        """)

    def link_concepts(self, concept_a: str, concept_b: str, weight: float = 1.0):
        self._run_graph_write(f"""
            MATCH (a:Concept {{id: '{_escape(concept_a)}'}}), (b:Concept {{id: '{_escape(concept_b)}'}})
            MERGE (a)-[r:RELATED_TO]->(b)
            SET r.weight = {weight}
        """)

    def link_project_concept(self, project_id: str, concept_id: str):
        self._run_graph_write(f"""
            MATCH (p:Project {{id: '{_escape(project_id)}'}}), (c:Concept {{id: '{_escape(concept_id)}'}})
            MERGE (p)-[:PROJECT_CONCEPT]->(c)
        """)

    def add_project_event(self, project_id: str, summary: str,
                          event_type: str = "update", details: str = "",
                          source_id: str = ""):
        self._flat.execute(
            "INSERT INTO project_events (project_id, event_type, summary, details, source_id, timestamp) VALUES (?,?,?,?,?,?)",
            (project_id, event_type, summary, details, source_id, time.time()),
        )
        self._flat.commit()
        self._run_graph_write(f"""
            MATCH (p:Project {{id: '{_escape(project_id)}'}})
            SET p.last_updated = {int(time.time())}
        """)

    def get_project_status(self, project_id: str = "", limit: int = 5) -> list[dict]:
        if project_id:
            rows = self._run_graph(f"""
                MATCH (p:Project)
                WHERE p.id = '{_escape(project_id)}' OR p.name CONTAINS '{_escape(project_id)}'
                RETURN p.id, p.name, p.status, p.description, p.last_updated
                ORDER BY p.last_updated DESC LIMIT {limit}
            """)
        else:
            rows = self._run_graph(f"""
                MATCH (p:Project)
                RETURN p.id, p.name, p.status, p.description, p.last_updated
                ORDER BY p.last_updated DESC LIMIT {limit}
            """)
        projects = []
        for row in rows:
            events = self._flat.execute(
                "SELECT event_type, summary, timestamp FROM project_events WHERE project_id=? ORDER BY timestamp DESC LIMIT 10",
                (row[0],)
            ).fetchall()
            projects.append({
                "id": row[0], "name": row[1], "status": row[2],
                "description": row[3], "last_updated": row[4],
                "recent_events": [dict(e) for e in events],
            })
        return projects

    # ── Flat table operations ────────────────────────────────────────────

    def log_cli(self, commands: str, context: str = "", outcome: str = "",
                resolution: str = "", cost_hint: str = "low", session_id: str = ""):
        self._flat.execute(
            "INSERT INTO cli_log (commands, context, outcome, resolution, cost_hint, timestamp, session_id) VALUES (?,?,?,?,?,?,?)",
            (commands, context, outcome, resolution, cost_hint, time.time(), session_id),
        )
        self._flat.commit()

    def cache_web(self, query: str, url: str = "", summary: str = ""):
        self._flat.execute(
            "INSERT OR REPLACE INTO web_cache (query, url, summary, timestamp) VALUES (?,?,?,?)",
            (query, url, summary, time.time()),
        )
        self._flat.commit()

    def log_search(self, query: str, source: str = "", result_count: int = 0,
                   result_summary: str = ""):
        self._flat.execute(
            "INSERT INTO search_log (query, source, result_count, result_summary, timestamp) VALUES (?,?,?,?,?)",
            (query, source, result_count, result_summary, time.time()),
        )
        self._flat.commit()

    def check_web_cache(self, query: str) -> list[dict]:
        rows = self._flat.execute(
            "SELECT query, url, summary, timestamp FROM web_cache WHERE query LIKE ? ORDER BY timestamp DESC LIMIT 5",
            (f"%{query}%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def check_cli_history(self, context: str) -> list[dict]:
        rows = self._flat.execute(
            "SELECT commands, context, outcome, resolution, cost_hint, timestamp FROM cli_log WHERE context LIKE ? OR commands LIKE ? ORDER BY timestamp DESC LIMIT 5",
            (f"%{context}%", f"%{context}%"),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Bulk index helper ────────────────────────────────────────────────

    def index_finding(self, concepts: list[str], source_type: str, source_location: str,
                      source_title: str = "", project: str = "", turn_ids: str = "",
                      weight: float = 1.0, machine: str = "kaiju"):
        """Index a finding: multiple concepts pointing at a single source.

        This is the main "I found something" entry point.
        """
        sid = self.index_source(source_type, source_location, source_title, machine)

        concept_ids = []
        for c in concepts:
            cid = self.index_concept(c)
            self.link_concept_source(cid, sid, turn_ids=turn_ids, weight=weight)
            concept_ids.append(cid)

        # Link concepts to each other
        for i, a in enumerate(concept_ids):
            for b in concept_ids[i+1:]:
                self.link_concepts(a, b, weight=0.5)

        # Link to project if specified
        if project:
            pid = self.index_project(project)
            for cid in concept_ids:
                self.link_project_concept(pid, cid)

        return sid

    def stats(self) -> dict:
        counts = {}
        for table in ["Project", "Concept", "Source"]:
            rows = self._run_graph(f"MATCH (n:{table}) RETURN COUNT(n)")
            counts[table.lower() + "s"] = rows[0][0] if rows else 0

        for table in ["MENTIONS", "RELATED_TO", "PROJECT_CONCEPT"]:
            rows = self._run_graph(f"MATCH ()-[r:{table}]->() RETURN COUNT(r)")
            counts[table.lower()] = rows[0][0] if rows else 0

        counts["cli_logs"] = self._flat.execute("SELECT COUNT(*) FROM cli_log").fetchone()[0]
        counts["web_cache"] = self._flat.execute("SELECT COUNT(*) FROM web_cache").fetchone()[0]
        counts["search_logs"] = self._flat.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
        counts["project_events"] = self._flat.execute("SELECT COUNT(*) FROM project_events").fetchone()[0]
        return counts
