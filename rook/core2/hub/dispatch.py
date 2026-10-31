"""Hub dispatch: the one path every door ends at before a ticket exists
(spec 10 of docs/spec/core-v2.md).

    d = Dispatcher(hub_id=..., op_key=SigningKey, identities=Identities(),
                   workers={worker_id: Worker(...)}, send=transport, journal=store.append)
    s = d.start_session(caller, caps=["shell.*"], project="castling", task="t_…")
    reply = await d.dispatch(caller, Call(cap="shell.exec", target="kaiju",
                                          args={...}, why="...", session=s.id))
    # {"id", "ok", "result" | "error"}; a held call: {"held": True, "hold": name}
    await d.release(name, by="person:bake", present=True)   # runs the exact held call

Order (10.1): door, caller, session, target and cap, why, rights (the
intersection of person, agent, session, extra layers such as plugin ``uses``
or a page's caps, and the target's ceiling), gates, holds, ticket, journal.
Every call that reaches dispatch gets one journal entry; a held call gets a
second when it is released or refused.

Collaborators are plain callables so nothing here knows the transport or the
database:

- ``send(band, request) -> reply``: async; delivers a request to its target
  and returns the reply. TODO(castling:wiring): the band connection.
- ``journal(entry)``: stores an entry; the store assigns ``id``, ``chain``,
  ``prev`` and ``hash`` (11.3). TODO(castling:journal): journal_store.append.
- ``secrets(args, caller) -> args``: substitutes secret references for the
  wire; the gates and the journal only ever see the unresolved args.
"""
from __future__ import annotations

import asyncio
import copy
import secrets as _secrets
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from rook.kit._types import parse_duration

from .. import rights, wire
from . import gate as gates_mod
from .identity import Caller, Identities

SESSION_SECS = 12 * 3600
TICKET_SECS = 60
DEFAULT_TIMEOUT = 15.0
TIMEOUT_GRACE = 5.0
REMINT = ("denied by worker: expired", "denied by worker: not_yet_valid")
NO_RETRY = {"destructive", "physical"}
# The one component that may release a gate flag on its own: the security
# plugin's triage step (a flag of its own gate only; never a hard stop).
TRIAGE = ("system:plugin.security", "security")


@dataclass
class Worker:
    """A worker as its announce describes it to this hub."""
    id: str
    name: str
    band: str
    caps: dict                                       # cap -> manifest cap data
    ceiling: dict                                    # this hub's ceiling on it (rights)
    appliances: dict = field(default_factory=dict)  # name -> {cap: manifest cap data}
    uses: dict = field(default_factory=dict)        # plugin -> [pattern] (manifest ``uses``)
    sacred: bool = False


@dataclass
class Session:
    id: str
    person: str | None
    agent: str | None
    caps: list
    project: str | None
    task: str | None
    started: float
    exp: float
    tainted: bool = False
    ended: bool = False

    def view(self) -> dict:
        return {"id": self.id, "caps": list(self.caps), "project": self.project,
                "task": self.task, "tainted": self.tainted}


@dataclass
class Call:
    """What a door hands dispatch. ``target`` is a worker id or name."""
    cap: str
    target: str
    why: str
    session: str
    args: dict | None = None
    appliance: str | None = None
    elevate: bool = False
    door: str | None = None
    layers: list = field(default_factory=list)  # extra [[name, rights]]: plugin uses, page caps


@dataclass
class Hold:
    name: str
    caller: Caller
    call: Call
    session: Session
    worker: Worker
    meta: dict
    entry: dict
    hard: list           # hard stops: only the person releases
    new_cap: bool        # approve_new: an owner releases
    flagged_by: list     # gates that flagged it

    def reasons(self) -> list:
        return self.hard + (["new cap"] if self.new_cap else []) + [f"gate {g}" for g in self.flagged_by]


class Dispatcher:
    def __init__(self, *, hub_id: str, op_key, identities: Identities, workers: dict,
                 send: Callable[[str, dict], Awaitable[dict]], journal: Callable[[dict], Any],
                 gates: list | None = None, secrets: Callable[[Any, Caller], Any] | None = None,
                 hubkey: dict | None = None, clock: Callable[[], float] = time.time):
        self.hub_id, self.op_key, self.hubkey = hub_id, op_key, hubkey
        self.identities, self.workers = identities, workers
        self.send, self.journal, self.secrets, self.clock = send, journal, secrets, clock
        self.gates: list = list(gates or ())
        self.sessions: dict[str, Session] = {}
        self.holds: dict[str, Hold] = {}
        self.closed_doors: set[str] = set()
        self._inflight: dict[str, tuple] = {}   # nonce -> (ticket, caller, session id, extra layers)
        self.leases: dict[str, tuple[str, str, int]] = {}   # lease id -> (worker id, session id, exp)

    # -- sessions (8.4) ----------------------------------------------------

    def start_session(self, caller: Caller, caps: list, project: str | None = None,
                      task: str | None = None) -> Session:
        if caller.problem():
            raise ValueError(f"caller: {caller.problem()}")
        if not isinstance(caps, list) or not all(isinstance(c, str) and c for c in caps):
            raise ValueError("caps must be a list of patterns")
        now = self.clock()
        s = Session(_secrets.token_hex(16), caller.person, caller.agent, list(caps), project, task,
                    now, now + SESSION_SECS)
        self.sessions[s.id] = s
        self._note("session", s, event="start", caps=list(caps), task=task)
        return s

    def end_session(self, sid: str) -> None:
        s = self.sessions.get(sid)
        if s and not s.ended:
            s.ended = True
            self._note("session", s, event="end")

    def lease_holders(self, sid: str) -> list[str]:
        """Workers holding a live lease for session ``sid``, forgotten here:
        the hub drops them there when the session ends (7.4)."""
        now = self.clock()
        for lid in [lid for lid, v in self.leases.items() if v[2] < now]:
            del self.leases[lid]
        held = [lid for lid, v in self.leases.items() if v[1] == sid]
        return sorted({self.leases.pop(lid)[0] for lid in held})

    def taint(self, sid: str) -> None:
        """An event with ``taints`` reached the session; never cleared."""
        if sid in self.sessions:
            self.sessions[sid].tainted = True

    # -- owner actions -----------------------------------------------------

    def set_gates(self, gates: list, by: str) -> None:
        """Replace the gate list (enable, disable, swap): a deliberate,
        journaled owner action."""
        if not self.identities.is_owner(by):
            raise PermissionError("only an owner changes gates")
        self.gates = list(gates)
        self._note("gates", None, by=by, gates=[[g.name, g.mode] for g in self.gates])

    def close_door(self, door: str, by: str) -> None:
        """The kill switch: refuse everything from one door (the MCP door)."""
        if not self.identities.is_owner(by) and not by.startswith("system:"):
            raise PermissionError("only an owner or the hub closes doors")
        self.closed_doors.add(door)
        self._note("door", None, by=by, door=door, open=False)

    def open_door(self, door: str, by: str) -> None:
        if not self.identities.is_owner(by):
            raise PermissionError("only an owner opens doors")
        self.closed_doors.discard(door)
        self._note("door", None, by=by, door=door, open=True)

    # -- dispatch (10.1) ---------------------------------------------------

    async def dispatch(self, caller: Caller, call: Call) -> dict:
        cid = _secrets.token_hex(16)
        entry = {"kind": "call", "ts": self.clock(), "call": cid, "hub": self.hub_id,
                 "cap": call.cap, "person": caller.person, "agent": caller.agent,
                 "session": call.session, "why": call.why, "args": call.args}
        out = await self._dispatch(caller, call, entry)
        self.journal(entry)
        return out

    async def _dispatch(self, caller: Caller, call: Call, entry: dict) -> dict:
        cid = entry["call"]
        # Our own copy: a held call must run exactly as asked, whatever the
        # door does with its objects afterwards.
        call = replace(call, args=copy.deepcopy(call.args), layers=copy.deepcopy(call.layers))
        entry["args"] = call.args

        def deny(error: str, layer: str | None = None) -> dict:
            entry.update(decision="deny", ok=False, error=error)
            if layer:
                entry["layer"] = layer
            return {"id": cid, "ok": False, "error": error}

        if call.door in self.closed_doors:
            return deny(f"denied: door {call.door} closed")
        # 1. caller
        if caller.problem():
            return deny(f"denied: caller: {caller.problem()}", "caller")
        # 2. session
        s = self.sessions.get(call.session)
        if s is None or s.ended or self.clock() > s.exp:
            return deny("denied: session: unknown or ended", "session")
        if (s.person, s.agent) != (caller.person, caller.agent):
            return deny("denied: session: belongs to another caller", "session")
        entry["project"] = s.project
        # 3. target and cap
        w = self.resolve(call.target)
        if w is None:
            return deny(f"unknown worker: {call.target}")
        caps = w.caps if call.appliance is None else w.appliances.get(call.appliance)
        if caps is None:
            return deny(f"unknown appliance: {call.appliance}")
        name = _resolve_alias(caps, call.cap)
        meta = caps.get(name)
        if meta is None:
            return deny(f"unknown capability: {call.cap}")
        call.cap = name
        entry.update(cap=name, worker=w.id, band=w.band, args_hash=_args_hash(call.args),
                     **_call_facts(meta, call.elevate))
        if entry["args_hash"] is None:
            return deny("bad args: no canonical JSON form")
        if call.appliance is not None:
            entry["appliance"] = call.appliance
        # 4. why
        why = call.why.strip() if isinstance(call.why, str) else ""
        if not why:
            return deny("denied: why required")
        if len(why) > wire.WHY_MAX:
            return deny(f"denied: why longer than {wire.WHY_MAX} characters")
        call.why = entry["why"] = why
        # 5. rights
        refused = self._rights(caller, s, w, call, entry)
        if refused:
            return deny(f"denied: {refused['layer']}: {refused['reason']}",
                        f"{refused['layer']}: {refused['reason']}")
        # 6. gates
        results = await gates_mod.run(self.gates, _gate_input(caller, s, w, call, entry))
        entry["gates"] = results
        verdict = rights.gate_decision(results)
        if verdict["verdict"] == "block":
            first = next(r for r in results if r["gate"] == verdict["by"][0] and r["mode"] == "enforce")
            return deny(f"denied: blocked by gate {first['gate']}: {first['reason']}", "gate")
        # 7. holds (10.3)
        hold = Hold(f"h_{_secrets.token_hex(8)}", caller, call, s, w, meta, {},
                    _hard_stops(meta, w, entry["risk"]),
                    self.identities.needs_approval(caller.person, name),
                    verdict["by"] if verdict["verdict"] == "flag" else [])
        if hold.reasons():
            entry.update(decision="held", hold=hold.name, reasons=hold.reasons())
            hold.entry = dict(entry)
            self.holds[hold.name] = hold
            return {"id": cid, "ok": False, "held": True, "hold": hold.name,
                    "error": "held: " + "; ".join(hold.reasons())}
        # 8. ticket
        entry["decision"] = "allow"
        return await self._run(caller, s, w, call, meta, entry)

    def resolve(self, target: str) -> Worker | None:
        """A worker by id, else by name (names are unique per band)."""
        if target in self.workers:
            return self.workers[target]
        named = [w for w in self.workers.values() if w.name == target]
        return named[0] if len(named) == 1 else None

    def _rights(self, caller: Caller, s: Session, w: Worker, call: Call, entry: dict) -> dict | None:
        if caller.person and self.identities.expired(caller.person, self.clock()):
            return {"layer": "person", "reason": "expired"}
        layers = self.identities.layers(caller) + [["session", {"grants": s.caps}]] + list(call.layers)
        # The worker's two exceptions (6.3): ALWAYS caps are within every
        # ceiling; core.trust.set goes by role, so a narrowed home ceiling
        # can be widened again.
        if call.cap not in wire.ALWAYS + wire.HOME_ONLY:
            layers.append(["ceiling", w.ceiling])
        facts = {"cap": call.cap, "risk": entry["risk"], "effects": entry["effects"],
                 "target": call.appliance or w.name}
        out = rights.check(layers, facts)
        return None if out["ok"] else out

    # -- run: mint, send, wait (6.2, 6.5) ----------------------------------

    async def _run(self, caller: Caller, s: Session, w: Worker, call: Call, meta: dict,
                   entry: dict) -> dict:
        cid = entry["call"]
        try:
            args = self.secrets(call.args, caller, call.cap) if self.secrets else call.args
            ahash = wire.args_hash(args)
        except Exception as e:
            entry.update(ok=False, error=f"secret: {e}")
            return {"id": cid, "ok": False, "error": entry["error"]}
        if "taints" in (meta.get("effects") or ()):
            s.tainted = True
        self.identities.mark_used(caller.person, call.cap)
        request = {"id": cid, "cap": call.cap, "target": w.id, "args": {} if args is None else args}
        if call.appliance is not None:
            request["appliance"] = call.appliance
        wait = _timeout(meta) + TIMEOUT_GRACE
        may_retry = bool(meta.get("idempotent")) and not NO_RETRY & set(_hold_effects(meta))
        entry["ticket"], reminted, retried = [], False, False
        start = time.monotonic()
        while True:
            request["ticket"] = self._mint(cid, caller, s, w, call, ahash)
            nonce = request["ticket"]["nonce"]
            entry["ticket"].append(nonce)
            self._inflight[nonce] = (request["ticket"], caller, s.id, call.layers)
            try:
                reply = await asyncio.wait_for(self.send(w.band, request), wait)
            except asyncio.TimeoutError:
                if may_retry and not retried:
                    retried = True
                    continue
                reply = {"ok": False, "error": "timeout: the call may still be running"}
            except Exception as e:
                reply = {"ok": False, "error": f"send failed: {e}"}
            finally:
                self._inflight.pop(nonce, None)
            if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
                reply = {"ok": False, "error": "malformed reply"}
            elif "from" in reply and reply["from"] != w.id:
                reply = {"ok": False, "error": "reply from the wrong worker"}
            if not reminted and not reply.get("ok") and reply.get("error") in REMINT:
                reminted = True
                continue
            break
        entry["ms"] = int((time.monotonic() - start) * 1000)
        out = {"id": cid, "ok": reply["ok"]}
        if reply["ok"]:
            out["result"] = reply.get("result")
        else:
            # Secret values never come back in error text (6.4): a schema
            # error quotes the argument, and only the hub knows what it resolved.
            out["error"] = _redact(str(reply.get("error", "failed")), _resolved(call.args, args))
            entry["error"] = out["error"]
        for k in ("retry", "elevated"):
            if k in reply:
                out[k] = reply[k]
        if "elevated" in reply:
            entry["lease"] = reply["elevated"]
        if out["ok"] and any(g.result for g in self.gates):
            out = await self._check_result(caller, s, w, call, entry, out)
        entry["ok"] = out["ok"]
        return out

    async def _check_result(self, caller: Caller, s: Session, w: Worker, call: Call, entry: dict,
                            out: dict) -> dict:
        """Gates' ``result`` checks (gate.py): a block withholds the result."""
        results = await gates_mod.run_result(self.gates, _gate_input(caller, s, w, call, entry), out.get("result"))
        entry["gates"] = list(entry.get("gates") or ()) + results
        verdict = rights.gate_decision(results)
        if verdict["verdict"] != "block":
            return out
        first = next(r for r in results if r["gate"] == verdict["by"][0] and r["mode"] == "enforce")
        entry["error"] = f"denied: blocked by gate {first['gate']}: {first['reason']}"
        return {"id": out["id"], "ok": False, "error": entry["error"]}

    def _mint(self, cid: str, caller: Caller, s: Session, w: Worker, call: Call, ahash: str) -> dict:
        now = int(self.clock())
        t = {"typ": "rook-ticket", "v": 2, "hub": self.hub_id, "band": w.band, "call": cid,
             "nonce": _secrets.token_hex(16), "worker": w.id, "cap": call.cap,
             "args_hash": ahash, "session": s.id, "why": call.why,
             "iat": now, "exp": now + TICKET_SECS}
        if call.appliance is not None:
            t["appliance"] = call.appliance
        if caller.person:
            t["person"] = caller.person
        if caller.agent:
            t["agent"] = caller.agent
        if call.elevate:
            t["elevate"] = True
        t = wire.sign(self.op_key, t)
        if self.hubkey:
            t["hubkey"] = self.hubkey
        return t

    # -- holds (10.3) ------------------------------------------------------

    def may_release(self, hold: Hold, by: str, present: bool = False) -> bool:
        """Hard stops: only the person the call is for (an owner when it is
        for no person), and only from a door where that person is there
        (``present``: the site, a phone card, ``POST /holds`` with their own
        token), never from a dispatched cap a job or plugin can make (10.3).
        A new cap for a child: an owner. A gate flag: the person, an owner,
        or the security triage component for its own gate's flags."""
        if not isinstance(by, str):
            return False
        person, owner = hold.caller.person, self.identities.is_owner(by)
        if hold.hard and (not present or (by != person and (person or not owner))):
            return False
        if hold.new_cap and not owner:
            return False
        triage = by == TRIAGE[0] and set(hold.flagged_by) <= {TRIAGE[1]}
        return not hold.flagged_by or by == person or owner or triage

    async def release(self, name: str, by: str, present: bool = False) -> dict:
        """Run the exact held call with a ticket minted now. Holds don't
        expire, but rights are checked again (a grant taken away while it
        waited still counts) and a session ended on purpose stays ended."""
        hold = self.holds.get(name)
        if hold is None:
            return {"ok": False, "error": f"unknown hold: {name}"}
        if not self.may_release(hold, by, present):
            return {"ok": False, "error": f"{by} may not release {name}"}
        del self.holds[name]
        entry = {**hold.entry, "ts": self.clock(), "decision": "released", "by": by}
        # The worker and the cap as they are now: a re-announce (new effects)
        # or a machine made sacred since must not ride on an approval given
        # for less (a gate flag triage may release is not a hard stop).
        w = self.workers.get(hold.worker.id)
        caps = None if w is None else w.caps if hold.call.appliance is None else w.appliances.get(hold.call.appliance)
        meta = (caps or {}).get(hold.call.cap)
        refused = None if meta is None else self._rights(hold.caller, hold.session, w, hold.call, entry)
        if meta != hold.meta or set(_hard_stops(meta, w, entry["risk"])) - set(hold.hard):
            refused = {"layer": "hold", "reason": f"{hold.call.cap} changed since it was held; call it again"}
        if hold.session.ended:
            refused = {"layer": "session", "reason": "ended"}
        if refused:
            entry.update(decision="deny", ok=False, layer=f"{refused['layer']}: {refused['reason']}",
                         error=f"denied: {refused['layer']}: {refused['reason']}")
            out = {"id": entry["call"], "ok": False, "error": entry["error"]}
        else:
            out = await self._run(hold.caller, hold.session, hold.worker, hold.call, hold.meta, entry)
        self.journal(entry)
        return out

    def refuse(self, name: str, by: str, present: bool = False) -> dict:
        """The person said no: final."""
        hold = self.holds.get(name)
        if hold is None:
            return {"ok": False, "error": f"unknown hold: {name}"}
        if not self.may_release(hold, by, present):
            return {"ok": False, "error": f"{by} may not refuse {name}"}
        del self.holds[name]
        self.journal({**hold.entry, "ts": self.clock(), "decision": "refused", "by": by})
        return {"ok": True}

    # -- core.call (13): a plugin's sub-call inside a call in flight -------

    async def sub_call(self, from_worker: str, ticket: dict, cap: str, target: str,
                       args: dict | None, why: str) -> dict:
        """Dispatch a plugin's ``core.call`` as the parent call's person,
        agent and session, with the parent's own extra layers (a page's caps,
        an outer plugin's ``uses``) plus the calling plugin's ``uses``. The
        parent ticket must be one this hub minted for that worker and still
        in flight."""
        nonce = ticket.get("nonce") if isinstance(ticket, dict) else None
        held = self._inflight.get(nonce) if isinstance(nonce, str) else None
        if held is None or held[0] != ticket or ticket["worker"] != from_worker:
            return {"ok": False, "error": "denied: core.call: no such call in flight"}
        parent, caller, sid, layers = held
        w = self.workers.get(from_worker)
        plugin = parent["cap"].partition(".")[0]
        uses = {"grants": list((w.uses if w else {}).get(plugin, ()))}
        return await self.dispatch(caller, Call(cap=cap, target=target, args=args, why=why,
                                                session=sid, layers=[*layers, ["uses", uses]]))

    # -- core.elevate (7.2, 13): a lease for a call in flight -------------

    def elevate(self, from_worker: str, ticket: dict, reason: str) -> dict:
        """Answer a worker's ``core.elevate``: the ticket must be one this hub
        minted for that worker and still in flight, and the call's rights
        (person, agent, session, the worker's ceiling) must permit
        ``core.elevate`` there. Journals the outcome; returns the reply body."""
        nonce = ticket.get("nonce") if isinstance(ticket, dict) else None
        held = self._inflight.get(nonce) if isinstance(nonce, str) else None
        w = self.workers.get(from_worker)
        s = self.sessions.get(held[2]) if held else None
        if held is None or held[0] != ticket or ticket["worker"] != from_worker or w is None or s is None:
            return {"ok": False, "error": "elevation: denied"}
        caller = held[1]
        target = ticket.get("appliance") or w.name
        # Every layer the call itself ran under (8.3): a page's caps, a plugin's uses.
        layers = self.identities.layers(caller) + [["session", {"grants": s.caps}], *held[3],
                                                   ["ceiling", w.ceiling]]
        out = rights.check(layers, {"cap": "core.elevate", "risk": "admin", "effects": [], "target": target})
        if s.ended or self.clock() > s.exp:
            out = {"ok": False, "layer": "session", "reason": "ended"}
        elif caller.person and self.identities.expired(caller.person, self.clock()):
            out = {"ok": False, "layer": "person", "reason": "expired"}
        entry = {"kind": "elevation", "ts": self.clock(), "hub": self.hub_id, "call": ticket["call"],
                 "cap": ticket["cap"], "worker": from_worker, "band": ticket["band"], "risk": "admin",
                 "reason": reason if isinstance(reason, str) else None, "session": s.id,
                 "project": s.project, "person": caller.person, "agent": caller.agent}
        if not out["ok"]:
            entry.update(decision="deny", layer=f"{out['layer']}: {out['reason']}")
            self.journal(entry)
            return {"ok": False, "error": "elevation: outside ceiling" if out["layer"] == "ceiling"
                    else "elevation: denied"}
        now = int(self.clock())
        lease = {"typ": "rook-lease", "v": 2, "id": _secrets.token_hex(16), "hub": self.hub_id,
                 "band": ticket["band"], "worker": from_worker, "session": s.id,
                 "iat": now, "exp": now + wire.LEASE_SECS}
        for k in ("person", "agent"):
            if k in ticket:
                lease[k] = ticket[k]
        lease = wire.sign(self.op_key, lease)
        self.leases[lease["id"]] = (from_worker, s.id, lease["exp"])
        entry.update(decision="allow", lease=lease["id"])
        self.journal(entry)
        return {"ok": True, "result": {"lease": lease}}

    # -- journal -----------------------------------------------------------

    def _note(self, kind: str, s: Session | None, **fields) -> None:
        entry = {"kind": kind, "ts": self.clock(), "hub": self.hub_id, **fields}
        if s is not None:
            entry.update(session=s.id, project=s.project, person=s.person, agent=s.agent)
        self.journal(entry)


def _gate_input(caller: Caller, s: Session, w: Worker, call: Call, entry: dict) -> dict:
    """What a gate sees (spec 10.2); args with secret references unresolved."""
    return {"cap": call.cap, "args": call.args, "caller": {"person": caller.person, "agent": caller.agent},
            "worker": w.name, "appliance": call.appliance, "why": call.why, "risk": entry["risk"],
            "scope": entry["scope"], "effects": entry["effects"], "session": s.view(), "band": w.band}


def _resolve_alias(caps: dict, cap: str) -> str:
    if cap in caps:
        return cap
    named = [n for n, m in caps.items() if cap in (m.get("aliases") or ())]
    return named[0] if len(named) == 1 else cap


def _timeout(meta: dict) -> float:
    try:
        return parse_duration(meta["timeout"]) if "timeout" in meta else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def _call_facts(meta: dict, elevate: bool) -> dict:
    """risk, scope and effects as dispatch sees them: unknown risk is admin,
    unknown scope external, an elevated call admin (4.2, 4.4, 7.1), and so is
    a cap declared ``elevation: upfront`` (it always runs elevated)."""
    risk = meta.get("risk")
    scope = meta.get("scope")
    elevated = elevate or meta.get("elevation") == "upfront"
    return {"risk": "admin" if elevated or risk not in rights.RISKS else risk,
            "scope": scope if scope in rights.SCOPES else "external",
            "effects": list(meta.get("effects") or ())}


def _hold_effects(meta: dict) -> list:
    effects = list(meta.get("effects") or ())
    return effects + ["destructive"] if set(effects) - set(rights.EFFECTS) else effects


def _reveals_secret(meta: dict) -> bool:
    """A ``sensitive`` cap whose result declares a ``secret`` field."""
    def has(spec: Any) -> bool:
        if isinstance(spec, dict):
            return spec.get("type") == "secret" or spec.get("secret") is True or any(
                has(v) for v in spec.values())
        if isinstance(spec, list):
            return any(has(v) for v in spec)
        return spec == "secret"
    return "sensitive" in (meta.get("effects") or ()) and has(meta.get("result"))


def _hard_stops(meta: dict, w: Worker, risk: str) -> list:
    out = [e for e in rights.HARD_STOP_EFFECTS if e in _hold_effects(meta)]
    if _reveals_secret(meta):
        out.append("reveals a secret")
    if w.sacred and risk != "read":
        out.append("sacred machine")
    return out


def _args_hash(args: Any) -> str | None:
    try:
        return wire.args_hash(args)
    except (ValueError, RecursionError):
        return None


def _resolved(before: Any, after: Any) -> list[str]:
    """The strings the secret step put into ``after`` in place of ``before``."""
    if isinstance(after, str):
        return [after] if after != before and after else []
    if isinstance(after, dict):
        b = before if isinstance(before, dict) else {}
        return [x for k, v in after.items() for x in _resolved(b.get(k), v)]
    if isinstance(after, list):
        b = before if isinstance(before, list) else []
        return [x for i, v in enumerate(after) for x in _resolved(b[i] if i < len(b) else None, v)]
    return []


def _redact(text: str, values: list[str]) -> str:
    for v in sorted(set(values), key=len, reverse=True):
        text = text.replace(v, "[redacted]")
    return text
