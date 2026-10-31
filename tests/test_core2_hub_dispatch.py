"""Hub dispatch (rook/core2/hub): identity, sessions, why, rights, gates,
holds, tickets and the journal, per docs/spec/core-v2.md sections 8 and 10.
Every async test runs under a hard timeout; the transport is a fake."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("nacl")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import rights, wire  # noqa: E402
from rook.core2.hub import gate as gates  # noqa: E402
from rook.core2.hub.dispatch import Call, Dispatcher, Worker  # noqa: E402
from rook.core2.hub.identity import Caller, Identities  # noqa: E402

VECTORS = Path(__file__).resolve().parents[1] / "conformance" / "core-v2" / "vectors"
BAND = "e17904e6ed4c664592f64f6f1d02c682"
WID = "3f0a9c1d2e3f40516273849506a7b8c9"
NOW = 1793300000.0

BAKE = Caller(person="person:bake")
CLAUDE_FOR_BAKE = Caller(person="person:bake", agent="agent:claude")
KID = Caller(person="person:kid")
NIGHTLY = Caller(agent="agent:nightly")

CAPS = {
    "shell.exec": {"risk": "exec", "scope": "local", "effects": []},
    "file.read": {"risk": "read", "scope": "local", "effects": [], "aliases": ["fs.read"],
                  "idempotent": True, "timeout": "1s"},
    "file.delete": {"risk": "write", "scope": "local", "effects": ["destructive"], "idempotent": True,
                    "timeout": "1s"},
    "mail.send": {"risk": "write", "scope": "external", "effects": ["sends_as"]},
    "shop.buy": {"risk": "write", "scope": "external", "effects": ["spends"]},
    "odd.thing": {"risk": "write", "scope": "local", "effects": ["teleports"]},
    "vault.show": {"risk": "read", "scope": "self", "effects": ["sensitive"],
                   "result": {"type": "map", "of": {"type": "secret"}}},
    "screen.grab": {"risk": "read", "scope": "local", "effects": ["sensitive"],
                    "result": {"type": "file"}},
    "web.fetch": {"risk": "read", "scope": "external", "effects": ["taints"]},
    "light.set": {"risk": "write", "scope": "local", "effects": ["physical"]},
    "homework.item.list": {"risk": "read", "scope": "self", "effects": []},
    "agent.chat.send": {"risk": "write", "scope": "self", "effects": []},
    "weird.risk": {"risk": "godmode", "scope": "nowhere", "effects": []},
    "pkg.fix": {"risk": "read", "scope": "local", "effects": [], "elevation": "upfront"},
    "core.manifests": {"risk": "read", "scope": "self", "effects": []},
    "core.trust.set": {"risk": "admin", "scope": "self", "effects": ["persists"]},
}


def run(coro, timeout=10):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class Wire:
    """Fake transport: records requests, verifies each ticket the way the
    worker would (wire.verify_ticket), then answers."""

    def __init__(self, hub_id, op_pub, answer=None):
        self.requests = []
        self.answer = answer or (lambda req, n: {"id": req["id"], "from": WID, "ok": True,
                                                  "result": {"cap": req["cap"]}})
        self.worker = {"worker_id": WID, "band": BAND, "name": "kaiju",
                       "trust": {hub_id: {"band": BAND, "role": "home",
                                          "keys": {wire.key_id(op_pub): op_pub},
                                          "ceiling": {"grants": ["*"]}}},
                       "caps": {c: {"risk": m["risk"], "effects": m["effects"]} for c, m in CAPS.items()}}
        self.replay = wire.ReplayCache()
        self.verdicts = []

    async def __call__(self, band, request):
        assert band == BAND
        self.requests.append(json.loads(json.dumps(request)))
        ok, reason = wire.verify_ticket(request["ticket"], request, self.worker, int(NOW), self.replay)
        self.verdicts.append(reason)
        out = self.answer(request, len(self.requests))
        if isinstance(out, Exception):
            raise out
        if asyncio.iscoroutine(out):
            return await out
        return out


@pytest.fixture
def hub():
    root = SigningKey(bytes(range(32)))
    op = SigningKey(bytes(range(1, 33)))
    hub_id = wire.key_id(bytes(root.verify_key))
    ids = Identities()
    ids.add_user("bake", "owner")
    ids.add_user("ann", "adult")
    ids.add_user("kid", "child")
    ids.add_user("visitor", "guest", created=NOW)
    ids.add_agent("claude", {"grants": ["*"], "max_risk": "exec"})
    ids.add_agent("nightly", {"grants": ["file.*", "web.*"], "max_risk": "read"})
    journal = []
    tx = Wire(hub_id, wire.pub_text(op))
    w = Worker(WID, "kaiju", BAND, dict(CAPS), {"grants": ["*"]}, uses={"file": ["web.*"]})
    clock = [NOW]
    d = Dispatcher(hub_id=hub_id, op_key=op, identities=ids, workers={WID: w}, send=tx,
                   journal=journal.append, clock=lambda: clock[0])
    d.tx, d.log, d.clock_box, d.worker = tx, journal, clock, w
    return d


class Registry:
    """The hub's appliance registry as dispatch sees it (get, worked)."""

    def __init__(self):
        self.items, self.worked_calls = {}, []

    def get(self, name):
        rec = self.items.get(name)
        return json.loads(json.dumps(rec)) if rec else None

    def worked(self, name, wid):
        self.worked_calls.append((name, wid))
        d = self.items[name]["delegates"]
        d.remove(wid)
        d.insert(0, wid)


def lamps(d, *names, delegates=(WID,)):
    """Light appliances of a ``light`` plugin on kaiju."""
    d.worker.plugins["light"] = ["light.set"]
    if d.appliances is None:
        d.appliances = Registry()
    for n in names:
        d.appliances.items[n] = {"name": n, "plugin": "light", "delegates": list(delegates), "settings": {}}


def call(d, caller, cap, args=None, caps=("*",), why="because the test says so", **kw):
    s = d.start_session(caller, list(caps), project=kw.pop("project", None), task=kw.pop("task", None))
    return run(d.dispatch(caller, Call(cap=cap, target=kw.pop("target", "kaiju"), args=args, why=why,
                                       session=s.id, **kw))), s


def calls(d):
    return [e for e in d.log if e["kind"] == "call"]


# -- the happy path: ticket, worker check, journal ---------------------------

def test_allowed_call_mints_a_ticket_the_worker_accepts(hub):
    out, s = call(hub, CLAUDE_FOR_BAKE, "shell.exec", {"argv": ["uptime"]}, project="castling", task="t_1")
    assert out == {"id": out["id"], "ok": True, "result": {"cap": "shell.exec"}}
    assert hub.tx.verdicts == ["ok"]
    t = hub.tx.requests[0]["ticket"]
    assert t["call"] == out["id"] == hub.tx.requests[0]["id"]
    assert (t["person"], t["agent"], t["session"], t["worker"], t["band"]) == (
        "person:bake", "agent:claude", s.id, WID, BAND)
    assert t["exp"] - t["iat"] == 60 and t["why"] == "because the test says so"
    assert t["args_hash"] == wire.args_hash({"argv": ["uptime"]})
    [e] = calls(hub)
    assert e["decision"] == "allow" and e["ok"] is True and e["ticket"] == [t["nonce"]]
    for k in ("ts", "call", "hub", "band", "worker", "cap", "person", "agent", "session", "project",
              "why", "args_hash", "risk", "scope", "effects", "gates", "ms"):
        assert k in e, k
    assert e["project"] == "castling"
    start = hub.log[0]
    assert start["kind"] == "session" and start["event"] == "start" and start["task"] == "t_1"


def test_target_by_id_and_alias_resolves(hub):
    out, _ = call(hub, BAKE, "fs.read", target=WID)
    assert out["ok"] and hub.tx.requests[0]["cap"] == "file.read"
    assert hub.tx.requests[0]["ticket"]["cap"] == "file.read" and calls(hub)[0]["cap"] == "file.read"


def test_core_manifests_and_trust_set_ignore_the_ceiling(hub):
    hub.worker.ceiling = {"grants": []}
    assert call(hub, BAKE, "core.manifests")[0]["ok"]
    # the worker takes core.trust.set from its home hub whatever the ceiling
    # (6.3): a home hub that narrowed it can widen it again
    assert call(hub, BAKE, "core.trust.set", {"membership": {}})[0]["ok"]
    assert hub.tx.verdicts[-1] == "ok"
    assert call(hub, BAKE, "file.read")[0]["error"] == "denied: ceiling: grants"


def test_secrets_resolved_for_the_wire_only(hub):
    hub.secrets = lambda args, caller, cap, target=None: {**args, "pw": "hunter2"} if args else args
    seen = []
    hub.gates = [gates.Gate("spy", lambda inp: seen.append(inp) or
                            {"verdict": "allow", "confidence": 1, "reason": "other"}, mode="enforce")]
    out, _ = call(hub, BAKE, "shell.exec", {"pw": "{{secret:pw}}"})
    assert out["ok"]
    assert hub.tx.requests[0]["args"]["pw"] == "hunter2"
    assert hub.tx.requests[0]["ticket"]["args_hash"] == wire.args_hash({"pw": "hunter2"})
    assert seen[0]["args"] == {"pw": "{{secret:pw}}"}
    assert calls(hub)[0]["args"] == {"pw": "{{secret:pw}}"}
    assert "hunter2" not in json.dumps(hub.log)



def test_secret_values_never_come_back_in_error_text(hub):
    # A worker's schema error quotes the argument it got: the resolved value.
    hub.secrets = lambda args, caller, cap, target=None: {**args, "n": "hunter2"} if args else args
    hub.tx.answer = lambda req, n: {"id": req["id"], "from": WID, "ok": False,
                                    "error": f"bad args: n: {req['args']['n']!r} is not of type 'integer'"}
    out, _ = call(hub, BAKE, "file.read", {"n": "{{secret:pw}}"})
    assert out["error"] == "bad args: n: '[redacted]' is not of type 'integer'"
    assert "hunter2" not in json.dumps(hub.log)

# -- step by step refusals ---------------------------------------------------

@pytest.mark.parametrize("caller,why", [
    (Caller(), "no identity"),
    (Caller(person="bake"), "bad person"),
    (Caller(agent="person:bake"), "bad agent"),
    (Caller(agent="band:abc"), "bad agent"),
])
def test_bad_callers_refused(hub, caller, why):
    s = hub.start_session(BAKE, ["*"])
    out = run(hub.dispatch(caller, Call(cap="file.read", target="kaiju", why="x", session=s.id)))
    assert out["error"] == f"denied: caller: {why}"
    with pytest.raises(ValueError):
        hub.start_session(caller, ["*"])


def test_session_must_exist_belong_and_be_live(hub):
    out = run(hub.dispatch(BAKE, Call(cap="file.read", target="kaiju", why="x", session="nope")))
    assert out["error"] == "denied: session: unknown or ended"
    s = hub.start_session(CLAUDE_FOR_BAKE, ["*"])
    out = run(hub.dispatch(BAKE, Call(cap="file.read", target="kaiju", why="x", session=s.id)))
    assert out["error"] == "denied: session: belongs to another caller"
    hub.clock_box[0] += 12 * 3600 + 1
    out = run(hub.dispatch(CLAUDE_FOR_BAKE, Call(cap="file.read", target="kaiju", why="x", session=s.id)))
    assert out["error"] == "denied: session: unknown or ended"
    s2 = hub.start_session(BAKE, ["*"])
    hub.end_session(s2.id)
    out = run(hub.dispatch(BAKE, Call(cap="file.read", target="kaiju", why="x", session=s2.id)))
    assert out["error"] == "denied: session: unknown or ended"
    assert hub.log[-2]["event"] == "end"


def test_prune_forgets_ended_and_expired_sessions_only(hub):
    live = hub.start_session(BAKE, ["*"])
    ended = hub.start_session(BAKE, ["*"])
    hub.end_session(ended.id)
    held, s = call(hub, BAKE, "file.delete")             # waits for the person
    busy = hub.start_session(CLAUDE_FOR_BAKE, ["*"], project="castling")
    hub._inflight["n"] = ({}, CLAUDE_FOR_BAKE, busy.id)  # a call still running in it
    assert hub.prune_sessions() == 1 and ended.id not in hub.sessions
    hub.clock_box[0] += 12 * 3600 + 1
    assert hub.prune_sessions() == 1 and set(hub.sessions) == {busy.id, s.id}
    del hub._inflight["n"]
    assert run(hub.release(held["hold"], "person:bake", present=True))["ok"]
    assert hub.prune_sessions() == 2 and not hub.sessions
    assert hub.projects == {"agent:claude": {"castling"}}  # the agent still reads its projects
    assert live.id not in hub.sessions


def test_session_caps_scope_tickets(hub):
    out, _ = call(hub, BAKE, "shell.exec", caps=["file.*"])
    assert out["error"] == "denied: session: grants"
    assert calls(hub)[0]["layer"] == "session: grants"
    assert call(hub, BAKE, "file.read", caps=["file.*"])[0]["ok"]
    assert hub.tx.verdicts == ["ok"]


def test_unknown_worker_appliance_and_cap(hub):
    assert call(hub, BAKE, "file.read", target="ghost")[0]["error"] == "unknown worker: ghost"
    assert call(hub, BAKE, "file.read", appliance="tv")[0]["error"] == "unknown appliance: tv"
    assert call(hub, BAKE, "nope.nope")[0]["error"] == "unknown capability: nope.nope"
    assert not hub.tx.requests


@pytest.mark.parametrize("why", ["", "   ", None])
def test_why_required(hub, why):
    out, _ = call(hub, BAKE, "file.read", why=why)
    assert out["error"] == "denied: why required"
    assert calls(hub)[0]["decision"] == "deny" and not hub.tx.requests


def test_why_too_long_and_trimmed(hub):
    assert call(hub, BAKE, "file.read", why="x" * 501)[0]["error"].startswith("denied: why longer")
    out, _ = call(hub, BAKE, "file.read", why="  tidy  ")
    assert out["ok"] and hub.tx.requests[0]["ticket"]["why"] == "tidy"


def test_args_without_canonical_form_refused(hub):
    out, _ = call(hub, BAKE, "file.read", {"x": float("nan")})
    assert out["error"] == "bad args: no canonical JSON form" and not hub.tx.requests


# -- rights: the intersection rule (8.3) -------------------------------------

def test_agent_for_person_gets_the_intersection(hub):
    # bake (owner) may do anything; claude is capped at exec
    hub.worker.caps["sys.admin"] = {"risk": "admin", "scope": "local", "effects": []}
    assert call(hub, BAKE, "sys.admin")[0]["ok"]
    out, _ = call(hub, CLAUDE_FOR_BAKE, "sys.admin")
    assert out["error"] == "denied: agent: risk"


def test_agent_alone_uses_its_own_rights(hub):
    assert call(hub, NIGHTLY, "file.read")[0]["ok"]
    assert call(hub, NIGHTLY, "shell.exec")[0]["error"] == "denied: agent: grants"
    e = calls(hub)[0]
    assert e["agent"] == "agent:nightly" and e["person"] is None
    assert "person" not in hub.tx.requests[0]["ticket"]


def test_unknown_agent_and_person_have_no_rights(hub):
    assert call(hub, Caller(agent="agent:ghost"), "file.read")[0]["error"] == "denied: agent: grants"
    assert call(hub, Caller(person="person:ghost"), "file.read")[0]["error"] == "denied: person: grants"


def test_system_identities_only_core_caps(hub):
    sys_hub = Caller(agent="system:hub")
    assert call(hub, sys_hub, "core.manifests")[0]["ok"]
    assert call(hub, sys_hub, "shell.exec")[0]["error"] == "denied: agent: grants"


def test_a_contract_name_resolves_and_grants_on_it_cover_the_provider(hub):
    # spec 4.1, 8.2: kvm.screen.capture provides screen.capture.
    cap = {"risk": "read", "scope": "local", "effects": [], "contracts": ["screen.capture"]}
    hub.worker.caps["kvm.screen.capture"] = cap
    hub.tx.worker["caps"]["kvm.screen.capture"] = {"risk": "read", "effects": [], "contracts": ["screen.capture"]}
    hub.tx.worker["trust"][hub.hub_id]["ceiling"] = {"grants": ["screen.capture"]}   # the worker's own check
    nightly = ["uses", {"grants": ["screen.capture"]}]
    out, _ = call(hub, BAKE, "screen.capture", caps=("screen.*",), layers=[nightly])
    assert out["ok"] and hub.tx.verdicts == ["ok"]
    req = hub.tx.requests[-1]
    assert req["cap"] == "kvm.screen.capture" and req["ticket"]["contract"] == "screen.capture"
    assert calls(hub)[-1]["cap"] == "kvm.screen.capture" and calls(hub)[-1]["contract"] == "screen.capture"
    # By its own name the contract's grant doesn't reach it.
    assert call(hub, BAKE, "kvm.screen.capture", caps=("screen.*",))[0]["error"] == "denied: session: grants"
    # Two providers on one worker: say which.
    hub.worker.caps["vnc.screen.capture"] = dict(cap)
    out, _ = call(hub, BAKE, "screen.capture")
    assert out["error"] == ("several caps here provide screen.capture "
                            "(kvm.screen.capture, vnc.screen.capture): call one by name")


def test_a_given_contract_must_be_one_the_cap_provides(hub):
    out, _ = call(hub, BAKE, "file.read", contract="notify.send")
    assert out["error"] == "file.read doesn't provide notify.send" and not hub.tx.requests


def test_extra_layers_page_and_uses(hub):
    page = ["page", {"grants": ["file.read"]}]
    assert call(hub, BAKE, "file.read", layers=[page])[0]["ok"]
    assert call(hub, BAKE, "shell.exec", layers=[page])[0]["error"] == "denied: page: grants"


def test_ceiling_with_target_patterns(hub):
    hub.worker.ceiling = {"grants": ["light.*@downstairs-*", "file.*"], "max_risk": "write"}
    assert call(hub, BAKE, "file.read")[0]["ok"]
    assert call(hub, BAKE, "shell.exec")[0]["error"] == "denied: ceiling: grants"
    lamps(hub, "downstairs-lamp", "attic-lamp")
    assert call(hub, BAKE, "light.set", appliance="downstairs-lamp")[0]["ok"]
    assert call(hub, BAKE, "light.set", appliance="attic-lamp")[0]["error"] == "denied: ceiling: grants"
    t = hub.tx.requests[-1]["ticket"]
    assert t["appliance"] == "downstairs-lamp" and hub.tx.requests[-1]["appliance"] == "downstairs-lamp"


def test_elevated_call_counts_as_admin(hub):
    out, _ = call(hub, CLAUDE_FOR_BAKE, "file.read", elevate=True)
    assert out["error"] == "denied: agent: risk"
    out, _ = call(hub, BAKE, "file.read", elevate=True)
    assert out["ok"] and hub.tx.requests[0]["ticket"]["elevate"] is True
    assert calls(hub)[-1]["risk"] == "admin"



def test_upfront_elevation_counts_as_admin(hub):
    # It always runs elevated (7.1), so gates, the sacred hold and the journal see admin.
    out, _ = call(hub, CLAUDE_FOR_BAKE, "pkg.fix")
    assert out["error"] == "denied: agent: risk"
    out, _ = call(hub, BAKE, "pkg.fix")
    assert out["ok"] and calls(hub)[-1]["risk"] == "admin"


def test_elevate_keeps_the_calls_own_layers_and_session(hub):
    got = []

    async def answer(req, n):
        if n == 1:
            got.append(await hub.elevate(WID, req["ticket"], "access_denied"))
        return {"id": req["id"], "from": WID, "ok": True, "result": n}

    hub.tx.answer = answer
    out, s = call(hub, BAKE, "file.read", layers=[["page", {"grants": ["file.read"]}]])
    assert out["ok"] and got[0] == {"ok": False, "error": "elevation: denied"}     # the page didn't allow it
    got.clear()

    async def end_then(req, n):
        hub.end_session(req["ticket"]["session"])
        got.append(await hub.elevate(WID, req["ticket"], "upfront"))
        return {"id": req["id"], "from": WID, "ok": True, "result": n}

    hub.tx.answer = end_then
    out, _ = call(hub, BAKE, "file.read")
    assert got[0] == {"ok": False, "error": "elevation: denied"}               # an ended session gets no lease


def _elevating(hub, reason="access_denied", before=None):
    """The worker asks for a lease during the call's first run."""
    got = []

    async def answer(req, n):
        if n == 1:
            if before:
                before(req)
            got.append(await hub.elevate(WID, req["ticket"], reason))
        return {"id": req["id"], "from": WID, "ok": True, "result": n}

    hub.tx.answer = answer
    return got


def elevations(d):
    return [e for e in d.log if e["kind"] == "elevation"]


def test_elevation_goes_through_the_gates_as_admin(hub):
    # castling-security: the gate hook sits before ticket minting on every
    # door, and "elevation must pass" (drift #16).
    seen = []
    hub.gates = [gates.Gate("spy", lambda inp: seen.append(inp) or
                            {"verdict": "allow", "confidence": 1, "reason": "other"}, mode="enforce")]
    got = _elevating(hub)
    out, s = call(hub, BAKE, "file.read", {"path": "/etc/shadow"}, why="read the shadow file")
    assert out["ok"] and got[0]["ok"] and "lease" in got[0]["result"]
    at_dispatch, at_elevation = seen
    assert at_dispatch["risk"] == "read" and "elevation" not in at_dispatch
    assert at_elevation["risk"] == "admin" and at_elevation["elevation"] == "access_denied"
    assert (at_elevation["cap"], at_elevation["args"], at_elevation["why"], at_elevation["session"]["id"]) == (
        "file.read", {"path": "/etc/shadow"}, "read the shadow file", s.id)
    [e] = elevations(hub)
    assert e["decision"] == "allow" and e["gates"][0]["gate"] == "spy"


@pytest.mark.parametrize("verdict", ["flag", "block"])
def test_elevation_a_gate_does_not_pass_is_refused(hub, verdict):
    # A flag can't wait for a person inside a running call: no lease.
    hub.gates = [G("security", verdict, match={"risk": ["admin"]})]
    got = _elevating(hub)
    out, _ = call(hub, BAKE, "shell.exec")      # exec: the gate doesn't select it at dispatch
    assert out["ok"] and got == [{"ok": False, "error": "elevation: denied"}]
    [e] = elevations(hub)
    assert e["decision"] == "deny" and e["layer"] == f"gate: {verdict} by security"
    assert e["gates"][0]["verdict"] == verdict and not hub.leases


def test_elevation_shadow_gates_are_recorded_only(hub):
    hub.gates = [G("ml", "block", mode="shadow")]
    got = _elevating(hub)
    out, _ = call(hub, BAKE, "shell.exec")
    assert out["ok"] and got[0]["ok"]
    [e] = elevations(hub)
    assert e["decision"] == "allow" and e["gates"][0]["verdict"] == "block"


def test_elevation_of_an_admin_call_is_not_gated_twice(hub):
    # Asked up front, the call met the gates as admin before its ticket, and
    # a person may have released that hold; the elevation doesn't undo it.
    hub.gates = [G("security", "flag", match={"risk": ["admin"]})]
    got = _elevating(hub, "upfront")
    out, _ = call(hub, BAKE, "pkg.fix")
    assert out["held"]
    rel = run(hub.release(out["hold"], "person:bake", present=True))
    assert rel["ok"] and got[0]["ok"]
    [e] = elevations(hub)
    assert e["decision"] == "allow" and "gates" not in e


def test_elevation_rechecks_rights_after_the_gates(hub):
    async def slow(inp):
        await asyncio.sleep(0.01)
        hub.end_session(inp["session"]["id"])           # access changed while the gate ran
        return {"verdict": "allow", "confidence": 1, "reason": "other"}

    hub.gates = [gates.Gate("slow", slow, budget=1, mode="enforce", match={"risk": ["admin"]})]
    got = _elevating(hub)
    call(hub, BAKE, "shell.exec")
    assert got == [{"ok": False, "error": "elevation: denied"}] and not hub.leases
    assert elevations(hub)[0]["layer"] == "session: ended"


def test_sub_call_keeps_the_parent_calls_layers(hub):
    results = []

    async def answer(req, n):
        if n == 1:
            results.append(await hub.sub_call(WID, req["ticket"], "web.fetch", "kaiju", None, "warm the cache"))
        return {"id": req["id"], "from": WID, "ok": True, "result": n}

    hub.tx.answer = answer
    # file uses web.*; a page that only declared file.read must not reach web.fetch through it
    out, _ = call(hub, BAKE, "file.read", layers=[["page", {"grants": ["file.read"]}]])
    assert out["ok"] and results[0]["error"] == "denied: page: grants"

def test_sub_call_goes_through_the_parent_calls_door(hub):
    # Closing a door stops the work already started through it (review H10).
    results = []

    async def answer(req, n):
        if n == 1:
            hub.close_door("mcp", "person:bake")
            results.append(await hub.sub_call(WID, req["ticket"], "web.fetch", "kaiju", None, "warm the cache"))
        return {"id": req["id"], "from": WID, "ok": True, "result": n}

    hub.tx.answer = answer
    out, _ = call(hub, BAKE, "file.read", door="mcp")
    assert out["ok"] and results[0]["error"] == "denied: door mcp closed"


def test_unknown_risk_is_admin_and_unknown_scope_external(hub):
    out, _ = call(hub, CLAUDE_FOR_BAKE, "weird.risk")
    assert out["error"] == "denied: agent: risk"
    assert calls(hub)[0]["scope"] == "external" and calls(hub)[0]["risk"] == "admin"


# -- presets (8.5) -----------------------------------------------------------

def test_presets_match_vectors():
    v = json.loads((VECTORS / "presets.json").read_text())
    for c in v["cases"]:
        ids = Identities()
        ids.add_user("p", c["preset"], c["user_grants"])
        for plugin, pats in (c["plugin_grants"] or {}).items():
            ids.add_plugin_presets(plugin, {c["preset"]: {"grants": pats}})
        layers = ids.layers(Caller(person="person:p"))
        assert layers == [["person", c["rights"]]], c["name"]
        assert rights.check(layers, c["call"]) == (
            {"ok": True} if c["expect"]["ok"] else {"ok": False, "layer": "person", **{
                k: v for k, v in c["expect"].items() if k != "ok"}}), c["name"]


def test_rights_vectors_through_the_intersection():
    v = json.loads((VECTORS / "rights.json").read_text())
    for c in v["cases"]:
        assert rights.check(c["layers"], c["call"]) == c["expect"], c["name"]


def test_child_physical_refused_even_with_a_grant(hub):
    hub.identities.users["kid"].grants.append("light.set")
    assert call(hub, KID, "light.set")[0]["error"] == "denied: person: effects"


def test_a_child_gets_what_its_preset_grants_without_a_queue(hub):
    """castling-manifest-presets (Bake): "canned, don't overthink". A child has
    what the preset grants, no more and no first-use queue (drift #25)."""
    assert call(hub, KID, "homework.item.list")[0]["error"].startswith("denied: person")
    hub.identities.add_plugin_presets("homework", {"child": {"grants": ["homework.item.list"]}})
    out, _ = call(hub, KID, "homework.item.list")
    assert out["ok"] and not out.get("held")
    assert hub.tx.verdicts == ["ok"]


def test_guest_expires_after_seven_days(hub):
    visitor = Caller(person="person:visitor")
    assert call(hub, visitor, "agent.chat.send")[0]["ok"]
    hub.clock_box[0] = NOW + 7 * 86400
    assert call(hub, visitor, "agent.chat.send")[0]["error"] == "denied: person: expired"


def test_tokens_authenticate_to_a_caller():
    ids = Identities()
    tok = ids.add_token(CLAUDE_FOR_BAKE)
    assert ids.authenticate(tok) == CLAUDE_FOR_BAKE and ids.authenticate("nope") is None
    assert tok not in json.dumps(list(ids._tokens))
    ids.revoke_token(tok)
    assert ids.authenticate(tok) is None
    with pytest.raises(ValueError):
        ids.add_token(Caller())
    with pytest.raises(ValueError):
        ids.add_user("x", "wizard")
    with pytest.raises(ValueError):
        ids.add_agent("x", {"grants": "*"})


# -- holds and hard stops (10.3) ---------------------------------------------

@pytest.mark.parametrize("cap,reason", [
    ("file.delete", "destructive"), ("mail.send", "sends_as"), ("shop.buy", "spends"),
    ("odd.thing", "destructive"), ("vault.show", "reveals a secret"),
])
def test_hard_stops_hold(hub, cap, reason):
    out, s = call(hub, CLAUDE_FOR_BAKE, cap, {"p": 1})
    assert out["held"] is True and reason in out["error"] and not hub.tx.requests
    e = calls(hub)[0]
    assert e["decision"] == "held" and e["hold"] == out["hold"] and "ticket" not in e
    # not the agent, not the platform, not another owner-less person
    for by in ("agent:claude", "system:security", "person:ann"):
        assert not run(hub.release(out["hold"], by, present=True))["ok"]
    # the person, but through a dispatched cap (a job or plugin acting as them)
    assert "may not release" in run(hub.release(out["hold"], "person:bake"))["error"]
    rel = run(hub.release(out["hold"], "person:bake", present=True))
    assert rel["ok"] and rel["id"] == out["id"]
    req = hub.tx.requests[0]
    assert req["id"] == out["id"] and req["ticket"]["session"] == s.id
    assert req["ticket"]["args_hash"] == wire.args_hash({"p": 1}) and hub.tx.verdicts == ["ok"]
    released = calls(hub)[-1]
    assert released["decision"] == "released" and released["by"] == "person:bake"
    assert released["call"] == out["id"] and released["ok"] is True
    assert run(hub.release(out["hold"], "person:bake", present=True))["error"].startswith("unknown hold")


def test_sensitive_without_a_secret_is_not_a_hard_stop(hub):
    assert call(hub, BAKE, "screen.grab")[0]["ok"]


def test_hard_stop_without_a_person_needs_an_owner(hub):
    hub.identities.agents["nightly"] = {"grants": ["*"]}
    out, _ = call(hub, NIGHTLY, "file.delete")
    assert out["held"]
    assert not run(hub.release(out["hold"], "person:ann", present=True))["ok"]
    assert run(hub.release(out["hold"], "person:bake", present=True))["ok"]


def test_sacred_machine_holds_changes_not_reads(hub):
    hub.worker.sacred = True
    assert call(hub, BAKE, "file.read")[0]["ok"]
    out, _ = call(hub, BAKE, "shell.exec")
    assert out["held"] and "sacred machine" in out["error"]


def test_refused_hold_is_final(hub):
    out, _ = call(hub, BAKE, "file.delete")
    assert not hub.refuse(out["hold"], "agent:claude", present=True)["ok"]
    assert hub.refuse(out["hold"], "person:bake", present=True) == {"ok": True}
    assert calls(hub)[-1]["decision"] == "refused"
    assert not run(hub.release(out["hold"], "person:bake", present=True))["ok"] and not hub.tx.requests


def test_released_call_is_the_held_one_even_if_the_door_changes_its_args(hub):
    s = hub.start_session(BAKE, ["*"])
    args = {"path": "/tmp/x"}
    out = run(hub.dispatch(BAKE, Call(cap="file.delete", target="kaiju", args=args, why="x", session=s.id)))
    args["path"] = "/"
    assert run(hub.release(out["hold"], "person:bake", present=True))["ok"]
    assert hub.tx.requests[0]["args"] == {"path": "/tmp/x"}
    assert calls(hub)[0]["args"] == {"path": "/tmp/x"}


def test_ended_session_stays_ended_for_holds(hub):
    out, s = call(hub, BAKE, "file.delete")
    hub.end_session(s.id)
    assert run(hub.release(out["hold"], "person:bake", present=True))["error"] == "denied: session: ended"
    assert not hub.tx.requests


def test_holds_do_not_expire(hub):
    out, _ = call(hub, BAKE, "file.delete")
    hub.clock_box[0] += 3 * 86400
    assert run(hub.release(out["hold"], "person:bake", present=True))["ok"]
    t = hub.tx.requests[0]["ticket"]
    assert t["iat"] == int(hub.clock_box[0])


def test_release_rechecks_rights(hub):
    out, _ = call(hub, CLAUDE_FOR_BAKE, "file.delete")
    hub.identities.agents["claude"] = {"grants": ["shell.*"]}
    rel = run(hub.release(out["hold"], "person:bake", present=True))
    assert rel["error"] == "denied: agent: grants" and not hub.tx.requests
    assert calls(hub)[-1]["decision"] == "deny"



def test_release_sees_the_cap_and_worker_as_they_are_now(hub):
    # Held for a gate flag only; meanwhile the worker re-announces the cap as
    # destructive. Triage may release flags, never a hard stop.
    hub.gates = [G("security", "flag")]
    out, _ = call(hub, BAKE, "file.read")
    assert out["held"]
    hub.worker.caps = {**hub.worker.caps, "file.read": {**CAPS["file.read"], "effects": ["destructive"]}}
    rel = run(hub.release(out["hold"], "system:plugin.security"))
    assert "changed since it was held" in rel["error"] and not hub.tx.requests
    # the worker became sacred after the hold: same
    hub.worker.caps = dict(CAPS)
    out, _ = call(hub, BAKE, "shell.exec")
    hub.worker.sacred = True
    rel = run(hub.release(out["hold"], "system:plugin.security"))
    assert "changed since it was held" in rel["error"] and not hub.tx.requests

# -- gates (10.2) ------------------------------------------------------------

def G(name, verdict="allow", mode="enforce", reason="anomaly", **kw):
    return gates.Gate(name, lambda inp: {"verdict": verdict, "confidence": 0.9, "reason": reason},
                      mode=mode, **kw)


def test_gate_block_refuses_and_is_journaled(hub):
    hub.gates = [G("ml", "flag"), G("canary", "block", reason="canary")]
    out, _ = call(hub, BAKE, "shell.exec")
    assert out["error"] == "denied: blocked by gate canary: canary" and not hub.tx.requests
    e = calls(hub)[0]
    assert e["decision"] == "deny" and e["layer"] == "gate"
    assert e["gates"] == [{"gate": "ml", "mode": "enforce", "verdict": "flag", "confidence": 0.9,
                           "reason": "anomaly"},
                          {"gate": "canary", "mode": "enforce", "verdict": "block", "confidence": 0.9,
                           "reason": "canary"}]


def test_gate_flag_holds_and_triage_may_release(hub):
    hub.gates = [G("security", "flag")]
    out, _ = call(hub, CLAUDE_FOR_BAKE, "shell.exec")
    assert out["held"] and "gate security" in out["error"]
    assert not run(hub.release(out["hold"], "agent:claude"))["ok"]
    assert run(hub.release(out["hold"], "system:plugin.security"))["ok"]


def test_only_the_security_triage_component_releases_flags_of_its_own_gate(hub):
    # Any other system: component (a hook, a job's plugin) is not triage, and
    # triage releases its own gate's flags only (review H7).
    hub.gates = [G("security", "flag")]
    out, _ = call(hub, CLAUDE_FOR_BAKE, "shell.exec")
    for by in ("system:security", "system:hub", "system:plugin.jobs", "system:triage"):
        assert "may not release" in run(hub.release(out["hold"], by))["error"]
    hub.gates = [G("ml", "flag")]
    out, _ = call(hub, CLAUDE_FOR_BAKE, "shell.exec")
    assert "may not release" in run(hub.release(out["hold"], "system:plugin.security"))["error"]
    assert run(hub.release(out["hold"], "person:bake"))["ok"]       # a flag needs no presence


def test_hard_stops_are_never_released_by_automation(hub):
    # A person-only caller that isn't there (a job made by the person, a hub
    # plugin's ctx.call_as) can't release or refuse the person's hard stop;
    # the triage component never can (review H7, spec 10.3).
    out, _ = call(hub, BAKE, "mail.send")
    for by in ("person:bake", "system:plugin.security"):
        assert "may not release" in run(hub.release(out["hold"], by))["error"]
        assert "may not refuse" in hub.refuse(out["hold"], by)["error"]
    assert not hub.tx.requests and out["hold"] in hub.holds
    assert run(hub.release(out["hold"], "person:bake", present=True))["ok"]


def test_shadow_gates_are_recorded_but_never_count(hub):
    hub.gates = [G("ml", "block", mode="shadow")]
    out, _ = call(hub, BAKE, "shell.exec")
    assert out["ok"] and calls(hub)[0]["gates"][0]["verdict"] == "block"


def test_gate_only_sees_matching_calls(hub):
    hub.gates = [G("writes", "block", match={"risk": ["write", "exec", "admin"]}),
                 G("taint", "block", match={"effects": ["taints"]}),
                 G("shellonly", "block", match={"caps": ["shell.*"]})]
    assert call(hub, BAKE, "file.read")[0]["ok"]
    assert calls(hub)[0]["gates"] == []
    out, _ = call(hub, BAKE, "web.fetch")
    assert out["error"] == "denied: blocked by gate taint: anomaly"
    out, _ = call(hub, BAKE, "shell.exec")
    assert [g["gate"] for g in calls(hub)[-1]["gates"]] == ["writes", "shellonly"]


def test_gates_get_the_call_and_cannot_change_it(hub):
    seen = []

    def nosy(inp):
        seen.append(json.loads(json.dumps(inp)))
        inp["args"]["argv"] = ["rm", "-rf", "/"]
        inp["cap"] = "evil"
        return {"verdict": "allow", "confidence": 1, "reason": "other", "grant": "*", "verdict2": "allow"}

    hub.gates = [gates.Gate("nosy", nosy, mode="enforce")]
    out, s = call(hub, CLAUDE_FOR_BAKE, "shell.exec", {"argv": ["ls"]}, project="p", task="t_9")
    assert out["ok"] and hub.tx.requests[0]["args"] == {"argv": ["ls"]}
    assert hub.tx.requests[0]["cap"] == "shell.exec"
    assert set(calls(hub)[0]["gates"][0]) == {"gate", "mode", "verdict", "confidence", "reason"}
    assert seen[0] == {"cap": "shell.exec", "args": {"argv": ["ls"]},
                       "caller": {"person": "person:bake", "agent": "agent:claude"},
                       "worker": "kaiju", "appliance": None, "why": "because the test says so",
                       "risk": "exec", "scope": "local", "effects": [],
                       "session": {"id": s.id, "caps": ["*"], "project": "p", "task": "t_9",
                                   "tainted": False},
                       "band": BAND}


@pytest.mark.parametrize("result", [
    None, "allow", {"verdict": "maybe", "confidence": 1, "reason": "other"},
    {"verdict": "allow", "confidence": 2, "reason": "other"},
    {"verdict": "allow", "confidence": True, "reason": "other"},
    {"verdict": "allow", "confidence": 0.5, "reason": "vibes"},
])
def test_malformed_gate_result_is_a_flag(hub, result):
    hub.gates = [gates.Gate("bad", lambda inp: result, mode="enforce")]
    out, _ = call(hub, BAKE, "shell.exec")
    assert out["held"]
    assert calls(hub)[0]["gates"][0]["reason"] == "gate_error"


def test_gate_error_and_timeout_flag_within_the_wait_cap(hub):
    def boom(inp):
        raise RuntimeError("nope")

    async def slow(inp):
        await asyncio.sleep(5)

    def slow_sync(inp):
        time.sleep(1)

    hub.gates = [gates.Gate("boom", boom, mode="enforce"),
                 gates.Gate("slow", slow, budget=10, mode="enforce"),
                 gates.Gate("slowsync", slow_sync, budget=0.05, mode="enforce")]
    t0 = time.monotonic()
    out, _ = call(hub, BAKE, "shell.exec")
    assert time.monotonic() - t0 < 1.0          # never waits past 200 ms for a gate
    assert out["held"]
    assert [g["reason"] for g in calls(hub)[0]["gates"]] == ["gate_error"] * 3


def test_gate_vectors_through_run():
    v = json.loads((VECTORS / "gate.json").read_text())
    for c in v["cases"]:
        gs = []
        for r in c["results"]:
            def check(inp, r=r):
                if r.get("error"):
                    raise RuntimeError("gate failed")
                return {k: r[k] for k in ("verdict", "confidence", "reason") if k in r}
            gs.append(gates.Gate(r["gate"], check, mode=r.get("mode", "enforce")))
        inp = {"cap": "shell.exec", "risk": "exec", "effects": []}
        results = run(gates.run(gs, inp))
        assert rights.gate_decision(results) == c["expect"], c["name"]


def test_gate_from_manifest():
    g = gates.Gate.from_manifest("ml", {"run": "scripts/gate.py:check", "budget": "50ms",
                                        "match": {"risk": ["exec"]}}, lambda i: None)
    assert (g.name, g.mode, g.budget, g.match) == ("ml", "shadow", 0.05, {"risk": ["exec"]})
    assert gates.Gate.from_manifest("x", {"run": "a.py:f"}, lambda i: None).budget == gates.DEFAULT_BUDGET
    with pytest.raises(ValueError):
        gates.Gate.from_manifest("x", {"run": "a.py:f", "mode": "loud"}, lambda i: None)


def test_changing_gates_and_doors_is_an_owner_action(hub):
    for by in ("agent:claude", "system:security", "person:ann"):
        with pytest.raises(PermissionError):
            hub.set_gates([], by)
    hub.set_gates([G("ml")], "person:bake")
    assert hub.log[-1] == {"kind": "gates", "ts": NOW, "hub": hub.hub_id, "by": "person:bake",
                           "gates": [["ml", "enforce"]]}
    with pytest.raises(PermissionError):
        hub.close_door("mcp", "person:ann")
    hub.close_door("mcp", "system:security")
    assert call(hub, BAKE, "file.read", door="mcp")[0]["error"] == "denied: door mcp closed"
    assert call(hub, BAKE, "file.read", door="dashboard")[0]["ok"]
    with pytest.raises(PermissionError):
        hub.open_door("mcp", "system:security")
    hub.open_door("mcp", "person:bake")
    assert call(hub, BAKE, "file.read", door="mcp")[0]["ok"]


def test_a_closed_mcp_door_refuses_agents_on_every_door(hub):
    # The agents' door: their tokens can't walk around it (review D7).
    hub.close_door("mcp", "person:bake")
    for caller in (CLAUDE_FOR_BAKE, NIGHTLY):
        assert call(hub, caller, "file.read", door="http")[0]["error"] == "denied: door mcp closed"
    assert call(hub, BAKE, "file.read", door="http")[0]["ok"]
    hub.identities.add_component("plugin.jobs", {"grants": ["*"]})
    system = Caller(agent="system:plugin.jobs")
    assert call(hub, system, "file.read", door="http")[0]["ok"]
    assert call(hub, CLAUDE_FOR_BAKE, "file.read")[0]["ok"]          # no door: the hub's own work


# -- taint (8.4) -------------------------------------------------------------

def test_taints_cap_marks_the_session_for_good(hub):
    seen = []
    hub.gates = [gates.Gate("t", lambda i: seen.append(i["session"]["tainted"]) or
                            {"verdict": "allow", "confidence": 1, "reason": "taint"}, mode="enforce")]
    s = hub.start_session(BAKE, ["*"])
    for cap in ("file.read", "web.fetch", "file.read"):
        run(hub.dispatch(BAKE, Call(cap=cap, target="kaiju", why="x", session=s.id)))
    assert seen == [False, False, True] and s.tainted
    s2 = hub.start_session(BAKE, ["*"])
    hub.taint(s2.id)
    assert s2.tainted


def test_denied_taints_cap_does_not_taint(hub):
    out, s = call(hub, BAKE, "web.fetch", caps=["file.*"])
    assert not out["ok"] and not s.tainted


# -- sending: re-mint, retries, timeouts (6.5) -------------------------------

def test_expired_ticket_is_reminted_once(hub):
    hub.tx.answer = lambda req, n: ({"id": req["id"], "from": WID, "ok": False,
                                     "error": "denied by worker: expired"} if n == 1 else
                                    {"id": req["id"], "from": WID, "ok": True, "result": 1})
    out, _ = call(hub, BAKE, "shell.exec")
    assert out["ok"] and len(hub.tx.requests) == 2
    a, b = (r["ticket"] for r in hub.tx.requests)
    assert a["call"] == b["call"] and a["nonce"] != b["nonce"]
    assert calls(hub)[0]["ticket"] == [a["nonce"], b["nonce"]]
    hub.tx.requests.clear()
    hub.tx.answer = lambda req, n: {"id": req["id"], "from": WID, "ok": False,
                                    "error": "denied by worker: not_yet_valid"}
    out, _ = call(hub, BAKE, "shell.exec")
    assert out["error"] == "denied by worker: not_yet_valid" and len(hub.tx.requests) == 2


def _hang(req, n):
    return asyncio.sleep(30)


def test_timeout_retries_only_idempotent_safe_caps(hub, monkeypatch):
    from rook.core2.hub import dispatch as dmod
    monkeypatch.setattr(dmod, "TIMEOUT_GRACE", 0.0)
    hub.tx.answer = lambda req, n: _hang(req, n) if n == 1 else {"id": req["id"], "from": WID,
                                                                  "ok": True, "result": 2}
    out, _ = call(hub, BAKE, "file.read")           # idempotent, 1 s timeout
    assert out["ok"] and len(hub.tx.requests) == 2
    hub.tx.requests.clear()
    hub.tx.answer = _hang
    hub.worker.caps["file.delete"] = {**CAPS["file.delete"], "effects": []}
    hub.worker.caps["light.set"] = {**CAPS["light.set"], "idempotent": True, "timeout": "1s"}
    out, _ = call(hub, BAKE, "light.set")           # physical: never retried
    assert out["error"].startswith("timeout") and len(hub.tx.requests) == 1


def test_send_errors_and_bad_replies(hub):
    hub.tx.answer = lambda req, n: ConnectionError("relay down")
    assert call(hub, BAKE, "file.read")[0]["error"] == "send failed: relay down"
    hub.tx.answer = lambda req, n: {"id": req["id"], "from": "someone-else", "ok": True}
    assert call(hub, BAKE, "file.read")[0]["error"] == "reply from the wrong worker"
    hub.tx.answer = lambda req, n: {"id": req["id"], "ok": "yes"}
    assert call(hub, BAKE, "file.read")[0]["error"] == "malformed reply"
    hub.tx.answer = lambda req, n: None
    assert call(hub, BAKE, "file.read")[0]["error"] == "malformed reply"
    hub.tx.answer = lambda req, n: {"id": req["id"], "from": WID, "ok": True, "result": None,
                                    "elevated": "l_1", "retry": False}
    out, _ = call(hub, BAKE, "file.read")
    assert out == {"id": out["id"], "ok": True, "result": None, "retry": False, "elevated": "l_1"}
    assert calls(hub)[-1]["lease"] == "l_1"
    assert all(e["ok"] is False for e in calls(hub)[:4])


def test_secret_resolution_failure_is_reported_not_sent(hub):
    def fail(args, caller, cap, target=None):
        raise KeyError("pw")
    hub.secrets = fail
    out, _ = call(hub, BAKE, "file.read", {"pw": "{{secret:pw}}"})
    assert out["error"].startswith("secret:") and not hub.tx.requests


def test_resolved_args_without_canonical_form_are_reported(hub):
    hub.secrets = lambda args, caller, cap, target=None: {"x": float("inf")}
    out, _ = call(hub, BAKE, "file.read", {"x": "{{secret:x}}"})
    assert out["error"].startswith("secret:") and not hub.tx.requests


def test_bad_timeout_falls_back_to_default(hub):
    hub.worker.caps["file.read"] = {**CAPS["file.read"], "timeout": "soon"}
    assert call(hub, BAKE, "file.read")[0]["ok"]


def test_sub_call_with_junk_ticket(hub):
    for t in (None, "x", {"nonce": ["unhashable"]}, {}):
        assert run(hub.sub_call(WID, t, "web.fetch", "kaiju", None, "x"))["error"] == (
            "denied: core.call: no such call in flight")


def test_hubkey_rides_unsigned(hub):
    hub.hubkey = {"typ": "rook-hubkey", "v": 2}
    call(hub, BAKE, "file.read")
    assert hub.tx.requests[0]["ticket"]["hubkey"] == {"typ": "rook-hubkey", "v": 2}
    assert hub.tx.verdicts == ["ok"]


# -- core.call: plugin sub-calls (13) ----------------------------------------

def test_sub_call_runs_as_the_parent_with_uses(hub):
    results = []

    async def answer(req, n):
        if n == 1:
            t = req["ticket"]
            results.append(await hub.sub_call(WID, t, "web.fetch", "kaiju", None, "warm the cache"))
            results.append(await hub.sub_call(WID, t, "shell.exec", "kaiju", None, "sneaky"))
            results.append(await hub.sub_call("other", t, "web.fetch", "kaiju", None, "wrong worker"))
            forged = {**t, "why": "changed"}
            results.append(await hub.sub_call(WID, forged, "web.fetch", "kaiju", None, "forged"))
        return {"id": req["id"], "from": WID, "ok": True, "result": n}

    hub.tx.answer = answer
    out, s = call(hub, CLAUDE_FOR_BAKE, "file.read")
    assert out["ok"]
    assert results[0]["ok"] and results[1]["error"] == "denied: uses: grants"
    assert results[2]["error"] == results[3]["error"] == "denied: core.call: no such call in flight"
    sub = hub.tx.requests[1]["ticket"]
    assert (sub["person"], sub["agent"], sub["session"]) == ("person:bake", "agent:claude", s.id)
    assert s.tainted                                   # the sub-call's taint lands on the session
    after = run(hub.sub_call(WID, hub.tx.requests[0]["ticket"], "web.fetch", "kaiju", None, "late"))
    assert after["error"] == "denied: core.call: no such call in flight"


def test_gate_result_check_withholds_a_result_it_blocks(hub):
    """A gate's result check (canaries) sees what came back; block turns the
    reply into a refusal, journaled with stage: result. Flags only count as
    journal entries: the call already ran."""
    seen = []

    def result(inp):
        seen.append(inp)
        bad = "AKIAFAKE" in json.dumps(inp["result"])
        return {"verdict": "block" if bad else "flag", "confidence": 1.0, "reason": "canary"}

    hub.gates = [gates.Gate("canary", lambda inp: {"verdict": "allow", "confidence": 1.0, "reason": "other"},
                            mode="enforce", result=result),
                 G("other", "allow")]
    out, s = call(hub, BAKE, "file.read", {"path": "/x"})
    assert out["ok"] and out["result"] == {"cap": "file.read"}           # a flag doesn't withhold
    assert seen[0]["result"] == {"cap": "file.read"} and seen[0]["session"]["id"] == s.id
    e = calls(hub)[-1]
    assert [(g["gate"], g.get("stage")) for g in e["gates"]] == [("canary", None), ("other", None),
                                                                  ("canary", "result")]
    assert e["ok"] is True
    hub.tx.answer = lambda req, n: {"id": req["id"], "from": WID, "ok": True, "result": {"key": "AKIAFAKE1"}}
    out, _ = call(hub, BAKE, "file.read", {"path": "/y"})
    assert out == {"id": out["id"], "ok": False, "error": "denied: blocked by gate canary: canary"}
    e = calls(hub)[-1]
    assert e["ok"] is False and e["error"] == out["error"] and e["gates"][-1]["verdict"] == "block"


def test_gate_result_check_shadow_and_failed_calls(hub):
    hub.gates = [gates.Gate("ml", lambda inp: {"verdict": "allow", "confidence": 1.0, "reason": "other"},
                            mode="shadow",
                            result=lambda inp: {"verdict": "block", "confidence": 1.0, "reason": "canary"})]
    out, _ = call(hub, BAKE, "file.read", {"path": "/x"})
    assert out["ok"] and calls(hub)[-1]["gates"][-1]["stage"] == "result"    # shadow: journaled only
    hub.gates[0].mode = "enforce"
    hub.tx.answer = lambda req, n: {"id": req["id"], "from": WID, "ok": False, "error": "nope"}
    out, _ = call(hub, BAKE, "file.read", {"path": "/x"})
    assert out["error"] == "nope" and "stage" not in calls(hub)[-1]["gates"][-1]    # nothing came back


def test_gate_from_manifest_with_a_result_check():
    g = gates.Gate.from_manifest("canary", {"run": "s.py:a", "result": "s.py:b"}, len, str)
    assert g.check is len and g.result is str


# -- crash guard (review H11) --------------------------------------------------

def test_a_crash_inside_dispatch_is_an_error_reply_and_still_journaled(hub):
    hub.worker.caps["bad.cap"] = ["not", "a", "mapping"]          # malformed cap data
    out, s = call(hub, BAKE, "bad.cap")
    assert out == {"id": out["id"], "ok": False, "error": "internal error"}
    e = calls(hub)[-1]
    assert (e["call"], e["ok"], e["error"], e["decision"], e["session"]) == (out["id"], False, "internal", "deny", s.id)

    def boom(*args):
        raise RuntimeError("identities broke")
    hub.identities.layers = boom
    out, _ = call(hub, BAKE, "file.read")
    assert out["error"] == "internal error" and calls(hub)[-1]["error"] == "internal"


def test_a_failing_journal_never_hides_a_result_that_ran(hub):
    def broken(entry):
        raise OSError("disk full")
    hub.journal = broken
    out, _ = call(hub, BAKE, "file.read")
    assert out["ok"] and out["result"] == {"cap": "file.read"} and len(hub.tx.requests) == 1


def test_a_crash_while_releasing_is_journaled(hub):
    hub.gates = [G("ml", "flag")]
    out, _ = call(hub, BAKE, "shell.exec")

    def boom(*a):
        raise RuntimeError("mint broke")
    hub._mint = boom
    rel = run(hub.release(out["hold"], "person:bake"))
    assert rel == {"id": out["id"], "ok": False, "error": "internal error"}
    e = calls(hub)[-1]
    assert (e["decision"], e["ok"], e["error"]) == ("released", False, "internal")


# -- appliances (5.7): by name, delegates, carry ------------------------------

WID2 = "4f0a9c1d2e3f40516273849506a7b8c9"


def two_delegates(hub):
    """den-lamp, delegated by kaiju (known-good) and janitor; both verify tickets."""
    w2 = Worker(WID2, "janitor", BAND, dict(CAPS), {"grants": ["*"]}, plugins={"light": ["light.set"]})
    hub.workers[WID2] = w2
    lamps(hub, "den-lamp", delegates=(WID, WID2))
    janitor = dict(hub.tx.worker, worker_id=WID2, name="janitor")
    replay = wire.ReplayCache()

    async def send(band, request):
        if request["target"] == WID2:
            hub.tx.requests.append(json.loads(json.dumps(request)))
            ok, reason = wire.verify_ticket(request["ticket"], request, janitor, int(NOW), replay)
            hub.tx.verdicts.append(reason)
            return {"id": request["id"], "from": WID2, "ok": True, "result": {"by": "janitor"}}
        return await hub.tx(band, request)
    hub.send = send
    return w2


def test_an_appliance_is_called_by_name_through_its_delegate(hub):
    lamps(hub, "den-lamp")
    hub.carry = lambda w, cap, app: ({"instance": {"name": app, "host": "10.0.0.9", "settings": {"key": "k3y"}}},
                                     ["k3y"])
    hub.tx.answer = lambda req, n: {"id": req["id"], "from": WID, "ok": True, "result": {"echo": "k3y"}}
    out, _ = call(hub, BAKE, "light.set", target="den-lamp")
    assert out["ok"] and out["result"] == {"echo": "[redacted]"}, out
    req = hub.tx.requests[-1]
    assert req["target"] == WID and req["appliance"] == "den-lamp" and hub.tx.verdicts[-1] == "ok"
    assert req["ticket"]["carry_hash"] == wire.args_hash(req["carry"])
    [e] = calls(hub)
    assert e["appliance"] == "den-lamp" and "k3y" not in json.dumps(e)
    # only the appliance plugin's caps
    assert call(hub, BAKE, "shell.exec", target="den-lamp")[0]["error"] == "unknown capability: shell.exec"
    assert call(hub, BAKE, "light.set", target="nowhere")[0]["error"] == "unknown worker: nowhere"


def test_a_carry_the_worker_didnt_get_is_refused(hub):
    req = {"id": "a" * 32, "cap": "light.set", "args": {}, "appliance": "den-lamp",
           "carry": {"instance": {"name": "den-lamp"}}}
    lamps(hub, "den-lamp")
    hub.carry = lambda w, cap, app: (req["carry"], [])
    out, _ = call(hub, BAKE, "light.set", target="den-lamp")
    sent = hub.tx.requests[-1]
    ok, reason = wire.verify_ticket(sent["ticket"], {**sent, "carry": {"instance": {"name": "evil"}}},
                                    hub.tx.worker, int(NOW), wire.ReplayCache())
    assert (ok, reason) == (False, "wrong_args")
    ok, reason = wire.verify_ticket(sent["ticket"], {k: v for k, v in sent.items() if k != "carry"},
                                    hub.tx.worker, int(NOW), wire.ReplayCache())
    assert (ok, reason) == (False, "wrong_args")


def test_a_secret_the_carry_step_cant_find_fails_before_sending(hub):
    lamps(hub, "den-lamp")

    def carry(w, cap, app):
        raise ValueError("setting light.cloud: secret nope is not in the vault")
    hub.carry = carry
    out, _ = call(hub, BAKE, "light.set", target="den-lamp")
    assert out["error"] == "secret: setting light.cloud: secret nope is not in the vault"
    assert not hub.tx.requests


def test_offline_known_good_delegate_is_tried_last_and_what_worked_is_remembered(hub):
    two_delegates(hub)
    hub.online = lambda wid: wid != WID
    out, _ = call(hub, BAKE, "light.set", target="den-lamp")
    assert out["ok"] and out["result"] == {"by": "janitor"}
    assert hub.appliances.items["den-lamp"]["delegates"] == [WID2, WID]
    # none online: still tried, known-good first (status is only a hint)
    hub.online = lambda wid: False
    assert call(hub, BAKE, "light.set", target="den-lamp")[0]["ok"]
    assert hub.tx.requests[-1]["target"] == WID2


def test_a_failed_send_moves_on_to_the_next_delegate_in_the_same_request(hub):
    two_delegates(hub)
    hub.tx.answer = lambda req, n: OSError("no route")
    out, _ = call(hub, BAKE, "light.set", target="den-lamp")
    assert out["ok"] and out["result"] == {"by": "janitor"}, out
    [e] = calls(hub)
    assert e["routes"] == [WID, WID2] and e["worker"] == WID2 and len(e["ticket"]) == 2
    assert hub.tx.requests[-1]["ticket"]["worker"] == WID2 and hub.tx.verdicts[-1] == "ok"
    assert hub.appliances.worked_calls == [("den-lamp", WID2)]


def test_a_physical_call_that_timed_out_is_not_sent_again(hub):
    two_delegates(hub)
    CAPS_T = dict(CAPS["light.set"], timeout="1s")
    for w in hub.workers.values():
        w.caps["light.set"] = CAPS_T

    async def slow(req, n):
        await asyncio.sleep(10)
    hub.tx.answer = slow
    out, _ = call(hub, BAKE, "light.set", target="den-lamp")
    assert out["error"] == "timeout: the call may still be running"
    assert [r["target"] for r in hub.tx.requests] == [WID]


def test_the_next_delegate_must_pass_rights_too(hub):
    w2 = two_delegates(hub)
    w2.ceiling = {"grants": ["file.*"]}
    hub.tx.answer = lambda req, n: OSError("no route")
    out, _ = call(hub, BAKE, "light.set", target="den-lamp")
    assert out["error"] == "send failed: no route"
    assert [r["target"] for r in hub.tx.requests] == [WID]


def test_a_delegate_can_be_pinned_and_only_a_delegate(hub):
    two_delegates(hub)
    assert call(hub, BAKE, "light.set", target="janitor", appliance="den-lamp")[0]["result"] == {"by": "janitor"}
    hub.workers["9" * 32] = Worker("9" * 32, "attic", BAND, dict(CAPS), {"grants": ["*"]},
                                   plugins={"light": ["light.set"]})
    out, _ = call(hub, BAKE, "light.set", target="attic", appliance="den-lamp")
    assert out["error"] == "attic is not a delegate of appliance den-lamp"
    hub.workers[WID].plugins.clear()
    hub.workers[WID2].plugins.clear()
    assert call(hub, BAKE, "light.set", target="den-lamp")[0]["error"] == \
        "appliance den-lamp: no delegate serves light now"
