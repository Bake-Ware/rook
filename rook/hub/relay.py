"""Relayed approvals: one list of everything waiting on a person, and
approving or denying it on their behalf through an agent they trust
(docs/design/permissions.md 3.12).

These kinds of item wait on a person:

* ``hold``: a held cap call (:mod:`rook.hub.approvals`);
* ``job``: a held call a job run made (a hold whose on-behalf-of chain has
  ``job:<id>``);
* ``card``: an action card proposed in a chaired huddle
  (:mod:`rook.hub.plugins.huddle.chair`);
* ``ask``: a question an agent pinned in a huddle;
* ``house``: a one-line ask from the house agent's front door (Yes / No /
  Explain; rook/hub/plugins/home/front.py), with ``line`` and ``explain``;
* ``trust``: a pending trust-map proposal (rook/hub/trust.py). Only the person
  decides one: a tap on its phone card, Manage > Trust, or their sure voice
  with the read-back (rook/hub/plugins/home/cardpass.py). No relay does.

:meth:`Relay.items` lists them, each with a speakable name, who asked, what
exactly would happen (one plain sentence), the risk tier, where it came from
and its age; :func:`summary` turns that into a line short enough to speak.

**Relaying** needs ``relayed_by {who, quote, session}`` (the person, their own
words, the conversation; an agent never relays its own judgement) and an item
that is not dashboard-only (:data:`FLOOR`, ``approvals.relay_dashboard_only``).
Phones, voice and the chair relay once the person ticked "May relay my
approvals" (Tokens page; :meth:`ApprovalQueue.set_relayer`, out of agents'
reach). Agent clients need that tick only with advanced security on; off,
their quote is the consent, never on a hard stop (:func:`own_tap`).

Every relayed decision writes an append-only row in ``approvals.db``
(``relays``): the relaying identity, the person, the quote and the session.
The dashboard shows it beside the hold, card or question ("approved by Bake
via Claude web: ..."), and the requester sees the same line: a denied call's
reply carries the reason, a card's proposer is told in the room, an asker
gets the answer with the attribution.
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import re
import time
from typing import Any, Callable

from ..core import authz as core_authz
from ..core import names
from ..core.text import clip
from . import plain as _plain

log = logging.getLogger("rook.hub.relay")

KINDS = ("hold", "job", "card", "ask", "house", "trust")
DECISIONS = ("approve", "deny")

#: Never relayed, whatever ``approvals.relay_dashboard_only`` says: secret and
#: vault actions, who may do what (tokens, policy, settings, members, the
#: approvals and relay machinery) and job locks. Selectors as in
#: :func:`matches`.
FLOOR = ("secret.*", "vault.*", "token.*", "tokens.*", "policy.*", "settings.*", "member.*",
         "approvals.*", "approval.*", "relay.*", "trust.*", "guidance.write", "band.admin",
         "band.deauth",
         "job.write:lock", "job.write:unlock")

WHO_MAX, QUOTE_MAX, SESSION_MAX, REASON_MAX = 120, 1000, 500, 500
SUMMARY_ITEMS = 5
SENTENCE_MAX = 220
_ROLE = re.compile(r"role:([a-z][a-z0-9_-]{0,31})")


#: Why no relay decides a trust proposal (:func:`own_tap`).
TRUST_OWN = "a trust change: trust changes need the person's own tap or voice"


class RelayError(ValueError):
    """A relay refused; the text says why and what to do instead."""


# -- the rules ---------------------------------------------------------------------

def _tier_at_least(tier: str, floor: str) -> bool:
    t = core_authz.norm_tier(floor)
    return t is not None and core_authz.tier_rank(tier or "exec") >= core_authz.tier_rank(t)


def matches(sel: str, item: dict) -> bool:
    """Whether one dashboard-only selector matches an item's facts
    (``kind``, ``cap``, ``action``, ``tier``, ``tags``, ``hub``,
    ``hold_kind``, ``card_kind``)."""
    sel = str(sel or "").strip()
    if not sel:
        return False
    cap = str(item.get("cap") or "")
    if sel.startswith("tier:"):
        return bool(cap) and _tier_at_least(item.get("tier") or "", sel[5:])
    if sel.startswith("hub:"):
        return bool(item.get("hub")) and _tier_at_least(item.get("tier") or "", sel[4:])
    if sel.startswith("tag:"):
        return sel[4:] in (item.get("tags") or ())
    if sel.startswith("hold:"):
        return item.get("hold_kind") == sel[5:]
    if sel.startswith("card:"):
        return item.get("card_kind") == sel[5:]
    if sel.startswith("kind:"):
        return item.get("kind") == sel[5:]
    if not cap:
        return False
    pat, _, act = sel.partition(":")
    if not fnmatch.fnmatchcase(cap, pat):
        return False
    return not act or fnmatch.fnmatchcase(str(item.get("action") or ""), act)


def refusal(item: dict, dashboard_only: tuple | list) -> str:
    """Why an item may not be relayed ("" when it may)."""
    for sel in FLOOR:
        if matches(sel, item):
            return "never relayed (secrets, tokens, policy, settings, approvals and job locks " \
                   "need the dashboard)"
    for sel in dashboard_only or ():
        if matches(sel, item):
            return f"dashboard only ({sel}, setting approvals.relay_dashboard_only)"
    return ""


def own_tap(item: dict) -> str:
    """Why only the person's own tap decides ``item`` ("": an agent may relay): core holds
    are all hard stops; a house ask's Yes approves the holds its call or request meets."""
    house = item.get("kind") == "house" and (item.get("cap") or item.get("request")
                                             or item.get("hard_stop"))
    if item.get("kind") == "trust":
        return TRUST_OWN
    return "a hard stop (their own tap)" if house or item.get("kind") in ("hold", "job") else ""


def is_client(p: Any) -> bool:
    """An MCP agent token talking with the person (not the house agent, a role or a job)."""
    return getattr(p, "kind", "") == "token" and not getattr(p, "via", ()) \
        and not str(getattr(p, "id", "")).startswith("home:")


def relayed_by(raw: Any) -> dict:
    """``{who, quote, session}`` cleaned, or :class:`RelayError`."""
    if not isinstance(raw, dict):
        raise RelayError("relayed_by {who, quote, session} is required: the person who decided, "
                         "their own words, and the conversation (URL or id) they said them in. "
                         "Relay only the person's explicit decision, never your own judgement")
    out = {}
    for key, limit in (("who", WHO_MAX), ("quote", QUOTE_MAX), ("session", SESSION_MAX)):
        val = raw.get(key)
        val = " ".join(str(val).split()) if isinstance(val, (str, int, float)) else ""
        if not val:
            raise RelayError(f"relayed_by.{key} is required (who: the person; quote: their own "
                             "words; session: the conversation URL or id)")
        out[key] = val[:limit]
    if len(out["quote"]) < 2:
        raise RelayError("relayed_by.quote: the person's own words, not a placeholder")
    return out


# -- describing items -----------------------------------------------------------------

def _age(secs: float) -> str:
    s = max(0, int(secs))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{round(s / 60)}m"
    if s < 2 * 86400:
        return f"{round(s / 3600)}h"
    return f"{round(s / 86400)}d"


def _args_text(cap: str, args: Any) -> str:
    """The concrete part of a call, short: a command line for shell-like
    caps, else compact JSON (secrets were redacted when the hold was made)."""
    if not isinstance(args, dict) or not args:
        return ""
    for key in ("argv", "cmd", "command"):
        v = args.get(key)
        if isinstance(v, list) and v:
            return "`" + " ".join(str(x) for x in v) + "`"
        if isinstance(v, str) and v.strip():
            return "`" + v.strip() + "`"
    if cap == "task.write" and isinstance(args.get("data"), dict):
        d = args["data"]
        what = d.get("title") or (d.get("patch") or {}).get("state")
        if what:
            return f'{args.get("action") or "write"} "{what}"'
    return json.dumps(args, ensure_ascii=False, default=str, separators=(",", ":"))


def _sentence(*parts: str) -> str:
    return clip(" ".join(p for p in parts if p).strip(), SENTENCE_MAX)


def _role_of(*idents: Any) -> str | None:
    for ident in idents:
        m = _ROLE.search(str(ident or ""))
        if m:
            return m.group(1)
    return None


def summary(items: list[dict]) -> str:
    """One line for voice: ``3 waiting: Restart nginx (Claude web, for Cert
    renewal; brave-otter-lamp); ...``. Never any secret: sentences come from
    redacted args."""
    if not items:
        return "Nothing is waiting for you."
    head = f"{len(items)} waiting" if len(items) > 1 else "1 waiting"
    parts = []
    for it in items[:SUMMARY_ITEMS]:
        tag = "" if it.get("relayable", True) else ", dashboard only"
        p = it.get("plain")
        if p:
            # Spoken: a title from the cap and machine only, never the args.
            title = p["title"] if it["kind"] in ("ask", "house") or not it.get("cap") else \
                _plain.safe_title(it["cap"], str(it.get("worker") or ""), hub=bool(it.get("hub")))
            parts.append(f"{title} ({p['who']}, for {p['for']}; "
                         f"{it.get('name') or it['id']}{tag})")
        else:
            parts.append(f"{it.get('name') or it['id']}: {it['who']} wants to {it['what']}{tag}")
    more = f"; and {len(items) - SUMMARY_ITEMS} more" if len(items) > SUMMARY_ITEMS else ""
    return f"{head}: " + "; ".join(parts) + more + "."


# -- the service ----------------------------------------------------------------------

class Relay:
    """The unified list and relayed decisions for one hub node.

    ``node()`` returns the hub node: its band client's authorizer has the
    approval queue (``client.authz.approvals``), and its ``huddle`` and
    ``job`` plugins have cards, questions and job names."""

    def __init__(self, node: Callable[[], Any], clock: Callable[[], float] = time.time) -> None:
        self._node = node
        self.clock = clock

    # -- plumbing ----------------------------------------------------------
    def approvals(self) -> Any:
        node = self._node()
        authz = getattr(getattr(node, "client", None), "authz", None)
        return getattr(authz, "approvals", None)

    def _plugin(self, ns: str) -> Any:
        node = self._node()
        return node.plugin(ns) if node is not None else None

    def huddles(self) -> Any:
        p = self._plugin("huddle")
        return getattr(p, "service", None)

    def dashboard_only(self) -> tuple:
        ap = self.approvals()
        if ap is None:
            from .approvals import Config
            return Config().relay_dashboard_only
        return ap.config().relay_dashboard_only

    def advanced(self) -> bool:
        """Advanced security is on (rook/hub/plugins/security): relaying needs the tick."""
        from .plugins.security import for_node
        return for_node(self._node())

    def may_relay(self, principal: str) -> bool:
        ap = self.approvals()
        return bool(ap is not None and principal and ap.queue.relayer(principal) is not None)

    def _job_name(self, jid: str) -> str:
        jobs = self._plugin("job")
        with contextlib.suppress(Exception):
            row = jobs.store.get_job(jid) if jobs is not None else None
            if row:
                return str(row.get("name") or jid)
        return jid

    def _hub_worker(self, worker: str) -> bool:
        return str(worker or "").lower() in ("rook", "hub")

    # -- the list ------------------------------------------------------------
    def items(self, kinds: tuple | list | None = None) -> list[dict]:
        """Everything waiting on a person, oldest first. Each item:
        ``{kind, id, name, who, what, risk, from, age, age_s, relayable,
        why_not?}`` plus the facts the dashboard-only rules match on."""
        want = set(kinds or KINDS)
        out: list[dict] = []
        if want & {"hold", "job"}:
            out += [i for i in self._holds() if i["kind"] in want]
        svc = self.huddles()
        if svc is not None:
            titles = {}
            with contextlib.suppress(Exception):
                titles = {h["room"]: h for h in svc.huddles(archived=True)}
            if "card" in want:
                out += self._cards(svc, titles)
            if "ask" in want:
                out += self._asks(svc, titles)
        if "house" in want:
            out += self._house()
        if "trust" in want:
            out += self._proposals()
        rules = self.dashboard_only()
        for it in out:
            why = refusal(it, rules)
            it["relayable"] = not why
            if why:
                it["why_not"] = why
            try:
                it["plain"] = _plain.describe(it)
            except Exception:  # noqa: BLE001 - a describing bug never hides an item
                log.exception("relay: describing %s %s", it.get("kind"), it.get("id"))
        out.sort(key=lambda i: i["created"])
        return out

    def _holds(self) -> list[dict]:
        ap = self.approvals()
        if ap is None:
            return []
        now = self.clock()
        out = []
        for h in ap.queue.pending():
            if h.get("expires") and h["expires"] < now:
                continue
            via = [str(v) for v in h.get("via") or []]
            job = next((v[4:] for v in via if v.startswith("job:")), None)
            who = h.get("principal_label") or h.get("principal") or "an agent"
            frm: dict = {}
            role = _role_of(h.get("principal"), h.get("principal_label"), *via)
            if role:
                frm["role"] = role
            if job:
                frm["job"] = self._job_name(job)
                who = f"job {frm['job']}"
            worker = h.get("target_name") or h.get("target_id") or "the band"
            args = _args_text(h["cap"], h.get("args"))
            why = f" (held: {h['reason']})" if h.get("reason") else ""
            action = (h.get("args") or {}).get("action") if isinstance(h.get("args"), dict) else None
            out.append({
                "kind": "job" if job else "hold", "id": h["id"], "name": h.get("name"),
                "who": who, "what": _sentence(f"run {h['cap']} on {worker}" + (":" if args else ""),
                                              args) + why,
                "risk": h.get("tier") or "exec", "from": frm,
                "created": h["created"], "age_s": int(now - h["created"]),
                "age": _age(now - h["created"]),
                "cap": h["cap"], "action": action, "tier": h.get("tier") or "exec",
                "tags": list(h.get("tags") or []), "hub": bool(h.get("is_hub")),
                "hold_kind": h.get("kind"), "worker": worker,
                # for the plain ask (rook/hub/plain.py); never in public()
                "args": h.get("args") or {}, "reason": h.get("reason") or "",
                "rule": h.get("rule"), "principal": h.get("principal"), "via": via,
                "context": h.get("context") or {}})
        return out

    def _roster(self) -> dict:
        node = self._node()
        with contextlib.suppress(Exception):
            return dict(getattr(getattr(node, "client", None), "workers", {}) or {})
        return {}

    def _hub_hosts(self) -> set:
        """Names and ids of workers on the hub's own machine (they can edit
        approvals.db): a card's call there is treated like a hub-host hold."""
        ap = self.approvals()
        if ap is None:
            return set()
        with contextlib.suppress(Exception):
            got = ap.hub_host_workers(ap.config())
            return {str(x).lower() for w in got.get("workers") or []
                    for x in (w.get("worker"), w.get("id")) if x}
        return set()

    def _cards(self, svc: Any, titles: dict) -> list[dict]:
        now = self.clock()
        out = []
        roster = self._roster()
        by_name = {str(w.get("name") or wid).lower(): w for wid, w in roster.items()}
        hosts = self._hub_hosts()
        for c in svc.store.pending_cards():
            if c["kind"] == "note":
                continue          # a note runs nothing: it never waits on the person
            h = titles.get(c["room"]) or {}
            call = c.get("call") or {}
            cap = str(call.get("cap") or "")
            args = call.get("args") or {}
            worker = str(call.get("worker") or "")
            entry = roster.get(worker) or by_name.get(worker.lower()) or {}
            declared = (entry.get("tiers") or {}).get(cap)
            tier = core_authz.effective_tier(cap, declared) if cap else "write"
            hub_host = bool(cap) and worker.lower() in hosts \
                and core_authz.tier_rank(tier) >= core_authz.tier_rank("exec")
            if cap:
                what = _sentence(f"{c['summary'].rstrip('.')}: runs {cap} on {call.get('worker')}"
                                 + (":" if _args_text(cap, args) else ""), _args_text(cap, args))
            else:
                what = _sentence(f"note: {c['summary']}")
            role = _role_of(c.get("by"))
            frm = {"huddle": h.get("name") or c["room"], "huddle_title": h.get("title") or "",
                   **({"role": role} if role else {})}
            out.append({
                "kind": "card", "id": c["id"], "name": c.get("name"), "who": c.get("by") or "",
                "what": what, "risk": tier if cap else "note", "from": frm,
                "created": c["created"], "age_s": int(now - c["created"]),
                "age": _age(now - c["created"]), "room": c["room"],
                "cap": cap, "action": args.get("action") if isinstance(args, dict) else None,
                "tier": tier, "tags": list(core_authz.builtin_tags(cap)) if cap else [],
                "hub": self._hub_worker(call.get("worker")) or hub_host, "card_kind": c["kind"],
                "hold_kind": "hub-host" if hub_host else None,
                "worker": call.get("worker"), "summary": c["summary"], "args": args,
                "context": {"huddle": h.get("title") or h.get("name") or c["room"]}})
        return out

    def _asks(self, svc: Any, titles: dict) -> list[dict]:
        now = self.clock()
        out = []
        for q in svc.store.open_questions():
            h = titles.get(q["room"]) or {}
            opts = q.get("options") or []
            role = _role_of(q.get("asker"))
            frm = {"huddle": h.get("name") or q["room"], "huddle_title": h.get("title") or "",
                   **({"role": role} if role else {})}
            out.append({
                "kind": "ask", "id": f"q{q['id']}", "name": q.get("name"), "who": q["asker"],
                "what": _sentence(f'get an answer: "{q["text"]}"'
                                  + (f" (options: {' / '.join(opts)})" if opts else "")),
                "risk": "question", "from": frm, "created": q["asked"],
                "age_s": int(now - q["asked"]), "age": _age(now - q["asked"]),
                "question": q["id"], "room": q["room"], "options": opts,
                "question_text": q["text"]})
        return out

    def _house(self) -> list[dict]:
        home = self._plugin("home")
        front = getattr(home, "front", None)
        if front is None or not getattr(home, "enabled", lambda: False)():
            return []
        try:
            items = front.relay_items()
        except Exception:  # noqa: BLE001 - the list failing never sinks the others
            log.exception("relay: listing the house agent's asks failed")
            return []
        for it in items:
            it["age"] = _age(it["age_s"])
        return items

    def trust(self) -> Any:
        from .plugins.trust import node_store
        return node_store(self._node())

    def _proposals(self) -> list[dict]:
        """Pending trust-map proposals, each with the plain sentence the person
        hears before saying yes (rook/hub/trust.py ``describe_change``)."""
        try:
            st = self.trust()
            doc = st.current() if st is not None else {}
        except Exception:  # noqa: BLE001 - the map failing never sinks the others
            log.exception("relay: reading the trust map's proposals failed")
            return []
        from .trust import describe_change
        now = self.clock()
        out = []
        for prop in doc.get("proposals") or ():
            if prop.get("state") != "pending":
                continue
            d = describe_change(doc, prop.get("change") or {}, now)
            at = float(prop.get("at") or 0)
            out.append({
                "kind": "trust", "id": str(prop["id"]), "who": prop.get("by") or "an agent",
                "principal": prop.get("principal"), "what": _sentence(d["sentence"]),
                "risk": "loosens" if d["loosens"] else "stricter", "from": {},
                "created": at, "age_s": int(now - at), "age": _age(now - at),
                "sentence": d["sentence"], "loosens": d["loosens"], "sacred": d["sacred"],
                "proposal": dict(prop), "context": {"why": str(prop.get("why") or "")},
                "explain": "The change: " + json.dumps(prop.get("change") or {}, default=str,
                                                       separators=(",", ":"))})
        return out

    @staticmethod
    def public(item: dict) -> dict:
        """An item as agents and the voice summary see it (no internal facts)."""
        keys = ("kind", "id", "name", "plain", "who", "what", "risk", "from", "age", "relayable",
                "why_not", "options", "line", "explain")
        out = {k: item[k] for k in keys if item.get(k) not in (None, "", [], {})
               or k == "relayable"}
        if item.get("kind") == "trust":
            # Listed so agents can tell the person; never theirs to relay (own_tap).
            out.update(relayable=False, why_not=TRUST_OWN)
        if isinstance(out.get("plain"), dict):
            # Explain and the command (cap, args, trust cite) stay on the dashboard and the phone.
            out["plain"] = {k: v for k, v in out["plain"].items() if k not in ("explain", "command")}
        return out

    def find(self, ref: str) -> dict:
        """A waiting item by id or speakable name (any case, spaces or
        hyphens), or :class:`RelayError` naming the waiting items."""
        ref = str(ref or "").strip()
        if not ref:
            raise RelayError("item: the id or name of a waiting item (rook_approvals list)")
        items = self.items()
        norm = names.normalize(ref)
        for it in items:
            if ref == it["id"] or (norm and it.get("name") == norm):
                return it
        hint = ", ".join(f"{i.get('name') or i['id']}" for i in items[:8]) or "nothing is waiting"
        raise RelayError(f"no waiting item {ref!r} (it may be decided or expired already). "
                         f"Waiting: {hint}")

    # -- deciding ------------------------------------------------------------------
    async def decide(self, ref: str, decision: str, said: Any, *, relayer: str,
                     relayer_label: str = "", reason: str = "", answer: str = "",
                     principal: Any = None, client: bool = False, tap: bool = False) -> dict:
        """Approve or deny one waiting item on the person's behalf.
        ``relayer`` is the verified principal id of the relaying identity
        (``token:<agent_id>``, or the house agent's identity); ``client``: an agent client;
        ``tap``: the person's own tap on a phone card (the attention service, in process:
        never set over the wire), the only relayed path that decides a trust proposal."""
        if decision not in DECISIONS:
            raise RelayError("decision: approve or deny")
        said = relayed_by(said)
        client = client and not self.advanced()
        if not client and not self.may_relay(relayer):
            raise PermissionError(
                f"{relayer_label or relayer} may not relay approvals: the person turns this on "
                "for a client on the dashboard (Tokens > May relay my approvals). Ask them to "
                "decide on the dashboard's Approvals page instead")
        it = self.find(ref)
        if it["kind"] == "trust" and not tap:
            raise PermissionError(f"{it['id']} is a trust change: trust changes need "
                                  f"{said['who']}'s own tap or voice (the phone card, the "
                                  "dashboard's Manage > Trust, or \"clear my cards\" by voice). "
                                  "Tell them it is waiting; don't relay it")
        why = refusal(it, self.dashboard_only()) or (own_tap(it) if client else "")
        if why:
            raise PermissionError(f"{it.get('name') or it['id']} is {why}: ask the person to "
                                  "decide it on the dashboard")
        reason = " ".join(str(reason or "").split())[:REASON_MAX]
        label = relayer_label or relayer
        p = it.get("plain") or {}
        rel = {"relayer": relayer, "relayer_label": label, **said, "reason": reason,
               "summary": f"{p['title']}: {p['want']}" if p else it["what"]}
        by = f"{said['who']} via {label}"
        from .approvals import relay_text
        text = relay_text({**rel, "decision": decision})
        ap = self.approvals()
        if it["kind"] in ("hold", "job"):
            from .approvals import HoldError
            try:
                row = ap.decide(it["id"], decision == "approve", by, scope="call",
                                note=reason if decision == "deny" else "",
                                relay={**rel, "kind": it["kind"]})
            except HoldError as e:
                raise RelayError(str(e)) from None
            out = {"state": row["state"]}
        elif it["kind"] == "card":
            svc = self.huddles()
            if ap is None:
                raise RelayError("relaying needs the approval queue (approvals.db)")
            # The card's call runs as the relaying agent, never as a person:
            # the band's policy, its holds and the hub's admin gates apply to
            # it as to any call that agent makes. The person's word only
            # replaces the click on the card. Recorded before it runs.
            runner = self._runner(principal, relayer, by)
            ap.queue.relay_add({**rel, "kind": "card", "item": it["id"], "decision": decision})
            res = await svc.card(it["room"], it["id"], "approve" if decision == "approve"
                                 else "dismiss", relayer, runner,
                                 relay={**rel, "label": by, "text": text})
            out = {"state": res.get("state"), **({"error": res["error"]} if res.get("error")
                                                 else {})}
        elif it["kind"] == "trust":
            from .trust import Actor, TrustError
            st = self.trust()
            if st is None:
                raise RelayError("the trust map is unavailable on this hub")
            try:
                st.change("decide_proposal", {"id": it["id"], "accept": decision == "approve"},
                          Actor("person", said["who"], principal=relayer, words=said["quote"],
                                session=said["session"], via=label))
            except TrustError as e:
                raise RelayError(str(e)) from None
            if ap is not None:
                ap.queue.relay_add({**rel, "kind": "trust", "item": it["id"],
                                    "decision": decision})
            out = {"state": "accepted" if decision == "approve" else "rejected"}
        elif it["kind"] == "house":
            front = getattr(self._plugin("home"), "front", None)
            if front is None:
                raise RelayError("the house agent is off on this hub")
            if ap is not None:
                ap.queue.relay_add({**rel, "kind": "house", "item": it["id"],
                                    "decision": decision})
            res = await front.answer(it["id"], "yes" if decision == "approve" else "no", by=by,
                                     quote=said["quote"], relay={**rel, "kind": "house"},
                                     reason=reason)
            out = {"state": res.get("answer"), **({"note": res["note"]} if res.get("note")
                                                  else {}),
                   **({"remember": res["remember"]} if res.get("remember") else {})}
        else:
            svc = self.huddles()
            if ap is None:
                raise RelayError("relaying needs the approval queue (approvals.db)")
            words = " ".join(str(answer or "").split())
            if not words:
                words = "Yes" if decision == "approve" else "No"
                if reason:
                    words += f": {reason}"
            ap.queue.relay_add({**rel, "kind": "ask", "item": it["id"], "decision": decision})
            res = await svc.answer(it["question"], words, f"relay:{relayer}", by, relayed=text)
            out = {"state": "answered", "answer": words, "wake": res.get("wake")}
        self._journal(it, decision, rel, out)
        return {"item": it["id"], "name": it.get("name"), "kind": it["kind"],
                "decision": decision, **out, "recorded": text}

    @staticmethod
    def _runner(principal: Any, relayer: str, label: str) -> Any:
        """The principal a relayed card's call runs as: the relaying agent's
        own (verified) principal, else one built for it as an agent."""
        from dataclasses import replace
        from .policy import Principal
        if principal is not None and getattr(principal, "id", None) == relayer:
            return replace(principal, label=label)
        kind = relayer.split(":", 1)[0] if ":" in relayer else "agent"
        return Principal(relayer, kind if kind in ("token", "agent") else "agent", "agent",
                         label=label)

    def _journal(self, it: dict, decision: str, rel: dict, out: dict) -> None:
        node = self._node()
        j = getattr(node, "journal", None)
        if j is None or it["kind"] in ("hold", "job"):   # holds journal approval.decide
            return
        with contextlib.suppress(Exception):
            j.record(cap="approval.relay", worker="rook", identity=rel["relayer"],
                     args={"item": it["id"], "kind": it["kind"], "decision": decision},
                     reply={"ok": True, "result": {**out, "relayed_by": {
                         k: rel[k] for k in ("who", "quote", "session")},
                         **({"reason": rel["reason"]} if rel.get("reason") else {})}},
                     thread_id=it.get("room"))

    # -- attribution for views ---------------------------------------------------------
    def relayed(self, kind: str | None, ids: list) -> dict[str, dict]:
        ap = self.approvals()
        if ap is None:
            return {}
        with contextlib.suppress(Exception):
            return ap.queue.relays_for(kind, [str(i) for i in ids])
        return {}
