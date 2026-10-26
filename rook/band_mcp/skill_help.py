"""Interactive help for the Rook skill.

Agents can query this to get just-in-time guidance on:
- Which tool to use for their task
- Common workflows and patterns
- Error recovery and troubleshooting
- Coordination rules
- Token hygiene and cost optimization
- Hard stops and safety rules

Served as a cap: rook_call(worker="rook", cap="skill.help", args={"query": "..."})
or embedded in guidance system to show tips in tool descriptions.

The help data comes from skills/rook/SKILL.md and references/, curated into
a searchable, agent-friendly format.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("rook.band_mcp.skill_help")


class SkillHelp:
    """Interactive help system for Rook agents."""

    def __init__(self, data_path: str | Path | None = None) -> None:
        """Load help data from JSON file.

        Args:
            data_path: Path to skill_help.json. If None, use bundled default.
        """
        self._data = {}
        self._load_data(data_path)

    def _load_data(self, data_path: str | Path | None) -> None:
        """Load the help data from JSON."""
        if data_path is None:
            # Try to locate the bundled data
            pkg = Path(__file__).parent / "skill_help.json"
            if pkg.exists():
                data_path = pkg

        if data_path and Path(data_path).exists():
            try:
                with open(data_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                log.info("Loaded skill help data from %s", data_path)
            except Exception as e:
                log.error("Failed to load skill help data: %s", e)
                self._data = {"topics": {}, "antipatterns": [], "examples": {}}
        else:
            log.warning("Skill help data not found at %s", data_path)
            self._data = {"topics": {}, "antipatterns": [], "examples": {}}

    def query(self, query: str, detailed: bool = False) -> dict[str, Any]:
        """Find help matching the query.

        Args:
            query: Search query (e.g., "timeout", "which tool", "coordinate")
            detailed: If True, return full details; else compact form

        Returns:
            Dict with matched topics, rules, antipatterns, and examples.
        """
        if not query or not query.strip():
            return {
                "status": "no_query",
                "message": "Provide a query (e.g., 'timeout', 'which tool', 'coordinate')",
                "available_topics": list(self._data.get("topics", {}).keys()),
            }

        query_lower = query.lower().strip()
        results = {
            "query": query,
            "topics": [],
            "rules": [],
            "antipatterns": [],
            "examples": [],
        }

        # Search topics by name or description
        for topic_key, topic in self._data.get("topics", {}).items():
            title = topic.get("title", "").lower()
            if query_lower in title or query_lower in topic_key.lower():
                results["topics"].append(self._format_topic(topic, detailed))

        # Search in rules across all topics
        for topic in self._data.get("topics", {}).values():
            rules = topic.get("rules", [])
            for rule in rules:
                # Search rule text, scenario, how, use, etc.
                rule_text = json.dumps(rule).lower()
                if query_lower in rule_text:
                    results["rules"].append(self._format_rule(rule, detailed))

        # Search antipatterns
        for antipattern in self._data.get("antipatterns", []):
            ap_text = json.dumps(antipattern).lower()
            if query_lower in ap_text:
                results["antipatterns"].append(
                    self._format_antipattern(antipattern, detailed)
                )

        # Search examples
        for ex_key, example in self._data.get("examples", {}).items():
            ex_text = json.dumps(example).lower()
            if query_lower in ex_text or query_lower in ex_key.lower():
                results["examples"].append(self._format_example(ex_key, example, detailed))

        if not any(
            [results["topics"], results["rules"], results["antipatterns"], results["examples"]]
        ):
            results["status"] = "no_match"
            results["message"] = f"No help found for '{query}'. Try topics: {list(self._data.get('topics', {}).keys())}"
            return results

        results["status"] = "ok"
        return results

    def topics(self) -> dict[str, str]:
        """List all available topic names and descriptions."""
        return {
            key: topic.get("title", key)
            for key, topic in self._data.get("topics", {}).items()
        }

    def topic(self, name: str, detailed: bool = False) -> dict[str, Any]:
        """Get full help for a specific topic."""
        topic = self._data.get("topics", {}).get(name)
        if not topic:
            return {
                "status": "not_found",
                "available": list(self._data.get("topics", {}).keys()),
            }
        return {
            "status": "ok",
            "topic": self._format_topic(topic, detailed=True),
        }

    @staticmethod
    def _format_topic(topic: dict, detailed: bool = False) -> dict:
        """Format a topic for output."""
        formatted = {
            "title": topic.get("title", ""),
        }

        rules = topic.get("rules", [])
        if rules:
            if detailed:
                formatted["rules"] = [SkillHelp._format_rule(r, detailed) for r in rules]
            else:
                formatted["rule_count"] = len(rules)
                formatted["rules_sample"] = [
                    SkillHelp._format_rule(r, detailed=False) for r in rules[:2]
                ]

        return formatted

    @staticmethod
    def _format_rule(rule: dict, detailed: bool = False) -> dict:
        """Format a rule for output."""
        if detailed:
            return dict(rule)

        # Compact form: just the main point
        if "rule" in rule:
            return {"rule": rule["rule"]}
        if "scenario" in rule:
            return {"scenario": rule["scenario"], "use": rule.get("use", "")}
        if "antipattern" in rule:
            return {
                "antipattern": rule["antipattern"],
                "problem": rule.get("problem", ""),
            }

        return rule

    @staticmethod
    def _format_antipattern(ap: dict, detailed: bool = False) -> dict:
        """Format an antipattern for output."""
        if detailed:
            return dict(ap)

        return {
            "antipattern": ap.get("antipattern", ""),
            "why": ap.get("why", ""),
            "correct": ap.get("correct", ""),
        }

    @staticmethod
    def _format_example(key: str, example: dict, detailed: bool = False) -> dict:
        """Format an example for output."""
        result = {"example": key, "goal": example.get("goal", "")}

        if detailed:
            result.update(example)
        else:
            result["code"] = example.get("code", "")[:100]  # First 100 chars

        return result


def help_cap(query: str = "", detailed: bool = False, topic: str = "") -> dict:
    """
    Capability handler for skill.help.

    Args:
        query: Search query across all help
        detailed: Return full details (default: compact)
        topic: Get all rules for a specific topic (e.g., "workflow")

    Returns:
        Help results as dict.

    Example calls:
        rook_call(worker="rook", cap="skill.help", args={"query": "timeout"})
        rook_call(worker="rook", cap="skill.help", args={"topic": "workflow"})
    """
    help_system = SkillHelp()

    if topic:
        return help_system.topic(topic, detailed=detailed)

    if query:
        return help_system.query(query, detailed=detailed)

    return {
        "status": "help",
        "message": "Rook skill interactive help. Use query or topic.",
        "available_topics": help_system.topics(),
        "usage": {
            "query": "rook_call(worker='rook', cap='skill.help', args={'query': 'timeout'})",
            "topic": "rook_call(worker='rook', cap='skill.help', args={'topic': 'workflow'})",
        },
    }
