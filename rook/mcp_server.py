"""Rook MCP Server — gives every Claude Code session access to the Rook network.

Tools:
  Local CC sessions:    rook_sessions, rook_read_session, rook_spawn, rook_output, rook_list, rook_kill
  Cloud (claude.ai):    rook_cloud_search, rook_cloud_conversations, rook_cloud_read, rook_cloud_projects, rook_cloud_docs
  Shared memory:        rook_remember, rook_recall

Run: python -m rook.mcp_server
Register: claude mcp add --scope user rook -- python -m rook.mcp_server
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .cli.cc_history import scan_history, search_sessions, read_session, SessionInfo
from .cli.cc_tmux import SessionManager, render_stream_json
from .cli import cloud_sync
from .cli.graph import RookGraph
from .cli import extractor
from .net.config import load_config, is_client

# MCP servers must only use stderr for logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [rook-mcp] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("rook-mcp")

mcp = FastMCP(
    "rook",
    instructions="""Rook is a knowledge graph and session network shared across all Claude Code sessions.

WORKFLOW — always follow this order:
1. BEFORE researching: call rook_lookup to check what's already known
2. AFTER finding something: call rook_index to save it for future sessions
3. AFTER web searches: call rook_cache_web so we don't re-search
4. AFTER multi-step CLI work: call rook_log_cli to save the resolution
5. WHEN progress is made on a project: call rook_project_update

SEARCH PRIORITY: rook_lookup (graph) → rook_cloud_search (claude.ai FTS) → rook_sessions (local CC) → grep/web

The more expensive a finding was to discover, the more important it is to index it.""",
)

# Shared state — initialized lazily
_session_mgr: SessionManager | None = None
_history_cache: dict | None = None
_history_cache_ts: float = 0

MEMORY_DB = Path.home() / ".rook" / "shared_memory.db"

# Network mode detection
_net_config = load_config()
_CLIENT_MODE = _net_config["mode"] == "client"
_HUB_URL = _net_config.get("hub_url", "ws://localhost:7006/band")

if _CLIENT_MODE:
    log.info("Running in CLIENT mode — proxying to hub at %s", _HUB_URL)
else:
    log.info("Running in LOCAL mode — graph on this machine")

# Graph — initialized lazily (local mode only)
_graph: RookGraph | None = None

def _get_graph() -> RookGraph:
    global _graph
    if _graph is None:
        _graph = RookGraph()
    return _graph

# Client — initialized lazily (client mode only)
_hub_client = None

async def _get_client():
    global _hub_client
    if _hub_client is None:
        from .net.client import RookClient
        _hub_client = RookClient(hub_url=_HUB_URL)
        await _hub_client.start()
    return _hub_client


def _mgr() -> SessionManager:
    global _session_mgr
    if _session_mgr is None:
        _session_mgr = SessionManager()
    return _session_mgr


def _get_history(max_age: float = 30.0) -> dict[str, list[SessionInfo]]:
    """Get cached session history, refreshing if stale."""
    global _history_cache, _history_cache_ts
    if _history_cache is None or (time.time() - _history_cache_ts) > max_age:
        _history_cache = scan_history()
        _history_cache_ts = time.time()
    return _history_cache


def _memory_db() -> sqlite3.Connection:
    """Get shared memory database connection."""
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(MEMORY_DB))
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS shared_memory (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            created_at REAL,
            updated_at REAL,
            source_session TEXT
        )
    """)
    db.commit()
    return db


# ── Session History Tools ────────────────────────────────────────────────────

@mcp.tool()
async def rook_sessions(query: str = "", project: str = "", limit: int = 20) -> str:
    """Search local Claude Code session history on this machine.

    NOTE: Try rook_lookup first (graph index) and rook_cloud_search (claude.ai history)
    before falling back to this. This searches raw local CC session files.

    Args:
        query: Search term to find in conversations (searches prompts and full text).
               Leave empty to list all projects.
        project: Filter by project path substring (e.g. "DROGA", "rook", "source").
        limit: Max results to return.
    """
    by_project = _get_history()

    if project:
        by_project = {k: v for k, v in by_project.items()
                      if project.lower() in k.lower()}

    if query:
        results = search_sessions(query, by_project)
        lines = [f"Found {len(results)} sessions matching '{query}':\n"]
        for session, matches in results[:limit]:
            lines.append(f"  [{session.session_id[:8]}] {session.project}")
            lines.append(f"    Last active: {_fmt_ts(session.last_ts)} | {session.message_count} messages")
            for m in matches[:3]:
                lines.append(f"    → {m}")
            lines.append("")
        return "\n".join(lines)

    # No query — list projects with session counts
    lines = [f"CC sessions across {len(by_project)} projects:\n"]
    sorted_projects = sorted(by_project.items(),
                             key=lambda x: max(s.last_ts for s in x[1]) if x[1] else 0,
                             reverse=True)
    for proj, sessions in sorted_projects[:limit]:
        total_msgs = sum(s.message_count for s in sessions)
        latest = _fmt_ts(max(s.last_ts for s in sessions))
        lines.append(f"  {proj}")
        lines.append(f"    {len(sessions)} sessions, {total_msgs} msgs, last: {latest}")
    return "\n".join(lines)


@mcp.tool()
async def rook_read_session(session_id: str, max_messages: int = 50) -> str:
    """Read the full conversation from a specific CC session.

    Args:
        session_id: Session ID prefix (at least 8 chars). Get these from rook_sessions.
        max_messages: Maximum messages to return.
    """
    by_project = _get_history()

    # Find matching session
    for sessions in by_project.values():
        for s in sessions:
            if s.session_id.startswith(session_id):
                messages = read_session(s, max_messages=max_messages)
                if not messages:
                    return f"Session {session_id} found but has no readable messages."

                lines = [f"Session {s.session_id[:8]} in {s.project}",
                         f"{len(messages)} messages:\n"]
                for msg in messages:
                    role = "you" if msg["role"] == "user" else "cc"
                    content = msg["content"]
                    if len(content) > 500:
                        content = content[:500] + f"... ({len(content)} chars)"
                    lines.append(f"[{role}] {content}\n")
                return "\n".join(lines)

    return f"No session found matching '{session_id}'."


# ── Session Management Tools ─────────────────────────────────────────────────

@mcp.tool()
async def rook_spawn(prompt: str, cwd: str = "") -> str:
    """Spawn a new Claude Code session in the background.

    The session runs independently and its output is captured. Use rook_output
    to check on it later, or rook_list to see all sessions.

    Args:
        prompt: The prompt/instruction for the new CC session.
        cwd: Working directory for the session. Defaults to current directory.
    """
    mgr = _mgr()
    short = await mgr.spawn(prompt, cwd=cwd or None, print_output=False)
    session = mgr.get_session(short)

    if session and session["status"] == "error":
        return f"Failed to spawn: {session.get('last_output', 'unknown error')}"

    # Wait briefly for it to start producing output
    import asyncio
    await asyncio.sleep(2)

    session = mgr.get_session(short)
    status = session["status"] if session else "unknown"
    pid = session.get("pid", "?") if session else "?"

    return f"Spawned session {short} (pid={pid}, status={status})\nUse rook_output('{short}') to read output, rook_list() to see all sessions."


@mcp.tool()
async def rook_output(session_id: str, lines: int = 50) -> str:
    """Read output from a managed CC session.

    Args:
        session_id: Short session ID (6 chars, from rook_spawn or rook_list).
        lines: Number of recent lines to return.
    """
    mgr = _mgr()
    raw = mgr.read_output(session_id, tail=lines)
    if not raw:
        session = mgr.get_session(session_id)
        if not session:
            return f"Session '{session_id}' not found."
        return f"Session '{session_id}' exists (status={session['status']}) but has no output yet."

    # Render stream-json to readable text
    text_parts = []
    for line in raw.splitlines():
        text = render_stream_json(line, print_it=False)
        if text:
            text_parts.append(text)

    return "".join(text_parts) or raw


@mcp.tool()
async def rook_list() -> str:
    """List all managed CC sessions (running and completed)."""
    mgr = _mgr()
    mgr.cleanup_dead()
    sessions = mgr.list_sessions()

    if not sessions:
        return "No managed sessions."

    lines = []
    for s in sessions[:20]:
        status = s["status"]
        prompt = s["prompt"][:60]
        ts = _fmt_ts(s["started_at"] * 1000)
        last = (s.get("last_output") or "")[:80]
        lines.append(f"  [{s['short_id']}] {status:10s} {ts}  {s['cwd']}")
        lines.append(f"           prompt: {prompt}")
        if last:
            lines.append(f"           output: {last}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def rook_kill(session_id: str) -> str:
    """Kill a running CC session.

    Args:
        session_id: Short session ID (6 chars).
    """
    mgr = _mgr()
    killed = await mgr.kill_session(session_id)
    return f"Killed session {session_id}." if killed else f"Session '{session_id}' not found or already dead."


# ── Shared Memory Tools ──────────────────────────────────────────────────────

@mcp.tool()
async def rook_remember(key: str, value: str, category: str = "general") -> str:
    """Store a key-value fact in shared memory accessible by ALL CC sessions.

    For structured knowledge (concepts, sources, projects), prefer rook_index and
    rook_project_update. Use rook_remember for simple facts, configs, and decisions
    that don't need graph relationships.

    Args:
        key: Short identifier for this memory (e.g. "droga_best_lr", "starscream_ip").
        value: The information to store. Be detailed — other sessions need full context.
        category: One of: config, credential, finding, decision, general.
    """
    if _CLIENT_MODE:
        from .net.hub import METHOD_REMEMBER
        client = await _get_client()
        await client.rpc(METHOD_REMEMBER, {"key": key, "value": value, "category": category})
        return f"Stored '{key}' → hub ({category})."

    db = _memory_db()
    now = time.time()
    db.execute(
        """INSERT OR REPLACE INTO shared_memory (key, value, category, created_at, updated_at, source_session)
           VALUES (?, ?, ?, COALESCE((SELECT created_at FROM shared_memory WHERE key = ?), ?), ?, ?)""",
        (key, value, category, key, now, now, "mcp"),
    )
    db.commit()
    return f"Stored '{key}' ({category}). All CC sessions can now access this via rook_recall."


@mcp.tool()
async def rook_recall(query: str = "", category: str = "") -> str:
    """Search shared memory for facts stored by any CC session.

    Args:
        query: Search term (matches key and value). Leave empty to list all.
        category: Filter by category (config, credential, finding, decision, general).
    """
    if _CLIENT_MODE:
        from .net.hub import METHOD_RECALL
        client = await _get_client()
        rows = await client.rpc(METHOD_RECALL, {"query": query, "category": category})
        if not rows or (isinstance(rows, list) and not rows):
            return "No matching memories." if query else "Shared memory is empty."
        if isinstance(rows, list):
            lines = [f"Found {len(rows)} memories:\n"]
            for r in rows:
                lines.append(f"  [{r.get('key','')}] ({r.get('category','')}) — {r.get('value','')[:200]}")
            return "\n".join(lines)
        return str(rows)

    db = _memory_db()
    if query:
        rows = db.execute("SELECT * FROM shared_memory WHERE key LIKE ? OR value LIKE ? ORDER BY updated_at DESC",
                          (f"%{query}%", f"%{query}%")).fetchall()
    elif category:
        rows = db.execute("SELECT * FROM shared_memory WHERE category = ? ORDER BY updated_at DESC",
                          (category,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM shared_memory ORDER BY updated_at DESC LIMIT 50").fetchall()

    if not rows:
        return "No matching memories found." if query else "Shared memory is empty."
    lines = [f"Found {len(rows)} memories:\n"]
    for r in rows:
        age = _fmt_age(r["updated_at"])
        lines.append(f"  [{r['key']}] ({r['category']}) — {age}")
        lines.append(f"    {r['value'][:200]}")
        lines.append("")
    return "\n".join(lines)


# ── Cloud (claude.ai) Tools ──────────────────────────────────────────────────

@mcp.tool()
async def rook_cloud_search(query: str, limit: int = 15) -> str:
    """Full-text search across synced Claude Desktop/web conversations AND project documents.

    NOTE: Try rook_lookup first — it checks the knowledge graph index which is faster
    and returns structured context with related concepts. Use this for broader text search
    when rook_lookup doesn't have what you need.

    Searches the local cache (synced from claude.ai). Covers 500+ conversations
    and all project documents. No API calls — instant local FTS search.

    Args:
        query: Search term (supports FTS5 syntax: AND, OR, NOT, "exact phrase").
        limit: Max results to return.
    """
    status = cloud_sync.get_sync_status()
    if not status.get("synced"):
        return "Cloud data not synced yet. Run: python -m rook.cli.cloud_sync sync"

    results = cloud_sync.search(query, limit=limit)
    if not results:
        return f"No results for '{query}' across {status['conversations']} conversations and {status['docs']} docs."

    lines = [f"Found {len(results)} results for '{query}':\n"]
    for r in results:
        if r["source"] == "conversation":
            lines.append(f"  [conversation] {r['convo_name']}")
            lines.append(f"    {r['role']}: {r['snippet']}")
        else:
            lines.append(f"  [document] {r['project_name']}/{r['file_name']}")
            lines.append(f"    {r['snippet']}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def rook_cloud_conversations(query: str = "", limit: int = 30) -> str:
    """List synced Claude Desktop/web conversations, optionally filtered by name.

    Args:
        query: Filter by conversation name (substring match). Empty = list all.
        limit: Max results.
    """
    convos = cloud_sync.list_conversations_local(limit=limit, query=query)
    if not convos:
        return "No synced conversations." if not query else f"No conversations matching '{query}'."

    lines = [f"{len(convos)} conversations" + (f" matching '{query}'" if query else "") + ":\n"]
    for c in convos:
        star = " *" if c.get("is_starred") else ""
        lines.append(f"  [{c['uuid'][:8]}] {c['updated_at'][:10]}  {c['turn_count']:>3} turns  {c['name']}{star}")
    return "\n".join(lines)


@mcp.tool()
async def rook_cloud_read(conversation_id: str, limit: int = 100) -> str:
    """Read a synced Claude Desktop/web conversation by UUID prefix.

    Args:
        conversation_id: UUID prefix (at least 8 chars). Get from rook_cloud_conversations or rook_cloud_search.
        limit: Max turns to return.
    """
    result = cloud_sync.read_conversation_local(conversation_id, limit=limit)
    if not result:
        return f"No conversation found matching '{conversation_id}'."

    convo = result["conversation"]
    turns = result["turns"]
    lines = [f"{convo['name']} ({convo['model']})", f"  {convo['created_at'][:10]} — {len(turns)} turns\n"]

    for t in turns:
        role = "you" if t["role"] == "human" else "claude"
        content = t["content"]
        if len(content) > 600:
            content = content[:600] + f"... ({len(content)} chars)"
        lines.append(f"[{role}] {content}\n")

    return "\n".join(lines)


@mcp.tool()
async def rook_cloud_projects() -> str:
    """List all synced Claude.ai projects with their document and conversation counts."""
    projects = cloud_sync.list_projects_local()
    if not projects:
        return "No synced projects."

    lines = ["Synced projects:\n"]
    for p in projects:
        lines.append(f"  [{p['uuid'][:8]}] {p['name']}")
        lines.append(f"    {p['doc_count']} docs, {p['convo_count']} conversations")
        if p.get("description"):
            lines.append(f"    {p['description'][:100]}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def rook_cloud_docs(project: str = "") -> str:
    """List synced project documents. Files are also on disk at ~/.rook/cloud/docs/.

    Args:
        project: Project UUID prefix to filter. Empty = all projects.
    """
    docs = cloud_sync.list_docs_local(project_uuid=project)
    if not docs:
        return "No synced docs." if not project else f"No docs for project '{project}'."

    lines = [f"{len(docs)} documents:\n"]
    for d in docs:
        tokens = f"{d['estimated_tokens']}t" if d.get("estimated_tokens") else ""
        lines.append(f"  [{d['project_name']}] {d['file_name']} {tokens}")
        lines.append(f"    {d['local_path']}")
    return "\n".join(lines)


# ── Knowledge Graph Tools ────────────────────────────────────────────────────

def _format_lookup(results: dict) -> str:
    """Format lookup results into readable text. Shared by local and client mode."""
    lines = []

    if results.get("projects"):
        lines.append("PROJECTS:")
        for p in results["projects"]:
            lines.append(f"  [{p.get('id','')}] {p.get('name','')} — {p.get('status','')}")
            if p.get("description"):
                lines.append(f"    {p['description'][:150]}")
            for e in (p.get("recent_events") or [])[:5]:
                ts = _fmt_age(e["timestamp"]) if e.get("timestamp") else "?"
                lines.append(f"    {e.get('event_type','')}: {e.get('summary','')} ({ts})")
        lines.append("")

    if results.get("concepts"):
        lines.append("CONCEPTS:")
        for c in results["concepts"]:
            via = f" (via {c['via_relation']})" if c.get("via_relation") else ""
            lines.append(f"  [{c.get('id','')}] {c.get('name','')} ({c.get('category','')}){via}")
        lines.append("")

    if results.get("sources"):
        lines.append("SOURCES:")
        for s in results["sources"]:
            via = f" (via {s['via_concept']})" if s.get("via_concept") else ""
            turns = f" turns:[{s['turn_ids']}]" if s.get("turn_ids") else ""
            lines.append(f"  [{s.get('type','')}] {s.get('location','')}{turns}{via}")
            if s.get("title"):
                lines.append(f"    {s['title']}")
        lines.append("")

    if results.get("cli_history"):
        lines.append("CLI HISTORY:")
        for h in results["cli_history"]:
            lines.append(f"  {h.get('context','')[:80]}")
            if h.get("resolution"):
                lines.append(f"    Resolution: {h['resolution'][:150]}")
            else:
                lines.append(f"    Commands: {h.get('commands','')[:150]}")
        lines.append("")

    if results.get("web_cache"):
        lines.append("PAST WEB SEARCHES:")
        for w in results["web_cache"]:
            ts = _fmt_age(w["timestamp"]) if w.get("timestamp") else "?"
            lines.append(f"  \"{w.get('query','')}\" ({ts})")
            if w.get("summary"):
                lines.append(f"    {w['summary'][:150]}")
        lines.append("")

    if results.get("past_searches"):
        lines.append("PAST ROOK SEARCHES:")
        for s in results["past_searches"]:
            lines.append(f"  \"{s.get('query','')}\" → {s.get('result_count',0)} results ({s.get('source','')})")
        lines.append("")

    return "\n".join(lines) if lines else ""


@mcp.tool()
async def rook_lookup(query: str, max_hops: int = 2) -> str:
    """Look up what Rook knows about a topic BEFORE doing any research.

    Cascades through the knowledge graph: finds matching concepts and projects,
    then follows edges to related sources, concepts, CLI history, past web
    searches, and cached results. 2-hop cascade by default.

    Call this FIRST when starting work on any topic. It may already have the
    answer or know exactly where to find it, saving significant research time.

    Args:
        query: Topic, keyword, project name, or concept to look up.
        max_hops: How many relationship hops to follow (1-3). Default 2.
    """
    if _CLIENT_MODE:
        from .net.hub import METHOD_LOOKUP
        client = await _get_client()
        results = await client.rpc(METHOD_LOOKUP, {"query": query, "max_hops": max_hops})
        if isinstance(results, dict) and results.get("error"):
            return results["error"]
        body = _format_lookup(results) if isinstance(results, dict) else str(results)
        offline = " (from cache)" if isinstance(results, dict) and results.get("_offline") else ""
        return (body or f"No existing knowledge about '{query}'.") + offline

    g = _get_graph()
    results = g.lookup(query, max_hops=max_hops)
    body = _format_lookup(results)
    if not body:
        return f"No existing knowledge about '{query}'. This is a new topic."
    stats = g.stats()
    header = f"Knowledge graph: {stats['concepts']} concepts, {stats['projects']} projects, {stats['sources']} sources\n\n"
    return header + body


@mcp.tool()
async def rook_index(concepts: str, source_type: str, source_location: str,
                     source_title: str = "", project: str = "",
                     turn_ids: str = "", weight: float = 1.0) -> str:
    """Index a finding in the knowledge graph. Call this AFTER discovering something.

    Creates concept nodes, links them to the source, and optionally links to a project.
    Concepts that co-occur in the same finding are automatically linked as related.

    The more expensive the finding was to discover (long research, many tool calls),
    the higher the weight should be — this prioritizes expensive knowledge in future lookups.

    Args:
        concepts: Comma-separated keywords/concepts (e.g. "orthogonal_transforms,mode_collapse,droga").
        source_type: One of: conversation, file, url, machine, cc_session.
        source_location: UUID, file path, URL, or machine name.
        source_title: Human-readable title for the source.
        project: Project name to associate with (e.g. "droga"). Optional.
        turn_ids: Specific turn indices within a conversation (e.g. "3,4,5"). Optional.
        weight: Importance weight 0.1-5.0. Higher = more expensive to rediscover. Default 1.0.
    """
    if _CLIENT_MODE:
        from .net.hub import METHOD_INDEX
        client = await _get_client()
        result = await client.rpc(METHOD_INDEX, {
            "concepts": concepts, "source_type": source_type,
            "source_location": source_location, "source_title": source_title,
            "project": project, "turn_ids": turn_ids, "weight": weight,
        })
        return f"Indexed → hub. {result}" if isinstance(result, dict) else str(result)

    g = _get_graph()
    concept_list = [c.strip() for c in concepts.split(",") if c.strip()]
    if not concept_list:
        return "No concepts provided."
    sid = g.index_finding(concepts=concept_list, source_type=source_type,
                          source_location=source_location, source_title=source_title,
                          project=project, turn_ids=turn_ids, weight=weight)
    return f"Indexed {len(concept_list)} concepts → {source_type}:{source_location[:40]}. Weight: {weight}"


@mcp.tool()
async def rook_project(project: str = "", limit: int = 5) -> str:
    """Get project status and recent activity. Call with no args to see all active projects.

    Args:
        project: Project name or ID to filter. Empty = all projects sorted by recent activity.
        limit: Max projects to return.
    """
    if _CLIENT_MODE:
        from .net.hub import METHOD_PROJECT
        client = await _get_client()
        projects = await client.rpc(METHOD_PROJECT, {"project": project, "limit": limit})
        if isinstance(projects, list) and projects:
            lines = []
            for p in projects:
                lines.append(f"[{p.get('id','')}] {p.get('name','')} — {p.get('status','')}")
                for e in (p.get("recent_events") or [])[:7]:
                    lines.append(f"  [{e.get('event_type','')}] {e.get('summary','')}")
                lines.append("")
            return "\n".join(lines)
        return f"No project '{project}' found." if project else "No projects tracked yet."

    g = _get_graph()
    projects = g.get_project_status(project_id=project, limit=limit)
    if not projects:
        return f"No project '{project}' found. Create one with rook_project_update." if project else "No projects tracked yet."

    lines = []
    for p in projects:
        lines.append(f"[{p['id']}] {p['name']} — {p['status']}")
        if p.get("description"):
            lines.append(f"  {p['description'][:200]}")
        events = p.get("recent_events", [])
        if events:
            lines.append(f"  Recent ({len(events)} events):")
            for e in events[:7]:
                ts = _fmt_age(e["timestamp"]) if e.get("timestamp") else "?"
                lines.append(f"    [{e['event_type']}] {e['summary']} — {ts}")
        else:
            lines.append("  No events logged yet.")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def rook_project_update(project: str, summary: str, event_type: str = "update",
                              status: str = "", details: str = "",
                              source_location: str = "") -> str:
    """Update a project's status or log a milestone/finding/decision.

    Call this when progress is made, a decision is reached, a phase completes,
    or a finding changes the direction of work. Claude should proactively call
    this when it infers meaningful progress.

    Args:
        project: Project name (e.g. "droga", "rook", "messy_boi").
        summary: One-line summary of what happened (e.g. "Run 7 disproved orthogonal transform benefits").
        event_type: One of: milestone, finding, decision, blocker, update, phase_change.
        status: Update project status: active, paused, done, blocked. Empty = no change.
        details: Extended details if needed.
        source_location: Link to relevant source (conversation UUID, file path, etc.).
    """
    if _CLIENT_MODE:
        from .net.hub import METHOD_PROJECT_UPDATE
        client = await _get_client()
        await client.rpc(METHOD_PROJECT_UPDATE, {
            "project": project, "summary": summary, "event_type": event_type,
            "status": status, "details": details, "source_location": source_location,
        })
        return f"Project '{project}' updated → hub: [{event_type}] {summary}"

    g = _get_graph()
    pid = g.index_project(project, status=status if status else "active")
    if status:
        g._run_graph_write(f"MATCH (p:Project {{id: '{_escape(pid)}'}}) SET p.status = '{_escape(status)}'")
    g.add_project_event(pid, summary, event_type=event_type, details=details, source_id=source_location)
    return f"Project '{project}' updated: [{event_type}] {summary}" + (f" (status → {status})" if status else "")


@mcp.tool()
async def rook_log_cli(commands: str, context: str, outcome: str = "",
                       resolution: str = "", cost_hint: str = "low") -> str:
    """Log a CLI command sequence for future reuse.

    Call this after completing a multi-step CLI operation, especially
    troubleshooting sequences. Next time the same problem comes up,
    rook_lookup will return the resolution directly.

    Args:
        commands: The command(s) that were run (can be multi-line).
        context: What problem was being solved (e.g. "dpkg lock on starscream").
        outcome: What happened (e.g. "fixed", "failed", "partial").
        resolution: The key insight or fix (e.g. "kill apt process, rm lock file, apt fix-install").
        cost_hint: How expensive this was: low (1-2 cmds), medium (5-10), high (10+), critical (required multiple compactions).
    """
    if _CLIENT_MODE:
        from .net.hub import METHOD_LOG_CLI
        client = await _get_client()
        await client.rpc(METHOD_LOG_CLI, {
            "commands": commands, "context": context, "outcome": outcome,
            "resolution": resolution, "cost_hint": cost_hint,
        })
        return f"CLI sequence logged → hub: {context[:60]} [{cost_hint}]"

    g = _get_graph()
    g.log_cli(commands, context, outcome, resolution, cost_hint)
    return f"CLI sequence logged: {context[:60]} [{cost_hint}]"


@mcp.tool()
async def rook_cache_web(query: str, url: str = "", summary: str = "") -> str:
    """Cache a web search result so we don't search for the same thing again.

    Call this after performing a web search. Future rook_lookup calls will
    return the cached result instead of re-searching.

    Args:
        query: The search query that was used.
        url: Key URL from the results.
        summary: Brief summary of what was found.
    """
    if _CLIENT_MODE:
        from .net.hub import METHOD_CACHE_WEB
        client = await _get_client()
        await client.rpc(METHOD_CACHE_WEB, {"query": query, "url": url, "summary": summary})
        return f"Cached → hub: \"{query}\""

    g = _get_graph()
    g.cache_web(query, url, summary)
    return f"Cached: \"{query}\" → {url[:50] if url else '(no url)'}"


def _escape(s: str) -> str:
    """Escape string for Cypher."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


# ── Extraction Tool ──────────────────────────────────────────────────────────

@mcp.tool()
async def rook_extract(limit: int = 50, force: bool = False, model: str = "",
                       conversation_id: str = "") -> str:
    """Run concept extraction on synced conversations using the LOCAL model (no quota).

    Uses the 9B model on LM Studio (localhost:1234) to extract keywords, concepts,
    and project associations from conversations, then indexes them into the knowledge graph.

    Call after rook_cloud_sync to index new conversations, or periodically to keep
    the graph fresh. Delta-aware — skips already-extracted conversations unless force=True.

    Args:
        limit: Max conversations to process in this batch.
        force: Re-extract already processed conversations.
        model: LM Studio model name. Default: qwen3.5-9b.
        conversation_id: Extract a single conversation by UUID prefix. Overrides limit/force.
    """
    if conversation_id:
        result = extractor.extract_single(
            conversation_id,
            model=model or extractor.DEFAULT_MODEL,
        )
        if "error" in result:
            return result["error"]
        lines = [f"Extracted from: {result['conversation']}"]
        lines.append(f"  Concepts: {', '.join(result['concepts'])}")
        if result.get("project"):
            lines.append(f"  Project: {result['project']}")
        if result.get("summary"):
            lines.append(f"  Summary: {result['summary']}")
        return "\n".join(lines)

    result = extractor.extract_batch(
        limit=limit,
        force=force,
        model=model or extractor.DEFAULT_MODEL,
    )
    if "error" in result:
        return result["error"]

    lines = [f"Extraction complete ({result['model']}):\n"]
    lines.append(f"  Processed: {result['extracted']}/{result['total']} conversations")
    lines.append(f"  Skipped: {result['skipped']} (too short)")
    lines.append(f"  Errors: {result['errors']}")
    lines.append(f"  Concepts extracted: {result['concepts_total']}")
    if result.get("graph"):
        g = result["graph"]
        lines.append(f"\n  Graph totals: {g['concepts']} concepts, {g['projects']} projects, {g['sources']} sources, {g['mentions']} edges")
    return "\n".join(lines)


# ── Cloud Sync Tool ──────────────────────────────────────────────────────────

@mcp.tool()
async def rook_cloud_sync(full: bool = False) -> str:
    """Sync Claude Desktop/web conversations and project docs from claude.ai.

    Uses the session cookie from Claude Desktop to authenticate. Delta sync —
    only fetches conversations where updated_at has changed since last sync.

    If this fails with a 403 (Cloudflare), the fallback is to run the sync
    manually from a CC session that has Chrome MCP access. Instructions will
    be provided in the error message.

    Args:
        full: Force re-sync of all conversations, ignoring delta.
    """
    status = cloud_sync.get_sync_status()
    last = status.get("last_sync")
    if last:
        age_h = (time.time() - last) / 3600
        existing = f"Last sync: {age_h:.1f}h ago ({status['conversations']} convos, {status['docs']} docs). "
    else:
        existing = "No previous sync. "

    try:
        result = cloud_sync.sync(full=full)
    except Exception as e:
        error_str = str(e)
        if "403" in error_str or "Forbidden" in error_str:
            return (
                f"{existing}Sync failed — Cloudflare blocked the request (403).\n\n"
                "Fallback: In a CC session with Chrome MCP, navigate to claude.ai and run:\n"
                "  1. Navigate to https://claude.ai\n"
                "  2. Use javascript_tool to run fetch() calls against the API\n"
                "  3. Download the JSON and import with: python -m rook.cli.cloud_sync import <file>\n\n"
                "Or just wait and retry — the cookie rotates and usually works again within minutes."
            )
        return f"{existing}Sync failed: {e}"

    if "error" in result:
        return f"{existing}{result['error']}"

    # Format stats
    lines = [f"{existing}Sync complete:\n"]
    if "conversations" in result:
        c = result["conversations"]
        lines.append(f"  Conversations: {c.get('new',0)} new, {c.get('updated',0)} updated, {c.get('unchanged',0)} unchanged")
    if "projects" in result:
        p = result["projects"]
        lines.append(f"  Projects: {p.get('projects',0)}")
        lines.append(f"  Docs: {p.get('docs_new',0)} new, {p.get('docs_updated',0)} updated, {p.get('docs_unchanged',0)} unchanged")

    new_status = cloud_sync.get_sync_status()
    lines.append(f"\n  Total: {new_status['conversations']} conversations, {new_status['turns']} turns, {new_status['docs']} docs")
    lines.append(f"  Docs on disk: {new_status['docs_path']}")
    return "\n".join(lines)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_ts(ts: float) -> str:
    if ts > 1e12:
        ts = ts / 1000
    try:
        from datetime import datetime
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "?"


def _fmt_age(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.0f}h ago"
    return f"{delta / 86400:.0f}d ago"


def main():
    log.info("Rook MCP server starting")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
