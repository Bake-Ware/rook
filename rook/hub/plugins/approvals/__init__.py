"""``approvals.*``: what is waiting on the person, and relaying their
decisions (worker ``rook``; :mod:`rook.hub.relay`, permissions.md 3.12).

* ``approvals.list`` (read): every held call, held job call, huddle card
  waiting for approval and pinned huddle question, with a speakable name,
  who asked, what exactly would happen, the risk tier, where it came from and
  its age; ``summary`` is a line short enough to speak.
* ``approvals.relay`` (write): record the person's own decision on one item
  with ``relayed_by {who, quote, session}``, by the rules in
  :mod:`rook.hub.relay` (never a hard stop; with advanced security on, only
  clients they ticked list and relay). Recorded append-only beside the item.

The ``rook_approvals`` MCP tool routes to both.
"""
from __future__ import annotations

import json

from ....core.plugin import Plugin, capability, place
from ...relay import KINDS, Relay, RelayError, is_client, summary

ACTIONS = ("list", "summary", "approve", "deny")
ERRORS = (ValueError, KeyError, TypeError, PermissionError, LookupError)
NOT_ENABLED = ("{who} may not see or relay the person's approvals: with advanced security on, "
               "they turn this on for a client on the dashboard (Tokens > May relay my approvals)")


def _caller():
    """The verified principal of this call: its id and label, or None."""
    from ...authz import current_principal
    p = current_principal.get()
    if p is None or not getattr(p, "verified", False) or p.kind in ("band", "unverified"):
        return None
    return p


class Approvals(Plugin):
    NAMESPACE = "approvals"
    NAME = "approvals"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    GUIDANCE = {
        "tool:rook_approvals": "",
        "cap:approvals.": "approvals.list {kind?, summary?} and approvals.relay {item, decision, "
                          "relayed_by {who, quote, session}, reason?, answer?}: see rook_approvals.",
    }
    SKILL = ("### approvals (relayed)\n"
             "`rook_approvals(action=\"summary\")` says what is waiting on the person (held calls, "
             "held job calls, huddle cards, pinned questions); `action=\"list\"` gives each item's "
             "name, who asked, what exactly would happen, risk, source and age. Read it back "
             "tersely: count, then name + one line each. When the person tells you their "
             "decision (also on something you did on their word), record it: "
             "`rook_approvals(action=\"approve\"|\"deny\", item=<name or id>, relayed_by={\"who\": "
             "<person>, \"quote\": <their exact words>, \"session\": <conversation URL or id>}, "
             "reason=<why, for a deny>)`. Never relay your own judgement, never decide items "
             "they did not name, and leave held calls and dashboard-only items to them; withdraw "
             "your own question that no longer needs them (rook_huddle withdraw).\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None
        self.relay = Relay(lambda: self._node)

    def bind_host(self, node) -> None:
        self._node = node

    def _may_see(self, p) -> bool:
        if p is None:
            return False
        if p.kind in ("human", "system") or is_client(p) and not self.relay.advanced():
            return True
        return self.relay.may_relay(p.id)

    # -- caps ----------------------------------------------------------------
    @capability("list", risk="read")
    def list_(self, kind: str | None = None, summary_only: bool = False) -> dict:
        """What is waiting on the person: held calls, held job calls, huddle cards and pinned questions (kind: hold|job|card|ask; summary_only: just the spoken line)."""
        p = _caller()
        if p is not None and tuple(p.via or ()):
            p = None                      # a job run never reads the person's queue
        if not self._may_see(p):
            raise PermissionError(NOT_ENABLED.format(who=getattr(p, "label", "") or
                                                     getattr(p, "id", "this caller")))
        kinds = None
        if kind:
            kinds = [k.strip() for k in str(kind).split(",") if k.strip()]
            bad = [k for k in kinds if k not in KINDS]
            if bad:
                raise ValueError("kind: one or more of " + ", ".join(KINDS))
        items = self.relay.items(kinds)
        out = {"count": len(items), "summary": summary(items)}
        if not summary_only:
            out["items"] = [Relay.public(i) for i in items]
        return out

    @capability("relay", risk="write")
    async def relay_(self, item: str, decision: str, relayed_by: dict | None = None,
                     reason: str = "", answer: str = "") -> dict:
        """Approve or deny one waiting item on the person's behalf: relayed_by {who, quote, session} is their own decision; reason (deny) goes back to the requester; answer: the reply to a question."""
        p = _caller()
        if p is None or p.kind in ("human", "system"):
            raise PermissionError("approvals.relay is for an agent the person enabled; people "
                                  "decide on the dashboard's Approvals page")
        if tuple(p.via or ()):
            # A job (or anything acting on someone's behalf) never relays: only
            # the client in conversation with the person hears their words.
            raise PermissionError("approvals.relay is refused on behalf of " + ", ".join(
                str(v) for v in p.via) + ": only a client talking with the person relays")
        try:
            return await self.relay.decide(item, decision, relayed_by, relayer=p.id,
                                           relayer_label=p.label or p.id, reason=reason,
                                           answer=answer, principal=p, client=is_client(p))
        except RelayError as e:
            raise ValueError(str(e)) from None

    # -- MCP -------------------------------------------------------------------
    def mcp_tools(self, invoke):
        async def rook_approvals(action: str = "summary", item: str | None = None,
                                 relayed_by: dict | None = None, reason: str | None = None,
                                 answer: str | None = None, kind: str | None = None) -> str:
            """What waits on the person (holds, job holds, huddle cards, questions).
            summary | list (kind?). approve/deny item=<name|id> with relayed_by {who, quote:
            their exact words, session}, reason?, answer?: only what they said (never held
            calls); never your own judgement."""
            try:
                if action in ("list", "summary"):
                    result = await invoke("approvals.list", {
                        k: v for k, v in {"kind": kind,
                                          "summary_only": action == "summary"}.items()
                        if v is not None})
                elif action in ("approve", "deny"):
                    result = await invoke("approvals.relay", {
                        "item": item or "", "decision": action, "relayed_by": relayed_by,
                        "reason": reason or "", "answer": answer or ""})
                else:
                    raise ValueError("action must be one of " + ", ".join(ACTIONS))
                return json.dumps({"ok": True, "result": result}, separators=(",", ":"),
                                  ensure_ascii=False, default=str)
            except ERRORS as error:
                return json.dumps({"ok": False, "error": str(error),
                                   "code": type(error).__name__}, separators=(",", ":"))
        return [rook_approvals]


PLUGIN = Approvals
