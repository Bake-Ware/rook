"""Fact extractor — background LLM call to extract key facts from conversation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from .facts import FactStore

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_EXTRACTION_PROMPT = """Extract key facts from this conversation exchange. Return a JSON array of objects with:
- "fact": the specific information (be precise — include exact values, URLs, names, numbers)
- "category": one of "url", "credential", "config", "concept", "decision", "preference", "reference", "general"
- "importance": 0.0 to 1.0 (1.0 = critical like credentials/URLs, 0.3 = casual mention)

Only extract facts worth remembering. Skip greetings, filler, and things already obvious from context.
If there are no notable facts, return an empty array: []

Return ONLY the JSON array, no other text."""


class FactExtractor:
    """Extracts facts from conversation exchanges using an LLM call."""

    def __init__(self, endpoint: str, model: str, fact_store: FactStore):
        self.endpoint = endpoint
        self.model = model
        self.fact_store = fact_store

    async def extract_and_store(self, user_message: str, assistant_response: str) -> list[dict]:
        """Extract facts from the latest exchange and push to volatile.

        Returns the list of extracted facts for logging.
        """
        exchange = f"User: {user_message}\n\nAssistant: {assistant_response}"

        # Truncate very long exchanges
        if len(exchange) > 8000:
            exchange = exchange[:8000] + "\n... (truncated)"

        client = AsyncOpenAI(base_url=self.endpoint, api_key="not-needed")
        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _EXTRACTION_PROMPT},
                    {"role": "user", "content": exchange},
                ],
                max_tokens=1000,
            )
        except Exception as e:
            log.error("Fact extraction LLM call failed: %s", e)
            return []
        finally:
            await client.close()

        raw = response.choices[0].message.content or ""
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

            # Skip error messages, request IDs, and other noise
            noise_patterns = ["error code:", "request_id", "req_01", "internal server error",
                              "timed out", "401", "500", "failed to"]
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
