"""Rook thin client — proxies MCP tool calls to the central hub.

Used by remote machines. Connects to the hub over Telesthete Band
(UDP on LAN, WebSocket through CF tunnel). Caches reads locally,
queues writes when offline.

The MCP server detects whether to run in local mode (hub is on this machine)
or client mode (connect to remote hub) based on config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

from .hub import (
    METHOD_LOOKUP, METHOD_INDEX, METHOD_PROJECT, METHOD_PROJECT_UPDATE,
    METHOD_LOG_CLI, METHOD_CACHE_WEB, METHOD_CLOUD_SEARCH, METHOD_CLOUD_READ,
    METHOD_REMEMBER, METHOD_RECALL, METHOD_STATS,
    _pack_rpc_request, _unpack_rpc_response,
)
from .ws_transport import WSTransportClient

log = logging.getLogger("rook.client")

CACHE_DIR = Path.home() / ".rook" / "cache"
CACHE_DB = CACHE_DIR / "local_cache.db"
WRITE_QUEUE_DB = CACHE_DIR / "write_queue.db"


def _init_cache() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(CACHE_DB))
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS lookup_cache (
            query TEXT PRIMARY KEY,
            result TEXT NOT NULL,
            cached_at REAL
        )
    """)
    db.commit()
    return db


def _init_write_queue() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(WRITE_QUEUE_DB))
    db.execute("""
        CREATE TABLE IF NOT EXISTS write_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            method INTEGER NOT NULL,
            payload TEXT NOT NULL,
            queued_at REAL,
            sent INTEGER DEFAULT 0
        )
    """)
    db.commit()
    return db


class RookClient:
    """Thin client that proxies graph operations to the hub."""

    def __init__(self, hub_url: str = "ws://localhost:7006/band",
                 cache_ttl: float = 300.0):
        self.hub_url = hub_url
        self.cache_ttl = cache_ttl

        self._transport = WSTransportClient(hub_url=hub_url)
        self._cache = _init_cache()
        self._write_queue = _init_write_queue()

        # Pending RPC requests: req_id -> Future
        self._pending: dict[int, asyncio.Future] = {}
        self._next_req_id = 1
        self._connected = False
        self._running = False

    async def start(self):
        """Connect to the hub."""
        self._running = True
        self._transport.start()

        # Register response handler — raw RPC bytes over WS
        self._transport.on_raw_message(self._handle_response_packet)

        asyncio.create_task(self._transport.run())
        asyncio.create_task(self._flush_write_queue())

        # Wait for connection
        for _ in range(50):  # 5 seconds
            if self._transport.connected:
                self._connected = True
                log.info("Connected to hub: %s", self.hub_url)
                return
            await asyncio.sleep(0.1)

        log.warning("Hub not reachable — running in offline/cache mode")

    async def stop(self):
        self._running = False
        await self._transport.stop()

    async def rpc(self, method: int, payload: dict, timeout: float = 10.0) -> Any:
        """Send an RPC request to the hub and wait for response."""
        req_id = self._next_req_id
        self._next_req_id += 1

        if not self._transport.connected:
            # Offline — check cache for reads, queue writes
            if method == METHOD_LOOKUP:
                cached = self._check_cache(payload.get("query", ""))
                if cached:
                    return cached
                return {"projects": [], "concepts": [], "sources": [],
                        "cli_history": [], "web_cache": [], "past_searches": [],
                        "_offline": True}
            elif method in (METHOD_INDEX, METHOD_PROJECT_UPDATE, METHOD_LOG_CLI,
                            METHOD_CACHE_WEB, METHOD_REMEMBER):
                self._queue_write(method, payload)
                return {"queued": True, "_offline": True}
            else:
                return {"error": "Hub offline and no cached data"}

        # Send request
        request_bytes = _pack_rpc_request(req_id, method, payload)
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        self._transport.send(self._transport.hub_addr, request_bytes)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            # Cache reads
            if method == METHOD_LOOKUP and "query" in payload:
                self._set_cache(payload["query"], result)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            # Fallback to cache
            if method == METHOD_LOOKUP:
                cached = self._check_cache(payload.get("query", ""))
                if cached:
                    return cached
            return {"error": "Hub request timed out"}

    def _handle_response_packet(self, peer_addr: tuple, packet_bytes: bytes):
        """Handle an RPC response from the hub."""
        try:
            # Try to parse as RPC response (4-byte req_id + JSON)
            if len(packet_bytes) < 5:
                return
            req_id, resp = _unpack_rpc_response(packet_bytes)
            future = self._pending.pop(req_id, None)
            if future and not future.done():
                future.set_result(resp.get("result", resp))
        except Exception as e:
            log.debug("Response parse error: %s", e)

    # ── Cache ────────────────────────────────────────────────────────────

    def _check_cache(self, query: str) -> dict | None:
        row = self._cache.execute(
            "SELECT result, cached_at FROM lookup_cache WHERE query = ?", (query,)
        ).fetchone()
        if row and (time.time() - row["cached_at"]) < self.cache_ttl:
            return json.loads(row["result"])
        return None

    def _set_cache(self, query: str, result: Any):
        self._cache.execute(
            "INSERT OR REPLACE INTO lookup_cache (query, result, cached_at) VALUES (?, ?, ?)",
            (query, json.dumps(result, default=str), time.time()),
        )
        self._cache.commit()

    # ── Write queue (offline mode) ───────────────────────────────────────

    def _queue_write(self, method: int, payload: dict):
        self._write_queue.execute(
            "INSERT INTO write_queue (method, payload, queued_at) VALUES (?, ?, ?)",
            (method, json.dumps(payload, default=str), time.time()),
        )
        self._write_queue.commit()
        log.info("Queued write (offline): method=%d", method)

    async def _flush_write_queue(self):
        """Periodically flush queued writes when hub comes online."""
        while self._running:
            await asyncio.sleep(10)
            if not self._transport.connected:
                continue

            rows = self._write_queue.execute(
                "SELECT id, method, payload FROM write_queue WHERE sent = 0 ORDER BY queued_at LIMIT 20"
            ).fetchall()

            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                    req_id = self._next_req_id
                    self._next_req_id += 1
                    request_bytes = _pack_rpc_request(req_id, row["method"], payload)
                    self._transport.send(self._transport.hub_addr, request_bytes)

                    self._write_queue.execute(
                        "UPDATE write_queue SET sent = 1 WHERE id = ?", (row["id"],)
                    )
                    log.info("Flushed queued write: method=%d", row["method"])
                except Exception as e:
                    log.error("Failed to flush write: %s", e)

            if rows:
                self._write_queue.commit()
                # Clean up old sent items
                self._write_queue.execute(
                    "DELETE FROM write_queue WHERE sent = 1 AND queued_at < ?",
                    (time.time() - 3600,),
                )
                self._write_queue.commit()


# ── Convenience functions for the MCP server ─────────────────────────────────

_client: RookClient | None = None


async def get_client(hub_url: str = "ws://localhost:7006/band") -> RookClient:
    global _client
    if _client is None:
        _client = RookClient(hub_url=hub_url)
        await _client.start()
    return _client


async def hub_lookup(query: str, max_hops: int = 2) -> dict:
    c = await get_client()
    return await c.rpc(METHOD_LOOKUP, {"query": query, "max_hops": max_hops})


async def hub_index(concepts: str, source_type: str, source_location: str, **kwargs) -> dict:
    c = await get_client()
    return await c.rpc(METHOD_INDEX, {
        "concepts": concepts, "source_type": source_type,
        "source_location": source_location, **kwargs,
    })


async def hub_project(project: str = "", limit: int = 5) -> Any:
    c = await get_client()
    return await c.rpc(METHOD_PROJECT, {"project": project, "limit": limit})


async def hub_project_update(project: str, summary: str, **kwargs) -> dict:
    c = await get_client()
    return await c.rpc(METHOD_PROJECT_UPDATE, {"project": project, "summary": summary, **kwargs})


async def hub_log_cli(commands: str, context: str, **kwargs) -> dict:
    c = await get_client()
    return await c.rpc(METHOD_LOG_CLI, {"commands": commands, "context": context, **kwargs})


async def hub_cache_web(query: str, url: str = "", summary: str = "") -> dict:
    c = await get_client()
    return await c.rpc(METHOD_CACHE_WEB, {"query": query, "url": url, "summary": summary})


async def hub_cloud_search(query: str, limit: int = 15) -> Any:
    c = await get_client()
    return await c.rpc(METHOD_CLOUD_SEARCH, {"query": query, "limit": limit})


async def hub_remember(key: str, value: str, category: str = "general") -> dict:
    c = await get_client()
    return await c.rpc(METHOD_REMEMBER, {"key": key, "value": value, "category": category})


async def hub_recall(query: str = "", category: str = "") -> Any:
    c = await get_client()
    return await c.rpc(METHOD_RECALL, {"query": query, "category": category})


async def hub_stats() -> dict:
    c = await get_client()
    return await c.rpc(METHOD_STATS, {})
