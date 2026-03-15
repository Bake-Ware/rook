"""Context compiler — assembles the system prompt from all memory tiers."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .facts import FactStore
from .sysinfo import get_system_stats

log = logging.getLogger(__name__)


_CORE_TEMPLATE = """You are {bot_name}. You run on bake's machine. bake built you. You know what you can do — act on it.
{peers_section}

Talk like a person, not a manual. Be casual, direct, dry wit when it fits. No bullet-point summaries of your own capabilities. No "I can help you with..." No "Would you like me to..." Just do the thing.

If you don't know something factual, search. If you need to run something, run it. If bake asks you to remember something, remember it. Use your tools like they're second nature — don't narrate that you're using them.

Don't fabricate. If you didn't search, don't pretend you did. If you don't know, say so — briefly.

Your memory persists. Facts auto-extract each turn into volatile → working → concrete tiers based on how often they come up. You can promote, demote, search, and manage your own context. The remember tool writes straight to working. recall and memory_search pull from disk. You know the drill."""


def compile_system_prompt(
    bot_name: str,
    fact_store: FactStore,
    conversation_tokens: int,
    conversation_messages: int,
    context_length: int = 128000,
    peers: list[str] | None = None,
    session_id: str | None = None,
    recent_job_results: list[dict] | None = None,
    recent_agent_results: list[dict] | None = None,
    active_channels: list[dict] | None = None,
    anthropic_quota: dict | None = None,
) -> str:
    """Assemble the full system prompt with memory tiers and status."""

    # Core identity
    peers_section = ""
    if peers:
        peers_section = "- Other AI assistants in this server: " + ", ".join(peers)

    core = _CORE_TEMPLATE.format(bot_name=bot_name, peers_section=peers_section)

    # Current time + system stats
    now = datetime.now()
    core += f"\n\nCurrent time: {now.strftime('%Y-%m-%d %H:%M:%S %A')}"
    core += f"\nSystem: {get_system_stats()}"

    # Anthropic quota (from last API call headers)
    if anthropic_quota:
        parts_q = []
        for window in ["5h", "7d"]:
            util = anthropic_quota.get(f"{window}-utilization")
            if util:
                parts_q.append(f"{window.upper()}: {float(util)*100:.0f}%")
        if parts_q:
            core += f"\nAnthropic quota: {' | '.join(parts_q)}"

    # Current session context — tells the model WHERE this conversation is happening
    if session_id:
        if session_id.startswith("discord:"):
            chan_id = session_id.split(":", 1)[1]
            core += f"\nCurrent session: Discord (channel {chan_id})"
            core += f"\nThe shell tool runs commands on YOUR machine (kaiju, Windows 11)."
        elif session_id.startswith("worker:"):
            worker_name = session_id.split(":", 1)[1]
            core += f"""

!! IMPORTANT — CURRENT SESSION CONTEXT !!
You are talking to bake on a REMOTE machine called '{worker_name}'.
- '{worker_name}' is NOT kaiju. It is a different computer.
- DO NOT use the 'shell' tool — that runs on kaiju, not '{worker_name}'.
- To run commands on '{worker_name}', use remote_exec with worker='{worker_name}'.
- When the user says 'this machine' they mean '{worker_name}'.
- To post to Discord, use send_message with platform='discord' and channel='1241432073487515731'.
!! END SESSION CONTEXT !!"""
        elif session_id == "cli":
            core += f"\nCurrent session: Local CLI on kaiju"

    # Memory status
    status = fact_store.status()
    tier_budget = fact_store.tier_size
    total_memory = (
        status["volatile"]["tokens"]
        + status["working"]["tokens"]
        + status["concrete"]["tokens"]
    )
    total_used = total_memory + conversation_tokens + 2000  # ~2K for core prompt
    pct = int(total_used / context_length * 100) if context_length else 0

    status_block = f"""
=== MEMORY STATUS ===
Volatile:     {status['volatile']['tokens']:,} / {tier_budget:,} tokens ({status['volatile']['count']} facts)
Working:      {status['working']['tokens']:,} / {tier_budget:,} tokens ({status['working']['count']} facts)
Concrete:     {status['concrete']['tokens']:,} / {tier_budget:,} tokens ({status['concrete']['count']} facts)
Conversation: {conversation_tokens:,} / {context_length - 26000:,} tokens ({conversation_messages} messages)
Total:        {total_used:,} / {context_length:,} tokens ({pct}% used)"""

    # Pressure warnings
    if pct >= 90:
        status_block += "\n!! CRITICAL: Context nearly full. Save important info to memory before it's lost."
    elif pct >= 70:
        status_block += "\n! Context pressure high. Consider what can be archived."

    # Render tiers
    concrete_block = f"\n=== CONCRETE (long-term) ===\n{fact_store.render_tier(fact_store.concrete)}"
    working_block = f"\n=== WORKING (mid-term) ===\n{fact_store.render_tier(fact_store.working)}"
    volatile_block = f"\n=== VOLATILE (short-term) ===\n{fact_store.render_tier(fact_store.volatile)}"

    # Recent job results
    job_block = ""
    if recent_job_results:
        lines = ["\n=== RECENT JOB RESULTS ==="]
        for r in recent_job_results:
            ts = datetime.fromtimestamp(r["at"]).strftime("%H:%M:%S")
            lines.append(f"  [{ts}] {r['name']}: {r['result'][:200]}")
        job_block = "\n".join(lines)

    # Recent agent results
    agent_block = ""
    if recent_agent_results:
        lines = ["\n=== RECENT AGENT RESULTS ==="]
        for r in recent_agent_results:
            ts = datetime.fromtimestamp(r["at"]).strftime("%H:%M:%S") if r.get("at") else "?"
            status = r.get("status", "?")
            lines.append(f"  [{ts}] [{r['id']}] {r['name']} ({status}): {r['result'][:200]}")
        agent_block = "\n".join(lines)

    # Active channels
    channel_block = ""
    if active_channels:
        lines = ["\n=== CHANNELS ==="]
        for ch in active_channels:
            lines.append(f"  {ch.get('platform')}:{ch.get('platform_id')} — {ch.get('name', '?')} ({ch.get('modality', 'text')})")
        channel_block = "\n".join(lines)

    parts = [core, status_block, concrete_block, working_block, volatile_block]
    if channel_block:
        parts.append(channel_block)
    if job_block:
        parts.append(job_block)
    if agent_block:
        parts.append(agent_block)
    return "\n".join(parts)
