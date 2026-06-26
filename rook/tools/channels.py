"""Cross-channel communication tools — send messages to any channel from anywhere."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Awaitable

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)


class ChannelBridge:
    """Routes messages between channels. Registered senders keyed by platform:id."""

    def __init__(self):
        # Senders: {platform -> async callable(platform_id, message)}
        self._senders: dict[str, Callable[[str, str], Awaitable[None]]] = {}

    def register_sender(self, platform: str, sender: Callable[[str, str], Awaitable[None]]) -> None:
        self._senders[platform] = sender
        log.info("Channel sender registered: %s", platform)

    async def send(self, platform: str, platform_id: str, message: str) -> bool:
        sender = self._senders.get(platform)
        if not sender:
            return False
        await sender(platform_id, message)
        return True


class SendMessageTool(Tool):
    def __init__(self, bridge: ChannelBridge, memory_store: Any):
        self.bridge = bridge
        self.memory_store = memory_store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="send_message",
            description=(
                "Send a message to any communication channel — Discord, worker CLI, etc. "
                "Use this to post results, relay information, or communicate across channels. "
                "Specify the platform and channel. For Discord use the channel ID."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["discord", "worker"],
                        "description": "Target platform.",
                    },
                    "channel": {
                        "type": "string",
                        "description": "Channel identifier — Discord channel ID or worker name.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message to send.",
                    },
                },
                "required": ["platform", "channel", "message"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        platform = kwargs.get("platform", "")
        channel = kwargs.get("channel", "")
        message = kwargs.get("message", "")

        if not all([platform, channel, message]):
            return ToolResult(success=False, output="", error="platform, channel, and message required")

        sent = await self.bridge.send(platform, channel, message)
        if sent:
            return ToolResult(success=True, output=f"Message sent to {platform}:{channel}")
        return ToolResult(success=False, output="", error=f"No sender registered for platform '{platform}'")


class ListChannelsTool(Tool):
    def __init__(self, memory_store: Any):
        self.memory_store = memory_store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="list_channels",
            description="List all known communication channels — Discord, workers, CLI, etc.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        channels = self.memory_store.list_channels()
        if not channels:
            return ToolResult(success=True, output="No channels registered.")
        return ToolResult(success=True, output=json.dumps(channels, indent=2, default=str))
