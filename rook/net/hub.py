"""Rook Hub — central server that owns the knowledge graph.

Listens for Band connections (UDP on LAN, WebSocket through CF tunnel).
Handles RPC requests from thin MCP clients on remote machines.

Run: python -m rook.net.hub
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import sys
import time
from typing import Any

from telesthete import Band
from telesthete.protocol.framing import ChannelType

from .ws_transport import WSTransportServer
from ..cli.graph import RookGraph
from ..cli import cloud_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [rook-hub] %(message)s",
)
log = logging.getLogger("rook-hub")

# RPC stream IDs
STREAM_RPC = 1        # Request/response (priority 0)
STREAM_WRITE_ACK = 2  # Write queue acks (priority 1)
STREAM_SYNC = 3       # Bulk sync (priority 128)

# RPC message format:
# First 4 bytes: request ID (uint32)
# Next 1 byte: method code
# Rest: JSON payload

METHOD_LOOKUP = 0x01
METHOD_INDEX = 0x02
METHOD_PROJECT = 0x03
METHOD_PROJECT_UPDATE = 0x04
METHOD_LOG_CLI = 0x05
METHOD_CACHE_WEB = 0x06
METHOD_CLOUD_SEARCH = 0x07
METHOD_CLOUD_READ = 0x08
METHOD_REMEMBER = 0x09
METHOD_RECALL = 0x0A
METHOD_EXTRACT = 0x0B
METHOD_STATS = 0x0F

METHOD_NAMES = {
    METHOD_LOOKUP: "lookup",
    METHOD_INDEX: "index",
    METHOD_PROJECT: "project",
    METHOD_PROJECT_UPDATE: "project_update",
    METHOD_LOG_CLI: "log_cli",
    METHOD_CACHE_WEB: "cache_web",
    METHOD_CLOUD_SEARCH: "cloud_search",
    METHOD_CLOUD_READ: "cloud_read",
    METHOD_REMEMBER: "remember",
    METHOD_RECALL: "recall",
    METHOD_EXTRACT: "extract",
    METHOD_STATS: "stats",
}


def _pack_rpc_request(req_id: int, method: int, payload: dict) -> bytes:
    """Pack an RPC request into bytes."""
    data = json.dumps(payload, default=str).encode("utf-8")
    return struct.pack("!IB", req_id, method) + data


def _unpack_rpc_request(data: bytes) -> tuple[int, int, dict]:
    """Unpack an RPC request. Returns (req_id, method, payload)."""
    req_id, method = struct.unpack("!IB", data[:5])
    payload = json.loads(data[5:].decode("utf-8")) if len(data) > 5 else {}
    return req_id, method, payload


def _pack_rpc_response(req_id: int, result: Any, error: str = "") -> bytes:
    """Pack an RPC response into bytes."""
    resp = {"result": result} if not error else {"error": error}
    data = json.dumps(resp, default=str).encode("utf-8")
    return struct.pack("!I", req_id) + data


def _unpack_rpc_response(data: bytes) -> tuple[int, dict]:
    """Unpack an RPC response. Returns (req_id, response_dict)."""
    req_id = struct.unpack("!I", data[:4])[0]
    resp = json.loads(data[4:].decode("utf-8"))
    return req_id, resp


class RookHub:
    """Central Rook server — owns the graph, handles RPC from thin clients."""

    def __init__(self, psk: str = "rook-hub-default", udp_port: int = 9999,
                 ws_port: int = 7006):
        self.psk = psk
        self.udp_port = udp_port
        self.ws_port = ws_port

        # Graph (the single source of truth)
        self._graph = RookGraph()

        # Telesthete Band for LAN peers
        self._band = Band(psk=psk, hostname="rook-hub", bind_port=udp_port)

        # WebSocket transport for internet peers
        self._ws_transport = WSTransportServer(bind_port=ws_port)

        # RPC streams
        self._rpc_stream = None
        self._write_stream = None

        # Stats
        self._requests_handled = 0
        self._connected_peers = 0

    async def start(self):
        """Start the hub — Band + WS listener."""
        log.info("Starting Rook Hub (UDP:%d, WS:%d)", self.udp_port, self.ws_port)

        # Setup Band RPC stream
        self._rpc_stream = self._band.stream(stream_id=STREAM_RPC, priority=0)
        self._write_stream = self._band.stream(stream_id=STREAM_WRITE_ACK, priority=1)

        # Register RPC handler on Band
        self._rpc_stream.on_receive(self._handle_rpc_packet)

        # Register raw RPC handler on WS transport
        # WS clients send raw RPC bytes (not Band-framed) since TLS handles security
        self._ws_transport.on_raw_message(self._handle_ws_raw_message)

        # Start Band
        await self._band.start()

        # Start WS transport
        self._ws_transport.start()
        asyncio.create_task(self._ws_transport.run())

        log.info("Rook Hub running")
        log.info("  LAN: Band on UDP port %d (PSK: %s...)", self.udp_port, self.psk[:8])
        log.info("  WAN: WebSocket on port %d (/band)", self.ws_port)
        log.info("  Graph: %s", self._graph.stats())

    def _handle_ws_raw_message(self, peer_addr: tuple, packet_bytes: bytes):
        """Handle raw RPC message from WS client."""
        self._handle_rpc_raw(peer_addr, packet_bytes)

    def _handle_rpc_packet(self, data: bytes, peer_addr: tuple, timestamp: int):
        """Handle an RPC request from a Band stream."""
        asyncio.create_task(self._process_rpc(peer_addr, data, via="band"))

    def _handle_rpc_raw(self, peer_addr: tuple, data: bytes):
        """Handle raw RPC data (from WS, no Band framing)."""
        asyncio.create_task(self._process_rpc(peer_addr, data, via="ws"))

    async def _process_rpc(self, peer_addr: tuple, data: bytes, via: str = "band"):
        """Process an RPC request and send the response."""
        try:
            req_id, method, payload = _unpack_rpc_request(data)
        except Exception as e:
            log.error("Failed to unpack RPC: %s", e)
            return

        method_name = METHOD_NAMES.get(method, f"unknown({method})")
        log.debug("RPC [%s] %s from %s: %s", via, method_name, peer_addr, str(payload)[:100])

        try:
            result = await self._dispatch(method, payload)
            response = _pack_rpc_response(req_id, result)
        except Exception as e:
            log.error("RPC %s failed: %s", method_name, e)
            response = _pack_rpc_response(req_id, None, error=str(e))

        # Send response back
        if via == "band" and self._rpc_stream:
            self._rpc_stream.send(response)
        elif via == "ws":
            self._ws_transport.send(peer_addr, response)

        self._requests_handled += 1

    async def _dispatch(self, method: int, payload: dict) -> Any:
        """Dispatch an RPC method to the appropriate handler."""
        g = self._graph

        if method == METHOD_LOOKUP:
            return g.lookup(payload.get("query", ""), max_hops=payload.get("max_hops", 2))

        elif method == METHOD_INDEX:
            concepts = [c.strip() for c in payload.get("concepts", "").split(",") if c.strip()]
            if not concepts:
                return {"error": "No concepts"}
            sid = g.index_finding(
                concepts=concepts,
                source_type=payload.get("source_type", ""),
                source_location=payload.get("source_location", ""),
                source_title=payload.get("source_title", ""),
                project=payload.get("project", ""),
                turn_ids=payload.get("turn_ids", ""),
                weight=float(payload.get("weight", 1.0)),
            )
            return {"source_id": sid, "concepts": len(concepts)}

        elif method == METHOD_PROJECT:
            return g.get_project_status(
                project_id=payload.get("project", ""),
                limit=int(payload.get("limit", 5)),
            )

        elif method == METHOD_PROJECT_UPDATE:
            pid = g.index_project(payload["project"],
                                  status=payload.get("status", "active"))
            if payload.get("status"):
                g._run_graph_write(
                    f"MATCH (p:Project {{id: '{pid}'}}) SET p.status = '{payload['status']}'"
                )
            g.add_project_event(
                pid, payload["summary"],
                event_type=payload.get("event_type", "update"),
                details=payload.get("details", ""),
                source_id=payload.get("source_location", ""),
            )
            return {"project": pid, "event": payload["summary"]}

        elif method == METHOD_LOG_CLI:
            g.log_cli(
                payload.get("commands", ""),
                payload.get("context", ""),
                payload.get("outcome", ""),
                payload.get("resolution", ""),
                payload.get("cost_hint", "low"),
            )
            return {"logged": True}

        elif method == METHOD_CACHE_WEB:
            g.cache_web(
                payload.get("query", ""),
                payload.get("url", ""),
                payload.get("summary", ""),
            )
            return {"cached": True}

        elif method == METHOD_CLOUD_SEARCH:
            return cloud_sync.search(payload.get("query", ""), limit=int(payload.get("limit", 15)))

        elif method == METHOD_CLOUD_READ:
            return cloud_sync.read_conversation_local(payload.get("conversation_id", ""))

        elif method == METHOD_REMEMBER:
            import sqlite3
            from pathlib import Path
            db_path = Path.home() / ".rook" / "shared_memory.db"
            db = sqlite3.connect(str(db_path))
            now = time.time()
            db.execute(
                """INSERT OR REPLACE INTO shared_memory (key, value, category, created_at, updated_at, source_session)
                   VALUES (?, ?, ?, COALESCE((SELECT created_at FROM shared_memory WHERE key = ?), ?), ?, ?)""",
                (payload["key"], payload["value"], payload.get("category", "general"),
                 payload["key"], now, now, "hub"),
            )
            db.commit()
            return {"stored": payload["key"]}

        elif method == METHOD_RECALL:
            import sqlite3
            from pathlib import Path
            db_path = Path.home() / ".rook" / "shared_memory.db"
            if not db_path.exists():
                return []
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            q = payload.get("query", "")
            if q:
                rows = db.execute(
                    "SELECT * FROM shared_memory WHERE key LIKE ? OR value LIKE ? ORDER BY updated_at DESC",
                    (f"%{q}%", f"%{q}%"),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM shared_memory ORDER BY updated_at DESC LIMIT 50").fetchall()
            return [dict(r) for r in rows]

        elif method == METHOD_STATS:
            return g.stats()

        else:
            return {"error": f"Unknown method: {method}"}

    async def run_forever(self):
        """Start and run until interrupted."""
        await self.start()
        try:
            while True:
                await asyncio.sleep(30)
                log.info("Hub stats: %d requests handled, graph: %s",
                         self._requests_handled, self._graph.stats())
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Hub shutting down")
        finally:
            await self._band.stop()
            await self._ws_transport.stop()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Rook Hub — central knowledge graph server")
    parser.add_argument("--psk", default="rook-hub-2026", help="Pre-shared key for Band encryption")
    parser.add_argument("--udp-port", type=int, default=9999, help="UDP port for LAN Band peers")
    parser.add_argument("--ws-port", type=int, default=7006, help="WebSocket port for internet peers")
    args = parser.parse_args()

    hub = RookHub(psk=args.psk, udp_port=args.udp_port, ws_port=args.ws_port)
    asyncio.run(hub.run_forever())


if __name__ == "__main__":
    main()
