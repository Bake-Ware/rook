"""Fact extractor — background LLM call to extract key facts from conversation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .facts import FactStore

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_EXTRACTION_PROMPT = """You manage long-term memory for an AI assistant. Extract ONLY facts that will be useful weeks or months from now.

Return a JSON array. Each object:
- "fact": A COMPLETE, self-contained statement. Include ALL context needed to understand it later without any conversation history. Bad: "192.168.1.168". Good: "starscream (Proxmox server) is at 192.168.1.168, SSH as root with password Jamison1129!"
- "category": EXACTLY one of:
    "url" — URLs, endpoints, domains
    "credential" — passwords, tokens, API keys, SSH creds
    "config" — IPs, hostnames, hardware specs, software versions, paths, ports
    "command" — CLI commands, syntax patterns worth remembering
    "preference" — how the user wants things done, standing instructions
    "general" — anything that doesn't fit above (will NOT be promoted to long-term)
- "importance": 0.0-1.0

RULES:
- Be VERBOSE in the fact text. It must make sense standalone with zero context.
- ONLY extract things that are PERMANENTLY true: hostnames, IPs, credentials, hardware, software versions, user preferences.
- NEVER extract: current connection status, error messages, what tools were called, task progress, temporary states, what just happened in conversation.
- NEVER duplicate information already covered by an existing fact. If unsure, skip it.
- If nothing worth remembering permanently was discussed, return []
- Most conversations should return [] — only extract when genuinely new permanent info appears.

Return ONLY the JSON array."""


class FactExtractor:
    """Extracts facts from conversation exchanges using the router."""

    def __init__(self, router, model_name: str, fact_store: FactStore):
        self.router = router
        self.model_name = model_name
        self.fact_store = fact_store

    async def extract_and_store(self, user_message: str, assistant_response: str) -> list[dict]:
        """Extract facts from the latest exchange and push to volatile."""
        exchange = f"User: {user_message}\n\nAssistant: {assistant_response}"

        if len(exchange) > 8000:
            exchange = exchange[:8000] + "\n... (truncated)"

        messages = [
            {"role": "system", "content": _EXTRACTION_PROMPT},
            {"role": "user", "content": exchange},
        ]

        try:
            # Use the router — handles OpenAI-compat and Anthropic transparently
            entry = self.router.resolve(self.model_name)
            if not entry:
                log.error("Extractor model '%s' not found", self.model_name)
                return []

            if entry.provider == "anthropic":
                response = await self.router._anthropic_chat(entry, messages, None)
            else:
                response = await self.router._openai_chat(entry, messages, None)

            raw = response.get("content") or ""
        except Exception as e:
            log.error("Fact extraction LLM call failed: %s", e)
            return []
        raw = _THINK_RE.sub("", raw).strip()

        # Parse JSON from response
        facts = self._parse_facts(raw)
        if not facts:
            return []

        # Push each fact to volatile
        for f in facts:
            fact_text = f.get("fact", "").strip()
            if not fact_text or len(fact_text) < 5:
                continue
            category = f.get("category", "general")
            importance = float(f.get("importance", 0.5))

            # Skip noise — transient states, errors, status, tool output
            noise_patterns = [
                "error code:", "request_id", "req_01", "internal server error",
                "timed out", "401", "500", "failed to", "not found",
                "connected", "disconnected", "reconnect", "online", "offline",
                "worker id:", "agent id:", "spawned", "completed",
                "module is loaded", "module started", "service is",
                "currently", "right now", "just now", "still",
            ]
            if any(p in fact_text.lower() for p in noise_patterns):
                continue

            # Check for duplicates across all tiers
            if self._is_duplicate(fact_text):
                continue

            self.fact_store.add_volatile(fact_text, category, importance)
            log.info("Extracted fact [%s]: %s", category, fact_text[:80])

        # Check promotions after adding new facts
        self.fact_store.check_promotions()

        # Flush to disk (the maintenance cycle)
        self.fact_store.flush_to_db()

        return facts

    def _parse_facts(self, raw: str) -> list[dict]:
        """Parse JSON array from LLM response, handling common formatting issues."""
        # Try to find JSON array in the response
        raw = raw.strip()

        # Strip markdown code fences
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()

        try:
            result = json.loads(raw)
            if isinstance(result, list):
                return result
            return []
        except json.JSONDecodeError:
            # Try to find array within the text
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            log.warning("Could not parse extraction response: %s", raw[:200])
            return []

    def _is_duplicate(self, fact_text: str) -> bool:
        """Check if a fact already exists in any tier (fuzzy match)."""
        fact_lower = fact_text.lower()
        for tier in [self.fact_store.volatile, self.fact_store.working, self.fact_store.concrete]:
            for existing in tier:
                existing_lower = existing.fact.lower()
                # Exact or near-exact match
                if fact_lower == existing_lower:
                    return True
                # Substantial overlap (>80% of words match)
                fact_words = set(fact_lower.split())
                existing_words = set(existing_lower.split())
                if fact_words and existing_words:
                    overlap = len(fact_words & existing_words) / max(len(fact_words), len(existing_words))
                    if overlap > 0.8:
                        existing.touch()  # existing fact gets an access bump
                        return True
        return False
