"""Memory tools — SQLite for structured data, KùzuDB for knowledge graph."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import kuzu

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)


class MemoryStore:
    """Shared backend for SQLite + KùzuDB. Initialized once, used by tools."""

    def __init__(self, sqlite_path: str = "./data/rook.db", graph_path: str = "./data/knowledge"):
        self.sqlite_path = Path(sqlite_path)
        self.graph_path = Path(graph_path)

        # Ensure parent directories exist (KùzuDB creates its own dir)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)

        # Init SQLite
        self._db = sqlite3.connect(str(self.sqlite_path))
        self._db.row_factory = sqlite3.Row
        self._init_sqlite()

        # Init KùzuDB
        self._graph_db = kuzu.Database(str(self.graph_path))
        self._graph_conn = kuzu.Connection(self._graph_db)
        self._init_graph()

        log.info("Memory store initialized: sqlite=%s, graph=%s", self.sqlite_path, self.graph_path)

    def _init_sqlite(self) -> None:
        """Create default tables if they don't exist."""
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                source TEXT DEFAULT '',
                confidence REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recall (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                context TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recall_key ON recall(key);

            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                platform_id TEXT NOT NULL,
                name TEXT DEFAULT '',
                modality TEXT NOT NULL DEFAULT 'text',
                session_id TEXT NOT NULL,
                last_active REAL,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_platform ON channels(platform, platform_id);
        """)
        self._db.commit()

    def _init_graph(self) -> None:
        """Create default node/relationship tables if they don't exist."""
        try:
            self._graph_conn.execute("""
                CREATE NODE TABLE IF NOT EXISTS Entity (
                    name STRING,
                    type STRING,
                    properties STRING,
                    PRIMARY KEY (name)
                )
            """)
            self._graph_conn.execute("""
                CREATE REL TABLE IF NOT EXISTS Related (
                    FROM Entity TO Entity,
                    relation STRING,
                    properties STRING
                )
            """)
            log.info("Graph schema initialized")
        except Exception as e:
            log.error("Graph init error: %s", e)

    # -- Channel tracking --

    def register_channel(
        self,
        platform: str,
        platform_id: str,
        session_id: str,
        name: str = "",
        modality: str = "text",
    ) -> None:
        """Register or update a communication channel."""
        import time
        import uuid
        now = time.time()
        self._db.execute(
            """INSERT INTO channels (id, platform, platform_id, name, modality, session_id, last_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform, platform_id) DO UPDATE SET
                 name=excluded.name,
                 modality=excluded.modality,
                 session_id=excluded.session_id,
                 last_active=excluded.last_active""",
            (str(uuid.uuid4())[:8], platform, platform_id, name, modality, session_id, now, now),
        )
        self._db.commit()

    def touch_channel(self, platform: str, platform_id: str) -> None:
        """Update last_active timestamp for a channel."""
        import time
        self._db.execute(
            "UPDATE channels SET last_active = ? WHERE platform = ? AND platform_id = ?",
            (time.time(), platform, platform_id),
        )
        self._db.commit()

    def list_channels(self) -> list[dict]:
        """List all known communication channels."""
        cursor = self._db.execute("SELECT * FROM channels ORDER BY last_active DESC")
        cursor.row_factory = sqlite3.Row
        return [dict(row) for row in cursor.fetchall()]

    # -- SQLite operations --

    def sql_execute(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute a SQL query and return results as list of dicts."""
        try:
            cursor = self._db.execute(query, params)
            self._db.commit()
            if cursor.description:
                columns = [d[0] for d in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
            return [{"rows_affected": cursor.rowcount}]
        except Exception as e:
            raise RuntimeError(f"SQL error: {e}") from e

    # -- Graph operations --

    def graph_query(self, cypher: str) -> list[dict]:
        """Execute a Cypher query on the knowledge graph."""
        try:
            result = self._graph_conn.execute(cypher)
            rows = []
            while result.has_next():
                row = result.get_next()
                rows.append({str(i): v for i, v in enumerate(row)})
            return rows
        except Exception as e:
            raise RuntimeError(f"Graph query error: {e}") from e

    def graph_add_entity(self, name: str, entity_type: str, properties: dict | None = None) -> None:
        """Add or update an entity node."""
        props_json = json.dumps(properties or {})
        self._graph_conn.execute(
            "MERGE (e:Entity {name: $name}) SET e.type = $type, e.properties = $props",
            parameters={"name": name, "type": entity_type, "props": props_json},
        )

    def graph_add_relation(self, from_entity: str, to_entity: str, relation: str, properties: dict | None = None) -> None:
        """Add a relationship between two entities."""
        props_json = json.dumps(properties or {})
        self._graph_conn.execute(
            """
            MATCH (a:Entity {name: $from}), (b:Entity {name: $to})
            CREATE (a)-[:Related {relation: $rel, properties: $props}]->(b)
            """,
            parameters={"from": from_entity, "to": to_entity, "rel": relation, "props": props_json},
        )


# -- Tools for the LLM --


class SQLQueryTool(Tool):
    """Let the LLM query and write to SQLite."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="db_query",
            description=(
                "Execute a SQL query on the local SQLite database. "
                "Available tables: notes (id, content, tags, created_at, updated_at), "
                "facts (id, subject, predicate, object, source, confidence, created_at), "
                "conversations (id, session_id, role, content, created_at). "
                "You can also CREATE new tables, INSERT, UPDATE, DELETE, or SELECT. "
                "Use this for storing and retrieving structured information."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute.",
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "")
        if not query:
            return ToolResult(success=False, output="", error="No query provided")

        log.info("db_query: %s", query[:200])
        try:
            results = self.store.sql_execute(query)
            output = json.dumps(results, indent=2, default=str)
            if len(output) > 4000:
                output = output[:4000] + "\n... (truncated)"
            return ToolResult(success=True, output=output)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class GraphQueryTool(Tool):
    """Let the LLM query and write to the knowledge graph."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="graph_query",
            description=(
                "Execute a Cypher query on the local KùzuDB knowledge graph. "
                "Node table: Entity (name STRING PK, type STRING, properties STRING as JSON). "
                "Relationship table: Related (relation STRING, properties STRING as JSON). "
                "Use MERGE to add entities, CREATE for relationships, MATCH for queries. "
                "Use this for storing and querying relationships between concepts, people, projects, etc."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Cypher query to execute.",
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "")
        if not query:
            return ToolResult(success=False, output="", error="No query provided")

        log.info("graph_query: %s", query[:200])
        try:
            results = self.store.graph_query(query)
            output = json.dumps(results, indent=2, default=str)
            if len(output) > 4000:
                output = output[:4000] + "\n... (truncated)"
            return ToolResult(success=True, output=output)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class GraphStoreTool(Tool):
    """Convenience tool for adding entities and relations without raw Cypher."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="graph_store",
            description=(
                "Store an entity or relationship in the knowledge graph. "
                "To add an entity: provide name, type, and optional properties. "
                "To add a relationship: provide from_entity, to_entity, and relation. "
                "Examples: store that 'bake' is a 'person', or that 'bake' 'works_on' 'Rook'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_entity", "add_relation"],
                        "description": "Whether to add an entity or a relationship.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Entity name (for add_entity).",
                    },
                    "type": {
                        "type": "string",
                        "description": "Entity type like 'person', 'project', 'concept' (for add_entity).",
                    },
                    "properties": {
                        "type": "object",
                        "description": "Optional properties as key-value pairs.",
                    },
                    "from_entity": {
                        "type": "string",
                        "description": "Source entity name (for add_relation).",
                    },
                    "to_entity": {
                        "type": "string",
                        "description": "Target entity name (for add_relation).",
                    },
                    "relation": {
                        "type": "string",
                        "description": "Relationship type like 'works_on', 'knows', 'part_of' (for add_relation).",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "")
        log.info("graph_store: %s %s", action, kwargs)

        try:
            if action == "add_entity":
                name = kwargs.get("name", "")
                etype = kwargs.get("type", "thing")
                props = kwargs.get("properties", {})
                if not name:
                    return ToolResult(success=False, output="", error="Entity name required")
                self.store.graph_add_entity(name, etype, props)
                return ToolResult(success=True, output=f"Entity '{name}' ({etype}) stored.")

            elif action == "add_relation":
                from_e = kwargs.get("from_entity", "")
                to_e = kwargs.get("to_entity", "")
                rel = kwargs.get("relation", "")
                props = kwargs.get("properties", {})
                if not all([from_e, to_e, rel]):
                    return ToolResult(success=False, output="", error="from_entity, to_entity, and relation required")
                self.store.graph_add_relation(from_e, to_e, rel, props)
                return ToolResult(success=True, output=f"Relation '{from_e}' -[{rel}]-> '{to_e}' stored.")

            else:
                return ToolResult(success=False, output="", error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class RememberTool(Tool):
    """Store a key-value pair for later recall."""

    def __init__(self, store: MemoryStore, fact_store=None):
        self.store = store
        self.fact_store = fact_store  # optional: also add to working tier

    def definition(self) -> ToolDef:
        return ToolDef(
            name="remember",
            description=(
                "Save something for later recall. Use this PROACTIVELY whenever you encounter: "
                "URLs, API keys, tokens, passwords, logins, config values, IP addresses, port numbers, "
                "version numbers, account IDs, license keys, server names, endpoint URLs, "
                "file paths that matter, command syntax that's non-obvious, or any specific detail "
                "that a human wouldn't memorize but might need again. "
                "Use a short descriptive key and store the exact value. "
                "If the key already exists, it will be updated."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short descriptive key, e.g. 'searxng_url', 'discord_bot_token', 'lm_studio_port'.",
                    },
                    "value": {
                        "type": "string",
                        "description": "The exact value to remember.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["url", "credential", "config", "command", "reference", "general"],
                        "description": "Category of the information.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Brief note on why this is stored or where it came from.",
                    },
                },
                "required": ["key", "value"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        key = kwargs.get("key", "")
        value = kwargs.get("value", "")
        category = kwargs.get("category", "general")
        context = kwargs.get("context", "")

        if not key or not value:
            return ToolResult(success=False, output="", error="key and value required")

        log.info("remember: [%s] %s = %s", category, key, value[:100])
        try:
            self.store.sql_execute(
                """INSERT INTO recall (key, value, category, context)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value,
                     category=excluded.category,
                     context=excluded.context,
                     updated_at=CURRENT_TIMESTAMP""",
                (key, value, category, context),
            )
            # Also add to working tier if fact_store is available
            if self.fact_store:
                self.fact_store.add_working(f"{key}: {value}", category)
                self.fact_store.flush_to_db()
            return ToolResult(success=True, output=f"Remembered: {key} = {value}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class RecallTool(Tool):
    """Search stored memories."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="recall",
            description=(
                "Search your memory for previously stored information. "
                "Search by key, category, or partial match. "
                "Use this when you need a URL, credential, config value, or any detail you stored before. "
                "Use this BEFORE asking the user for information you might already have."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term — matches against key, value, category, and context.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["url", "credential", "config", "command", "reference", "general"],
                        "description": "Optional: filter by category.",
                    },
                },
                "required": ["search"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        search = kwargs.get("search", "")
        category = kwargs.get("category")

        if not search:
            return ToolResult(success=False, output="", error="search term required")

        log.info("recall: %s (category=%s)", search, category)
        try:
            if category:
                results = self.store.sql_execute(
                    """SELECT key, value, category, context, updated_at FROM recall
                       WHERE category = ? AND (key LIKE ? OR value LIKE ? OR context LIKE ?)
                       ORDER BY updated_at DESC LIMIT 20""",
                    (category, f"%{search}%", f"%{search}%", f"%{search}%"),
                )
            else:
                results = self.store.sql_execute(
                    """SELECT key, value, category, context, updated_at FROM recall
                       WHERE key LIKE ? OR value LIKE ? OR context LIKE ? OR category LIKE ?
                       ORDER BY updated_at DESC LIMIT 20""",
                    (f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"),
                )

            if not results:
                return ToolResult(success=True, output="No memories found matching that search.")

            output = json.dumps(results, indent=2, default=str)
            return ToolResult(success=True, output=output)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
