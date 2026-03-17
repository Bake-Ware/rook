"""Context curator — uses the router to select relevant memory for each message."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .facts import FactStore

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_CURATION_PROMPT = """You filter memory for an AI assistant. Given a user message and stored facts, return ONLY the fact IDs the assistant NEEDS to respond to THIS specific message.

Be AGGRESSIVE about filtering:
- If the user says "hi" or casual chat, return [] — no facts needed.
- Only include a fact if NOT having it would cause a wrong or incomplete answer.
- Credentials/IPs only if the message involves connecting to that specific machine.
- Preferences only if the message relates to behavior/style.
- Never include everything "just in case".

Return a JSON array of fact IDs. Return [] if none are needed.
ONLY the JSON array."""


class ContextCurator:
    """Pre-filters memory facts for relevance using the router."""

    def __init__(self, router, model_name: str):
        self.router = router
        self.model_name = model_name

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

        fact_list = "\n".join(f"[{f['id']}] ({f['tier']}/{f['category']}) {f['fact']}" for f in all_facts)
        prompt = f"User message: {user_message}\n\nStored facts:\n{fact_list}"

        messages = [
            {"role": "system", "content": _CURATION_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            entry = self.router.resolve(self.model_name)
            if not entry:
                log.error("Curator model '%s' not found", self.model_name)
                return {"concrete": list(fact_store.concrete), "working": list(fact_store.working), "volatile": list(fact_store.volatile)}

            if entry.provider == "anthropic":
                response = await self.router._anthropic_chat(entry, messages, None)
            else:
                response = await self.router._openai_chat(entry, messages, None)

            raw = response.get("content") or "[]"
        except Exception as e:
            log.error("Curation call failed: %s — using all facts", e)
            return {
                "concrete": list(fact_store.concrete),
                "working": list(fact_store.working),
                "volatile": list(fact_store.volatile),
            }

        raw = _THINK_RE.sub("", raw).strip()
        selected_ids = self._parse_ids(raw)

        if not selected_ids:
            return {
                "concrete": list(fact_store.concrete),
                "working": [],
                "volatile": [],
            }

        log.info("Curator selected %d/%d facts for: %s",
                 len(selected_ids), len(all_facts), user_message[:60])

        selected_set = set(selected_ids)
        return {
            "concrete": [f for f in fact_store.concrete if f.id in selected_set],
            "working": [f for f in fact_store.working if f.id in selected_set],
            "volatile": [f for f in fact_store.volatile if f.id in selected_set],
        }

    def _parse_ids(self, raw: str) -> list[str]:
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
