"""Hygiene trigger: a deterministic nudge for a nondeterministic process.

When a claimed, in-progress task has been idle past a timeout and looks dirty
(activity since its last handoff), the hub asks an agent to write the work up:
save a handoff, link evidence, capture knowledge, set the task state.
(docs/DESIGN-agent-work-system.md §5)

For each dirty claim, once per idle period:

0. If the claimant's host isn't a live worker (or is ``web``), mark it dirty.
1. The claim has a provider session and the worker can ``<client>-history.send``
   → send the prompt into that same session.
2. The worker has ``agent.wake`` → open a chat room with the prompt and wake a
   fresh agent there.
3. Otherwise → mark it dirty (the deck shows ``needs_hygiene``).

It never changes task state and never blocks anything. Every nudge or mark is
recorded as an event on the task, and every band call is journaled.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .attribution import norm

log = logging.getLogger("rook.band_mcp.hygiene")

SYSTEM = {"id": "system:hygiene", "kind": "system", "label": "Rook hygiene"}
IDLE_SECONDS = 30 * 60
# Map a normalized MCP client name to the worker's history cap family.
FAMILY = {"claudecode": "claude", "claude": "claude", "codex": "codex", "codexmcpclient": "codex"}


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


class Hygiene:
    def __init__(self, knowledge, client, chat, prompt, journal=None,
                 idle: float = IDLE_SECONDS) -> None:
        self.knowledge = knowledge
        self.client = client
        self.chat = chat
        self.prompt = prompt  # callable() -> template text
        self.journal = journal
        self.idle = idle

    async def run(self, every: float = 60.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("hygiene tick failed")
            await asyncio.sleep(every)

    def _dirty(self, claim) -> bool:
        with self.knowledge.store.db(False) as db:
            row = db.execute("SELECT max(ts) FROM links WHERE record=? AND kind='handoff' AND retracts IS NULL",
                             (claim["task"],)).fetchone()
        last_handoff = row[0] or 0
        return last_handoff < claim["last_active"]

    def _worker(self, host):
        if not host or host == "web":
            return None
        for wid, w in self.client.workers.items():
            if norm(w.get("name"), "") == host:
                return wid, w
        return None

    def _text(self, claim, idle_min: int) -> str:
        return (self.prompt() or "").format_map(_Safe(
            slug=claim.get("slug") or "", title=claim.get("title") or "", id=claim["task"],
            idle=idle_min, actor=claim["actor"]))

    async def tick(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        done = []
        store = self.knowledge.store
        for c in store.active_claims():
            if now - c["last_active"] < self.idle:
                continue
            if c["nudged"] and c["nudged"] >= c["last_active"]:
                continue  # already handled this idle period
            if not self._dirty(c):
                continue
            idle_min = int((now - c["last_active"]) // 60)
            outcome = await self._nudge(c, idle_min)
            if outcome["action"] == "marked_dirty":
                store.mark_claim(c["id"], nudged=now, dirty=now, actor=SYSTEM,
                                 note="hygiene_dirty", data=outcome)
            else:
                store.mark_claim(c["id"], nudged=now, actor=SYSTEM, note="hygiene_nudge", data=outcome)
            done.append({"claim": c["id"], "task": c["task"], **outcome})
        return done

    async def _call(self, cap, args, target, timeout=30.0):
        reply = await self.client.call(cap=cap, args=args, target=target, timeout=timeout,
                                       identity=SYSTEM["id"])
        if self.journal is not None:
            self.journal.record(cap=cap, worker=(self.client.workers.get(target) or {}).get("name"),
                                identity=SYSTEM["id"], args={k: v for k, v in args.items() if k != "text"},
                                reply=reply, audit={"kind": "system", "actor": SYSTEM["id"]})
        return reply

    async def _nudge(self, c, idle_min: int) -> dict:
        found = self._worker(c.get("host"))
        if not found:
            return {"action": "marked_dirty", "reason": f"worker {c.get('host') or 'web'!r} is not on the band"}
        wid, w = found
        caps = w.get("caps", [])
        text = self._text(c, idle_min)
        family = FAMILY.get(c.get("client") or "")
        try:
            if c.get("provider_session") and family and f"{family}-history.send" in caps:
                reply = await self._call(f"{family}-history.send", {
                    "session_id": c["provider_session"], "text": text,
                    "command_id": "hygiene-" + uuid.uuid4().hex[:12]}, wid, timeout=40)
                if reply.get("ok") and (reply.get("result") or {}).get("ok", True):
                    return {"action": "sent_to_session", "worker": w.get("name")}
            if "agent.wake" in caps:
                room = self.chat.start(f"Hygiene: {c.get('title') or c['task']}", SYSTEM["id"], [c["actor"]])
                rid = room.get("room") or room.get("id")
                if rid:
                    self.chat.send(rid, SYSTEM["id"], text, [], True)
                    reply = await self._call("agent.wake", {
                        "room": rid, "title": f"Hygiene: {c.get('title') or ''}",
                        "transcript": [{"sender": SYSTEM["id"], "text": text}],
                        "woken_by": SYSTEM["id"], "note": "hygiene check for an idle claimed task"}, wid)
                    if reply.get("ok") and (reply.get("result") or {}).get("ok", True):
                        return {"action": "woke_agent", "worker": w.get("name"), "room": rid}
        except Exception as e:  # noqa: BLE001 — fall through to marking dirty
            log.warning("hygiene nudge for %s failed: %s", c["task"], e)
        return {"action": "marked_dirty", "reason": "no way to reach an agent on " + repr(w.get("name"))}
