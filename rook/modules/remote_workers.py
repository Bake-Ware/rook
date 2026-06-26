"""Remote workers module — WebSocket server for worker connections + bootstrap HTTP."""

from __future__ import annotations

import logging
from typing import Any

MODULE_NAME = "remote_workers"
MODULE_DESCRIPTION = "Remote worker system with WebSocket connections and bootstrap server"
MODULE_TYPE = "channel"

log = logging.getLogger(__name__)

_server = None


async def start(agent: Any, config: Any) -> None:
    global _server
    _server = agent.tools.remote_server

    # Wire worker connect/disconnect/chat to agent
    _server._on_worker_connect = agent._on_worker_connect
    _server._on_worker_disconnect = agent._on_worker_disconnect
    _server._on_worker_chat = agent._on_worker_chat

    await _server.start()

    # Register channel senders on the bridge
    bridge = agent.tools.channel_bridge

    async def worker_send(worker_name: str, message: str) -> None:
        worker = _server.get_worker(worker_name)
        if worker:
            await worker.ws.send_json({"type": "chat_response", "content": message})

    bridge.register_sender("worker", worker_send)


async def stop() -> None:
    if _server:
        await _server.stop()
