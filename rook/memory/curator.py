"""Context curator — uses fast local model to select relevant memory for each message."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from .facts import FactStore

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_CURATION_PROMPT = """You are a context filter. Given a user message and a list of stored facts, return ONLY the IDs of facts that are relevant to answering this message. Be selective — only include facts the assistant actually needs.

Return a JSON array of relevant fact IDs. If none are relevant, return [].
Return ONLY the JSON array, nothing else."""


class ContextCurator:
    """Pre-filters memory facts for relevance before sending to expensive models."""

    def __init__(self, endpoint: str, model: str):
        self.endpoint = endpoint
        self.model = model

    async def curate(self, user_message: str, fact_store: FactStore,
                     session_context: str = "") -> dict[str, list]:
        """Select relevant facts from each tier. Returns {tier: [MemoryFact]}."""
        all_facts = []
        for tier_name, tier_list in [
            ("concrete", fact_store.concrete),
            ("working", fact_store.working),
            ("volatile", fact_store.volatile),
        ]:
            for f in tier_list:
                all_facts.append({"id": f.id, "tier": tier_name, "fact": f.fact, "category": f.category})

        if not all_facts:
            return {"concrete": [], "working": [], "volatile": []}

        # Build the fact list for the model
        fact_list = "\n".join(f"[{f['id']}] ({f['tier']}/{f['category']}) {f['fact']}" for f in all_facts)

        prompt = f"User message: {user_message}\n\nStored facts:\n{fact_list}"

        client = AsyncOpenAI(base_url=self.endpoint, api_key="not-needed")
        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _CURATION_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=500,
            )
        except Exception as e:
            log.error("Curation call failed: %s — using all facts", e)
            return {
                "concrete": list(fact_store.concrete),
                "working": list(fact_store.working),
                "volatile": list(fact_store.volatile),
            }
        finally:
            await client.close()

        raw = response.choices[0].message.content or "[]"
        raw = _THINK_RE.sub("", raw).strip()
        selected_ids = self._parse_ids(raw)

        if not selected_ids:
            # Model said nothing relevant — still include concrete (always important)
            return {
                "concrete": list(fact_store.concrete),
                "working": [],
                "volatile": [],
            }

        log.info("Curator selected %d/%d facts for: %s",
                 len(selected_ids), len(all_facts), user_message[:60])

        # Filter each tier
        selected_set = set(selected_ids)
        return {
            "concrete": [f for f in fact_store.concrete if f.id in selected_set],
            "working": [f for f in fact_store.working if f.id in selected_set],
            "volatile": [f for f in fact_store.volatile if f.id in selected_set],
        }

    def _parse_ids(self, raw: str) -> list[str]:
        """Parse JSON array of IDs from model response."""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()

        try:
            result = json.loads(raw)
            if isinstance(result, list):
                return [str(x) for x in result]
        except json.JSONDecodeError:
            match = re.search(r"\[.*?\]", raw, re.DOTALL)
            if match:
                try:
                    return [str(x) for x in json.loads(match.group())]
                except json.JSONDecodeError:
                    pass
        return []
