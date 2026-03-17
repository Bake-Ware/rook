"""Model router — zero-friction model switching.

Say 'use opus' and it just works. No provider prefixes, no indirection.
Each model entry has everything needed to make the API call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from .config import Config
from .anthropic_auth import AnthropicAuth

log = logging.getLogger(__name__)

# Patterns that trigger a model switch in conversation
_SWITCH_PATTERNS = [
    re.compile(r"\b(?:use|switch\s+to|swap\s+to|change\s+to)\s+(\S+)", re.I),
    re.compile(r"\b(?:talk\s+to|ask)\s+(\S+)", re.I),
]


@dataclass
class ModelEntry:
    """Everything needed to call a model. No indirection."""

    name: str
    provider: str
    endpoint: str
    model: str
    api_key: str | None = None
    context_length: int = 128000


class Router:
    """Resolves model names/aliases and manages the active model per session."""

    def __init__(self, config: Config):
        self.config = config
        self._entries: dict[str, ModelEntry] = {}
        self._sessions: dict[str, str] = {}  # session_id -> model name
        self._anthropic_auth = AnthropicAuth()
        self._anthropic_quota: dict[str, Any] = {}  # latest rate limit info
        self._build_entries()

    def _build_entries(self) -> None:
        """Build the model registry from config."""
        self._entries.clear()
        for name, spec in self.config.models.items():
            api_key = None
            if key_env := spec.get("key_env"):
                api_key = self.config.resolve_env(key_env)
            self._entries[name] = ModelEntry(
                name=name,
                provider=spec.get("provider", "openai-compat"),
                endpoint=spec.get("endpoint", "http://localhost:1234/v1"),
                model=spec.get("model", name),
                api_key=api_key,
                context_length=spec.get("context_length", 128000),
            )

    def reload(self) -> None:
        """Reload model entries from config."""
        if self.config.reload():
            self._build_entries()
            log.info("Model registry reloaded: %s", list(self._entries.keys()))

    def resolve(self, name: str) -> ModelEntry | None:
        """Resolve a model name or alias to its entry."""
        name = name.lower().strip()
        if name in self._entries:
            return self._entries[name]
        if target := self.config.aliases.get(name):
            return self._entries.get(target)
        for key in self._entries:
            if name in key:
                return self._entries[key]
        return None

    def detect_switch(self, text: str) -> str | None:
        """Detect if the user wants to switch models. Returns model name or None."""
        for pattern in _SWITCH_PATTERNS:
            if m := pattern.search(text):
                candidate = m.group(1).strip(".,!?")
                if self.resolve(candidate):
                    return candidate
        return None

    def get_active(self, session_id: str = "default") -> ModelEntry:
        """Get the active model for a session."""
        name = self._sessions.get(session_id, self.config.default_model)
        entry = self.resolve(name)
        if not entry:
            entry = self.resolve(self.config.default_model)
        if not entry:
            entry = next(iter(self._entries.values()))
        return entry

    def set_active(self, session_id: str, name: str) -> ModelEntry | None:
        """Switch the active model for a session. Returns the entry or None."""
        entry = self.resolve(name)
        if entry:
            self._sessions[session_id] = entry.name
            log.info("Session %s switched to model: %s", session_id, entry.name)
        return entry

    def list_models(self) -> list[dict[str, str]]:
        """List all available models with their aliases."""
        result = []
        alias_map: dict[str, list[str]] = {}
        for alias, target in self.config.aliases.items():
            alias_map.setdefault(target, []).append(alias)
        for name, entry in self._entries.items():
            result.append({
                "name": name,
                "model": entry.model,
                "provider": entry.provider,
                "endpoint": entry.endpoint,
                "aliases": ", ".join(alias_map.get(name, [])),
            })
        return result

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        session_id: str = "default",
    ) -> dict[str, Any]:
        """Send a chat request, returns a normalized response dict.

        Returns:
            {
                "content": str | None,
                "tool_calls": [{"id": str, "name": str, "arguments": dict}] | None,
            }
        """
        entry = self.get_active(session_id)

        if entry.provider == "anthropic":
            return await self._anthropic_chat(entry, messages, tools)
        else:
            return await self._openai_chat(entry, messages, tools)

    async def _openai_chat(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        client = AsyncOpenAI(
            base_url=entry.endpoint,
            api_key=entry.api_key or "not-needed",
        )
        try:
            kwargs: dict[str, Any] = {"model": entry.model, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            response = await client.chat.completions.create(**kwargs)
        finally:
            await client.close()

        choice = response.choices[0]
        result: dict[str, Any] = {"content": choice.message.content, "tool_calls": None}

        if choice.message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments) if tc.function.arguments else {},
                }
                for tc in choice.message.tool_calls
            ]
        return result

    @staticmethod
    def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
        """Clean a JSON schema to be Anthropic 2020-12 compliant."""
        schema = dict(schema)

        # Remove unsupported keys
        for key in ["default", "examples", "patternProperties", "$schema", "additionalProperties"]:
            schema.pop(key, None)

        # Ensure properties exists for object types
        if schema.get("type") == "object" and not schema.get("properties"):
            schema["properties"] = {"_dummy": {"type": "string", "description": "unused"}}

        # Recursively clean nested properties
        if "properties" in schema:
            cleaned_props = {}
            for k, v in schema["properties"].items():
                if isinstance(v, dict):
                    v = dict(v)
                    for rm_key in ["default", "examples", "patternProperties"]:
                        v.pop(rm_key, None)
                    # Recurse for nested objects
                    if v.get("type") == "object" and "properties" in v:
                        v = Router._clean_schema(v)
                cleaned_props[k] = v
            schema["properties"] = cleaned_props

        # Clean items for array types
        if "items" in schema and isinstance(schema["items"], dict):
            schema["items"] = Router._clean_schema(schema["items"])

        return schema

    async def _anthropic_chat(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Use Claude Agent SDK to call Anthropic models via Claude Code subscription."""
        import os
        import anyio
        from claude_agent_sdk import query

        # Build prompt from messages
        system_prompt = ""
        conversation = []
        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            elif msg["role"] == "user":
                conversation.append(f"User: {msg.get('content', '')}")
            elif msg["role"] == "assistant":
                content = msg.get("content", "")
                if content:
                    conversation.append(f"Assistant: {content}")
            elif msg["role"] == "tool":
                conversation.append(f"Tool result ({msg.get('name', '?')}): {msg.get('content', '')}")

        # Build the prompt — system context + conversation + latest user message
        prompt_parts = []
        if system_prompt:
            prompt_parts.append(f"System context:\n{system_prompt}\n")
        if conversation:
            prompt_parts.append("Conversation so far:\n" + "\n".join(conversation[-20:]))  # last 20 exchanges

        prompt = "\n\n".join(prompt_parts)

        # Ensure CLAUDECODE is unset so SDK can launch
        env_backup = os.environ.pop("CLAUDECODE", None)

        try:
            result_text = ""
            async for msg in query(
                prompt=prompt,
                options={
                    "max_turns": 1,
                    "system_prompt": system_prompt[:10000] if system_prompt else None,
                    "model": entry.model,
                },
            ):
                # Extract text from AssistantMessage
                msg_type = type(msg).__name__
                if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                    for block in msg.content:
                        if hasattr(block, "text"):
                            result_text += block.text
                elif msg_type == "ResultMessage" and hasattr(msg, "result"):
                    if not result_text and msg.result:
                        result_text = msg.result
        finally:
            if env_backup is not None:
                os.environ["CLAUDECODE"] = env_backup

        return {"content": result_text or "No response from model.", "tool_calls": None}
