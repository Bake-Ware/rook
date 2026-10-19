"""Huddles H5 (docs/design/huddles.md 4.2, 6): job agent steps run by a role
(``agent: "role:<id>"``), the ``huddle`` step kind (one round, the synthesis
delivered), a role's sources read at the hub before its turn, and the lead
developer's session cards."""
from __future__ import annotations

import time

import pytest

from rook.hub.plugins.huddle.chair import CardError, validate_card
from rook.hub.plugins.jobs.model import ValidationError, check
from rook.hub.plugins.roles import model as role_model
from tests.jobs_fakes import FakeRuntime, execute, job_doc, ok
from tests.test_huddle_chair import _call, _roles, _settle, hub  # noqa: F401 - fixture


def _rt(hub, tmp_path):
    rt = FakeRuntime(tmp_path, caps={"notify.send": lambda args, wid: ok({"sent": True})})
    rt.roles = hub.roles
    rt.huddles = hub.svc
    return rt


@pytest.mark.asyncio
async def test_a_role_works_a_job_step_with_its_sources_read_first(hub, tmp_path):
    await _roles(hub, sources=[{"cap": "info.ping", "args": {"x": 1}}])
    hub.band.script = {"job-qa": [{"text": "All green."}]}
    rt = _rt(hub, tmp_path)
    res = await execute(rt, {"a": {"kind": "agent", "agent": "role:qa", "mode": "wait",
                                   "prompt": "Anything failing?"}})
    rec = res["steps"]["a"]
    out = rec["output"]
    assert out["agent"] == "agent:home/role:qa" and out["text"] == "All green."
    turn = hub.band.turns[-1]
    assert turn["kind"] == "job-qa" and "[job:j_test] Anything failing?" in turn["args"]["prompt"]
    assert "What your sources say now" in turn["args"]["prompt"]
    assert 'info.ping on box: {"pong":true' in turn["args"]["prompt"]
    ping = [c for c in hub.band.calls if c.cap == "info.ping"][-1]
    assert ping.identity == "agent:home/role:qa" and ping.args == {"x": 1}
    # Unknown roles and bad specs are refused.
    with pytest.raises(ValidationError):
        check(job_doc({"a": {"kind": "agent", "agent": "role:Not OK", "prompt": "x"}}))
    res = await execute(rt, {"a": {"kind": "agent", "agent": "role:ops", "prompt": "x", "mode": "wait"}})
    rec = res["steps"]["a"]
    assert rec["error"].startswith("no role")


@pytest.mark.asyncio
async def test_a_huddle_step_runs_one_round_and_delivers_the_synthesis(hub, tmp_path):
    await _roles(hub)
    hub.band.script = {
        "huddle-chair": [{"tools": [("call_on", {"calls": [{"role": "qa", "ask": "Failing?"},
                                                           {"role": "pm", "ask": "Next?"}]})]}],
        "huddle-qa": [{"text": "One flaky job.", "tools": [("propose", {
            "kind": "job.run", "summary": "Rerun nightly", "worker": "box", "cap": "info.ping"})]}],
        "huddle-pm": [{"text": "Deck is clear."}],
        "huddle-close": [{"text": "One flaky job; deck clear. Rerun the nightly."}]}
    rt = _rt(hub, tmp_path)
    res = await execute(rt, {"s": {"kind": "huddle", "prompt": "What's on deck?", "roles": ["qa", "pm"],
                                   "deliver": "telegram", "title": "Standup"}}, name="standup")
    rec = res["steps"]["s"]
    out = rec["output"]
    assert out["state"] == "done" and out["text"] == "One flaky job; deck clear. Rerun the nightly."
    assert [c["summary"] for c in out["cards"]] == ["Rerun nightly"]
    assert out["delivered"]["ok"]
    cap, args, target, _who = [c for c in rt.calls if c[0] == "notify.send"][-1]
    assert "1 action card(s) wait for you" in args["text"] and "Rerun nightly" in args["text"]
    h = next(x for x in hub.svc.huddles() if x["title"].startswith("Standup "))
    assert h["roles"] == ["qa", "pm"] and h["open_cards"] == 1
    assert hub.svc.chat.tail(h["room"], 1)[0]["sender"] == "agent:home"
    assert hub.svc.chat.tail(h["room"], 10)[0]["sender"] == "job:j_test"
    for bad in ({"kind": "huddle"}, {"kind": "huddle", "prompt": "x", "deliver": "fax"},
                {"kind": "huddle", "prompt": "x", "roles": "qa"}):
        with pytest.raises(ValidationError):
            check(job_doc({"s": bad}))


@pytest.mark.asyncio
async def test_the_lead_reads_sessions_across_hosts_and_proposes_session_cards(hub):
    lead = next(p for p in role_model.presets() if p["id"] == "lead")
    assert lead["sources"] == [{"cap": "sessions.list", "args": {"live_only": True, "limit": 15}}]
    for wid, name in (("w-dev1", "dev1"), ("w-dev2", "dev2")):
        hub.band.workers[wid] = {"name": name, "caps": ["sessions.list", "sessions.send"],
                                 "tiers": {"sessions.list": "read", "sessions.send": "exec"},
                                 "last_seen": time.time()}
    await _call(hub, "role.set", {"role": lead})
    h = await _call(hub, "huddle.start", {"title": "Dev", "roles": ["lead"]})
    hub.band.script = {"huddle-lead": [{"text": "dev2's session is stuck.", "tools": [
        ("propose", {"kind": "session.send", "summary": "Nudge it", "worker": "dev2",
                     "cap": "sessions.send", "args": {"agent": "claude", "native_id": "s1",
                                                      "text": "Are you blocked?"}}),
        ("propose", {"kind": "session.send", "summary": "Bad", "worker": "dev2",
                     "cap": "shell.exec", "args": {"native_id": "s1", "text": "x"}})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@lead how are the sessions?"})
    await _settle(hub)
    turn = hub.band.turns[-1]
    assert "sessions.list on dev1" in turn["args"]["prompt"] and "sessions.list on dev2" in turn["args"]["prompt"]
    assert [c.args for c in hub.band.calls if c.cap == "sessions.list"] == [
        {"live_only": True, "limit": 15}] * 2
    assert not turn["seen"][1]["ok"] and "runs sessions.send" in turn["seen"][1]["error"]
    card, = (await _call(hub, "huddle.read", {"room": h["room"]}))["cards"]
    out = await _call(hub, "huddle.card", {"huddle": h["room"], "card": card["id"], "action": "approve"})
    assert out["state"] == "done"
    send = [c for c in hub.band.calls if c.cap == "sessions.send"][-1]
    assert send.target == "w-dev2" and send.identity == "user:operator"


def test_session_cards_are_checked():
    with pytest.raises(CardError):
        validate_card({"kind": "session.start", "summary": "x", "worker": "w",
                       "cap": "work.stream.open", "args": {"harness": "claude"}})
    ok_card = validate_card({"kind": "session.start", "summary": "Start on the bug", "worker": "w",
                             "cap": "work.stream.open",
                             "args": {"harness": "claude", "cwd": "/src/app", "task": "t_1"}})
    assert ok_card["call"]["cap"] == "work.stream.open"
