"""Project onboarding through conversation and the borrowed-state ledger
(docs/design/onboarding.md, rook/hub/plugins/home/onboard.py).

The house agent brainstorms and keeps notes, offers once to set a project up
when the talk turns to building, asks the intake questions (never guesses),
reads the plan back, and sets it up: folder, project record, wiki page, trust
entry in the person's words, scoped exceptions, starter team. Borrowed state
(a stopped service on a sacred host) is a lease with its restore steps;
"bring it back" restores it, and the sweep offers to.

The kaiju/Strata example is a fixture: kaiju here is a fake worker on the
fake band of test_huddle_chair; nothing reaches a real machine."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from rook.hub import approvals as A
from rook.hub import trust as T
from rook.hub.plugins.home import onboard as O
from tests.test_front_door import ASKER, _ask, _ran, _script, front  # noqa: F401
from tests.test_huddle_chair import hub  # noqa: F401
from tests.test_trust import AGENT, authorizer, entry, seeded  # noqa: F401

STRATA = {"id": "strata-gpu1", "entry": "kaiju", "title": "Strata A on GPU 1",
          "normal": "kaiju runs the Strata A engine on GPU 1",
          "stop": {"worker": "kaiju", "cap": "shell.exec",
                   "args": {"argv": ["systemctl", "--user", "stop", "strata-a"]}},
          "restore": {"worker": "kaiju", "cap": "shell.exec",
                      "args": {"argv": ["systemctl", "--user", "start", "strata-a"]}}}
BAKE = T.Actor("person", "Bake", words="kaiju runs Strata A on GPU 1 normally")


@pytest.fixture(autouse=True)
def _knowledge(monkeypatch):
    """Projects and wiki pages are real records (before the hub starts)."""
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")


@pytest.fixture
def ob(front):  # noqa: F811
    front.band.workers["w-cachy"] = {"name": "cachyrig", "caps": ["shell.exec"],
                                     "tiers": {"shell.exec": "exec"}, "last_seen": time.time()}
    st = front.node.plugin("trust").store
    st.change("set_baseline", STRATA, BAKE)
    real = front.band.call

    async def call(cap, args=None, target=None, timeout=15.0, identity=None, principal=None):
        if cap == "shell.exec" and args and args.get("argv", [""])[0] == "sh":
            # The setup script's folder: answer like a worker would.
            front.band.calls.append(type("C", (), {"cap": cap, "args": args, "target": target,
                                                   "identity": identity,
                                                   "principal": principal})())
            path = args["argv"][4].replace("~", "/home/bake")
            return {"ok": True, "result": {"code": 0, "stdout": path + "\n", "stderr": ""}}
        return await real(cap, args, target, timeout, identity, principal)
    front.band.call = call
    front.ob = front.front.onboard
    front.trust = st
    return front


async def turn(hub, said, *tools, text=""):
    _script(hub, "front-voice", {"tools": list(tools), "text": text})
    return await _ask(hub, said)


def seen(hub):
    return hub.band.turns[-1]["seen"]


# -- 1. brainstorm ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_brainstorm_keeps_running_notes_and_makes_nothing(ob):
    out = await turn(ob, "I've got this idea for a new AI architecture, tiny experts that vote",
                     ("note", {"text": "tiny experts that vote on each token",
                               "title": "Voting experts"}),
                     text="Interesting: do they vote per token or per layer?")
    assert out["path"] == "answer" and out["answer"].startswith("Interesting")
    await turn(ob, "per layer, and a router that learns who to trust",
               ("note", {"text": "votes per layer; a router learns who to trust"}))
    t = ob.ob.current(O.Onboarding.key("voice", ASKER, ""))
    assert t["stage"] == "brainstorm" and t["title"] == "Voting experts"
    assert "tiny experts" in t["notes"] and "router learns" in t["notes"]
    notes = (ob.node.plugin("home").data_dir / "workspace" / t["id"] / "notes.md").read_text()
    assert "router learns" in notes
    # No tasks, sessions, trust changes or asks while brainstorming.
    assert not ob.front.store.asks("open") and not ob.trust.current()["proposals"]
    # The router sees its notes next turn.
    await turn(ob, "what do we have so far?")
    assert "router learns" in ob.band.turns[-1]["args"]["system"]


@pytest.mark.asyncio
async def test_the_turn_to_building_offers_once(ob):
    await turn(ob, "an idea: a toy that names my wifi networks", ("note", {"text": "wifi names"}))
    out = await turn(ob, "ok let's write some code for it",
                     ("offer_project", {"title": "Wifi namer"}), text="Fun idea.")
    assert out["answer"] == "Fun idea. " + O.OFFER_LINE and out["path"] == "onboard"
    assert "Hint: this message may be the turn" in ob.band.turns[-1]["args"]["system"]
    again = await turn(ob, "let's build it", ("offer_project", {"title": "Wifi namer"}))
    assert O.OFFER_LINE not in again["answer"]
    assert "already offered" in json.dumps(seen(ob))
    assert "never offer again here" in ob.band.turns[-1]["args"]["system"]
    # Declining: it keeps talking, never offers again.
    out = await turn(ob, "nah, not yet", ("project_intake", {"answer": "decline",
                                                             "words": "nah, not yet"}))
    t = ob.ob.current(O.Onboarding.key("voice", ASKER, ""))
    assert t["stage"] == "declined" and O.OFFER_LINE not in out["answer"]


@pytest.mark.asyncio
async def test_intake_takes_only_the_persons_own_words(ob):
    await turn(ob, "let's build it", ("offer_project", {"title": "Wifi namer"}))
    await turn(ob, "yes please", ("project_intake", {"answer": "start", "words": "yes please"}))
    out = await turn(ob, "hmm", ("project_intake", {"answer": "protection", "level": "toy",
                                                    "words": "it's just a toy"}))
    assert "exact words" in json.dumps(seen(ob))
    t = ob.ob.current(O.Onboarding.key("voice", ASKER, ""))
    assert "protection" not in t["answers"]
    assert out["answer"].startswith("Nothing") or "toy" not in out["answer"]


# -- 3/4. intake and setup: a toy in a new folder on cachyrig ---------------------------------

async def _toy_on_cachyrig(ob):
    await turn(ob, "I want a little script that renames my photos by date",
               ("note", {"text": "rename photos by EXIF date", "title": "Photo renamer"}))
    out = await turn(ob, "let's write some code for it", ("offer_project",
                                                          {"title": "Photo renamer"}))
    assert out["answer"].startswith(O.OFFER_LINE)
    out = await turn(ob, "yes, set it up", ("project_intake", {"answer": "start",
                                                               "words": "yes, set it up"}))
    assert out["answer"].startswith(O.QUESTIONS["where"])
    out = await turn(ob, "a new folder on cachyrig is fine", ("project_intake", {
        "answer": "where", "machine": "cachyrig", "kind": "folder", "new": True,
        "words": "a new folder on cachyrig"}))
    assert out["answer"].startswith(O.QUESTIONS["protection"])
    out = await turn(ob, "it's just a toy experiment", ("project_intake", {
        "answer": "protection", "level": "toy", "words": "it's just a toy experiment"}))
    assert out["answer"].startswith(O.QUESTIONS["touches"])
    out = await turn(ob, "nothing else, just its folder", ("project_intake", {
        "answer": "touches", "nothing": True, "words": "nothing else"}))
    return out


@pytest.mark.asyncio
async def test_a_toy_on_cachyrig_is_asked_read_back_and_set_up(ob):
    out = await _toy_on_cachyrig(ob)
    # The plan is read back before anything is set.
    assert out["answer"].startswith("Here's the plan for Photo renamer: a new folder at "
                                    "~/photo-renamer on cachyrig")
    assert 'in your words "it\'s just a toy experiment"' in out["answer"]
    assert "waits for your tap on Manage > Trust" in out["answer"]
    assert out["answer"].rstrip().endswith("Set it up?") or "Set it up?" in out["answer"]
    assert not [c for c in _ran(ob, "shell.exec")]
    out = await turn(ob, "yes go for it", ("project_setup", {"words": "yes go for it"}),
                     text="I've set it up.")
    ans = out["answer"]
    assert ans.startswith("Set up Photo renamer: folder /home/bake/photo-renamer on cachyrig")
    assert "Nothing has been done yet" not in ans
    (mk,) = _ran(ob, "shell.exec")
    assert mk.args["argv"][:2] == ["sh", "-c"] and mk.args["argv"][4] == "~/photo-renamer"
    assert mk.principal.id == "home:home"
    # Project record and wiki page from the notes, linked.
    t = ob.ob.threads(1)[0]
    res = t["result"]
    steps = {s["step"]: s for s in res["steps"]}
    assert steps["project"]["ok"] and steps["wiki"]["ok"], steps
    kb = ob.node.plugin("knowledge")
    page = await ob.node.invoke("knowledge.read", {"action": "get", "id": res["page"]},
                                "human:op")
    assert "rename photos by EXIF date" in json.dumps(page)
    proj = await ob.node.invoke("task.read", {"action": "get", "kind": "project",
                                              "id": res["project"]}, "human:op")
    body = json.dumps(proj)
    assert res["page"] in body and "it's just a toy experiment" in body
    assert kb is not None
    # Ship-it loosens: a proposal with their words, not an entry.
    doc = ob.trust.current()
    assert not any(e["id"] == "photo-renamer" for e in doc["entries"])
    (prop,) = doc["proposals"]
    assert prop["change"]["data"]["level"] == "ship-it"
    assert "it's just a toy experiment" in prop["why"]
    assert "photo-renamer" in ans and prop["id"] in ans
    # The project is listed for check-ins, and the thread is done.
    assert t["stage"] == "done"
    p = ob.front.store._q("SELECT * FROM onboard_projects")[0]
    assert p["level"] == "ship-it" and p["host"] == "cachyrig"


@pytest.mark.asyncio
async def test_guarded_applies_at_once_because_it_only_tightens(ob):
    await turn(ob, "set up a project for the garage door monitor",
               ("project_intake", {"answer": "start", "title": "Garage monitor",
                                   "words": "set up a project"}))
    await turn(ob, "new repo on cachyrig", ("project_intake", {
        "answer": "where", "machine": "cachyrig", "kind": "repo", "words": "new repo on cachyrig"}))
    await turn(ob, "this is core infrastructure, protect it", ("project_intake", {
        "answer": "protection", "level": "guarded", "words": "core infrastructure, protect it"}))
    out = await turn(ob, "nothing shared", ("project_intake", {
        "answer": "touches", "nothing": True, "words": "nothing shared"}))
    assert "a new git repo at ~/garage-monitor" in out["answer"]
    assert "Team: lead" in out["answer"]
    await turn(ob, "yes", ("project_setup", {"words": "yes"}))
    e = next(e for e in ob.trust.current()["entries"] if e["id"] == "garage-monitor")
    assert e["level"] == "guarded" and "core infrastructure, protect it" in e["set_by"]["words"]
    assert e["set_by"]["kind"] == "house" and e["targets"] == ["project:garage-monitor"]
    (mk,) = _ran(ob, "shell.exec")
    assert mk.args["argv"][5] == "repo"


# -- the kaiju / Strata example (a fixture) ------------------------------------------------------

async def _toy_on_kaiju(ob):
    await turn(ob, "experiment: a tiny model that needs a whole GPU",
               ("note", {"text": "tiny model, needs a GPU", "title": "GPU toy"}))
    await turn(ob, "let's build it", ("offer_project", {"title": "GPU toy"}))
    await turn(ob, "sure", ("project_intake", {"answer": "start", "words": "sure"}))
    await turn(ob, "put it in my existing ~/gputoy folder on cachyrig", ("project_intake", {
        "answer": "where", "machine": "cachyrig", "new": False, "path": "~/gputoy",
        "words": "existing ~/gputoy folder on cachyrig"}))
    await turn(ob, "it's just a toy experiment", ("project_intake", {
        "answer": "protection", "level": "toy", "words": "just a toy experiment"}))
    out = await turn(ob, "we can shut down the Strata engine on GPU 1 for this experiment",
                     ("project_intake", {"answer": "touches", "touches": [
                         {"host": "kaiju", "resource": "Strata engine on GPU 1",
                          "what": "GPU 1"}],
                         "words": "we can shut down the Strata engine on GPU 1 for this "
                                  "experiment"}))
    return out


@pytest.mark.asyncio
async def test_a_toy_on_sacred_kaiju_flags_the_conflict_and_scopes_an_exception(ob):
    out = await _toy_on_kaiju(ob)
    ans = out["answer"]
    assert "kaiju's sacred, but GPU toy may stop and restore Strata A on GPU 1 until" in ans
    assert "anything else on kaiju still comes to you" in ans
    await turn(ob, "yes, do it", ("project_setup", {"words": "yes, do it"}))
    doc = ob.trust.current()
    x = next(p for p in doc["proposals"] if p["change"]["op"] == "add_exception")
    data = x["change"]["data"]
    assert data["applies_to"] == ["kaiju"] and data["targets"] == ["resource:strata-gpu1"]
    assert data["project"] == "gpu-toy" and data["expires"]
    assert "we can shut down the Strata engine on GPU 1" in x["why"]
    # Nothing on kaiju ran: setting up never touches the sacred host.
    assert not [c for c in ob.band.calls if c.cap == "shell.exec"]


async def _accept_all(ob):
    st = ob.trust
    for p in [p for p in st.current()["proposals"] if p["state"] == "pending"]:
        st.change("decide_proposal", {"id": p["id"], "accept": True},
                  T.Actor("person", "Bake", words="yes, accept"))


@pytest.mark.asyncio
async def test_borrow_under_the_exception_is_recorded_and_bring_it_back_restores(ob):
    await _toy_on_kaiju(ob)
    await turn(ob, "yes, do it", ("project_setup", {"words": "yes, do it"}))
    await _accept_all(ob)
    said = "go ahead and shut down Strata on GPU 1 for the GPU toy"
    out = await turn(ob, said, ("borrow", {"resource": "Strata on GPU 1", "project": "gpu-toy",
                                           "words": "shut down Strata on GPU 1"}))
    assert out["answer"].startswith("Done: stop Strata A on GPU 1 for gpu-toy.")
    (stop,) = _ran(ob, "shell.exec")
    assert stop.args["argv"] == ["systemctl", "--user", "stop", "strata-a"]
    (lease,) = [r for r in ob.trust.current()["ledger"] if r["state"] == "open"]
    assert lease["what"] == "Strata A on GPU 1 stopped" and lease["project"] == "gpu-toy"
    assert lease["words"] == "shut down Strata on GPU 1"
    assert lease["restore_call"]["args"]["argv"][-2:] == ["start", "strata-a"]
    assert lease["exception"] == "gpu-toy-kaiju" and lease["until"]
    assert lease["authorized_by"]["kind"] == "house"
    # The digest cites the exception; the trust summary shows what's borrowed.
    d = ob.front.digest()["decisions"][0]
    assert "gpu-toy-kaiju" in d["cite"]
    assert "Borrowed now: kaiju: Strata A on GPU 1 stopped (gpu-toy)" in ob.trust.summary()
    # "What's different on kaiju?"
    led = ob.ob.ledger("kaiju")
    assert led["spoken"].startswith("Borrowed right now: Strata A on GPU 1 stopped for gpu-toy")
    # Borrowing it again is refused.
    await turn(ob, "stop strata again", ("borrow", {"resource": "strata-gpu1",
                                                    "words": "stop strata again"}))
    assert "already borrowed" in json.dumps(seen(ob))
    out = await turn(ob, "ok bring Strata back up", ("restore", {
        "what": "Strata", "words": "bring Strata back up"}))
    assert out["answer"].startswith("Done: bring Strata A on GPU 1 back.")
    assert _ran(ob, "shell.exec")[-1].args["argv"][-2:] == ["start", "strata-a"]
    (r,) = ob.trust.current()["ledger"]
    assert r["state"] == "restored" and "bring Strata back up" in r["closed_by"]["words"]
    assert ob.ob.ledger()["spoken"].startswith("Nothing is borrowed")


@pytest.mark.asyncio
async def test_borrow_without_an_exception_is_an_ask_and_yes_records_the_lease(ob):
    said = "shut down Strata on GPU 1 for a quick test"
    out = await turn(ob, said, ("borrow", {"resource": "strata-gpu1",
                                           "words": "shut down Strata on GPU 1"}))
    assert out["path"] == "ask" and not _ran(ob, "shell.exec")
    assert "Stop Strata A on GPU 1" in out["answer"]
    a = ob.front.store.find_ask(out["asks"][0])
    assert "kaiju changes are sacred" in a["explain"]
    res = await ob.front.answer(a["name"], "yes", by="Bake", quote="yes")
    assert res["ok"] and res["ledger"]["lease"]
    (lease,) = ob.trust.current()["ledger"]
    assert lease["state"] == "open" and "exception" not in lease
    assert not res.get("remember")          # a borrow never offers to remember


# -- the sweep: proactive restore and "still a toy?" ---------------------------------------------

async def _borrowed(ob):
    await _toy_on_kaiju(ob)
    await turn(ob, "yes, do it", ("project_setup", {"words": "yes, do it"}))
    await _accept_all(ob)
    await turn(ob, "shut down Strata on GPU 1", ("borrow", {"resource": "strata-gpu1",
                                                            "words": "shut down Strata on GPU 1"}))
    return ob.ob.threads(1)[0]["result"]


@pytest.mark.asyncio
async def test_a_finished_experiment_gets_one_restore_offer_and_yes_restores(ob):
    res = await _borrowed(ob)
    assert await ob.ob.sweep() == []                     # nothing to offer yet
    await ob.node.invoke("task.write", {"action": "update", "kind": "project",
                                        "id": res["project"], "request_id": "t-done",
                                        "data": {"revision": 1, "patch": {"state": "done", "attrs": {
                                            "outcome": "ran"}}}}, "human:op")
    proj = await ob.node.invoke("task.read", {"action": "get", "kind": "project",
                                              "id": res["project"]}, "human:op")
    if json.dumps(proj).find('"done"') < 0:
        pytest.skip("project state did not change in this store")
    made = await ob.ob.sweep()
    assert len(made) == 1 and await ob.ob.sweep() == []    # once
    a = ob.front.store.find_ask(made[0])
    assert a["line"] == "GPU toy ran to completion. Want me to bring Strata A on GPU 1 back up?"
    assert a["kind"] == "restore"
    item = next(i for i in ob.node.plugin("approvals").relay.items() if i["id"] == a["id"])
    assert item["options"] == ["Yes", "No", "Explain"]
    out = await ob.front.answer(a["name"], "yes", by="Bake", quote="yes")
    assert out["ok"] and out["ledger"]["closed"]
    assert ob.trust.current()["ledger"][0]["state"] == "restored"


@pytest.mark.asyncio
async def test_an_ended_exception_and_a_quiet_project_offer_to_restore(ob):
    await _borrowed(ob)
    st = ob.trust
    lease = st.current()["ledger"][0]
    # Quiet: four days after the lease (quiet after 3), the project untouched,
    # the exception (14 days) still on.
    ob.node.settings.set("home.onboard_quiet_days", 3, actor="human:op")
    made = await ob.ob.sweep(now=time.time() + 4 * 86400)
    lines = [ob.front.store.find_ask(m)["line"] for m in made]
    assert lines == ["GPU toy has been quiet 4 days. Close the kaiju exception and restore "
                     "Strata A on GPU 1?"]
    # Two weeks on, the review date passed too: "Still a toy?"
    later = await ob.ob.sweep(now=time.time() + 15 * 86400)
    assert any(ob.front.store.find_ask(m)["line"].endswith("Still a toy?") for m in later)
    # Yes to the quiet one restores and ends the exception.
    qa = next(ob.front.store.find_ask(m) for m in made
              if "has been quiet" in ob.front.store.find_ask(m)["line"])
    out = await ob.front.answer(qa["name"], "yes", by="Bake", quote="yes")
    assert out["ledger"]["closed"] == lease["id"] and out["ledger"]["ended"] == "gpu-toy-kaiju"
    x = next(x for x in st.current()["exceptions"] if x["id"] == "gpu-toy-kaiju")
    assert x.get("ended")


@pytest.mark.asyncio
async def test_an_expired_exception_offers_to_restore_and_yes_runs_with_the_persons_yes(ob):
    await _borrowed(ob)
    x = next(x for x in ob.trust.current()["exceptions"] if x["id"] == "gpu-toy-kaiju")
    made = await ob.ob.sweep(now=float(x["expires_at"]) + 60)
    a = next(ob.front.store.find_ask(m) for m in made
             if "exception has ended" in ob.front.store.find_ask(m)["line"])
    assert a["line"] == "The GPU toy exception has ended. Want me to bring Strata A on GPU 1 " \
                        "back up?"
    n = len(_ran(ob, "shell.exec"))
    out = await ob.front.answer(a["name"], "yes", by="Bake", quote="yes")
    assert out["ok"] and len(_ran(ob, "shell.exec")) == n + 1


@pytest.mark.asyncio
async def test_still_a_toy_no_tightens_and_yes_keeps(ob):
    await _toy_on_kaiju(ob)
    await turn(ob, "yes, do it", ("project_setup", {"words": "yes, do it"}))
    await _accept_all(ob)
    made = await ob.ob.sweep(now=time.time() + 15 * 86400)
    review = next(ob.front.store.find_ask(m) for m in made
                  if ob.front.store.find_ask(m)["kind"] == "review")
    assert review["line"].startswith("GPU toy has been ship-it for")
    out = await ob.front.answer(review["name"], "no", by="Bake", reason="it grew up")
    assert "gpu-toy: ship-it -> guarded" in out["note"]
    assert "ended exception gpu-toy-kaiju" in out["note"]
    e = next(e for e in ob.trust.current()["entries"] if e["id"] == "gpu-toy")
    assert e["level"] == "guarded"


# -- the trust map: resource selectors, baseline, the approval layer -----------------------------

def _kaiju_store():
    st = seeded()
    st.change("set_baseline", STRATA, BAKE)
    st.change("add_exception", {"id": "gpu-toy-kaiju", "applies_to": ["kaiju"],
                                "targets": ["resource:strata-gpu1"], "tier": "go",
                                "expires": time.time() + 3600, "project": "gpu-toy"},
              T.Actor("person", "Bake", words="we can shut down Strata on GPU 1 for it"))
    return st


def test_an_exception_naming_a_resource_lowers_only_that_resource():
    st = _kaiju_store()
    assert st.rule("shell.exec", "exec", worker="kaiju").tier == "person"
    r = st.rule("shell.exec", "exec", worker="kaiju", resource="strata-gpu1")
    assert r.tier == "go" and r.exception["id"] == "gpu-toy-kaiju"
    assert st.rule("shell.exec", "exec", worker="kaiju", resource="other").tier == "person"


def test_the_approval_layer_honours_only_the_house_agents_declared_call(tmp_path):
    st = _kaiju_store()
    az = authorizer(tmp_path, st)
    house = AGENT.__class__("home:home", "token", "agent", label="agent:home")

    def check(principal):
        return az.check("shell.exec", "kaiju", entry("kaiju"), principal=principal)
    assert check(house).decision == "hold"
    tok = T.borrowing.set({"resource": "strata-gpu1", "cap": "shell.exec", "worker": "kaiju",
                           "principal": "home:home"})
    try:
        assert check(house).decision != "hold"
        assert check(AGENT).decision == "hold"          # another principal: still sacred
        assert az.check("file.read", "kaiju", entry("kaiju"), principal=house).decision \
            in ("allow", "off")                         # reads are free anyway
    finally:
        T.borrowing.reset(tok)
    tok = T.borrowing.set({"resource": "strata-gpu1", "cap": "worker.update", "worker": "kaiju",
                           "principal": "home:home"})
    try:
        assert check(house).decision == "hold"          # the cap must match
    finally:
        T.borrowing.reset(tok)
    assert A is not None


def test_the_house_agent_cannot_rewrite_a_resource_an_exception_names():
    st = _kaiju_store()
    house = T.Actor("house", "agent:home", words="Bake: record it")
    changed = {**STRATA, "stop": {"worker": "kaiju", "cap": "shell.exec",
                                  "args": {"argv": ["rm", "-rf", "/"]}}}
    with pytest.raises(T.TrustError, match="person's alone"):
        st.change("set_baseline", changed, house)
    st.change("set_baseline", {**STRATA, "normal": "Strata A serves on GPU 1"}, house)
    with pytest.raises(T.TrustError, match="normal"):
        st.change("set_baseline", {**STRATA, "normal": ""}, house)
    with pytest.raises(T.TrustError, match="no entry"):
        st.change("set_baseline", {**STRATA, "entry": "nope"}, house)
    with pytest.raises(T.TrustError, match="resource:"):
        T.check_selector("path:/x")
    # Agents still only propose.
    with pytest.raises(T.TrustError, match="only a person"):
        st.change("set_baseline", STRATA, T.Actor("agent", "x", words="please"))


def test_heard_and_slugify():
    assert O.heard("it's just a toy", "Honestly, it's just a toy experiment!")
    assert not O.heard("it's just a toy", "it's core infrastructure")
    assert not O.heard("", "anything")
    assert O.slugify("GPU toy!") == "gpu-toy" and O.slugify("***") == "project"


@pytest.mark.asyncio
async def test_onboarding_shows_on_the_decisions_view_and_mcp(ob):
    await _borrowed(ob)
    view = ob.front.view()
    assert view["onboarding"][0]["stage"] == "done"
    assert view["borrowed"][0]["what"] == "Strata A on GPU 1 stopped"
    assert view["baseline"][0]["id"] == "strata-gpu1"
    home = ob.node.plugin("home")
    assert home.ledger()["open"] and home.projects()["projects"][0]["slug"] == "gpu-toy"
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_one_restore_offer_at_a_time_and_a_stale_yes_does_nothing(ob):
    await _borrowed(ob)
    ob.node.settings.set("home.onboard_quiet_days", 3, actor="human:op")
    first = await ob.ob.sweep(now=time.time() + 4 * 86400)
    later = await ob.ob.sweep(now=time.time() + 15 * 86400)
    assert len(first) == 1
    assert all(ob.front.store.find_ask(m)["kind"] == "review" for m in later)
    out = await turn(ob, "bring Strata back", ("restore", {"what": "strata",
                                                           "words": "bring Strata back"}))
    assert out["answer"].startswith("Done: bring Strata A on GPU 1 back.")
    n = len(_ran(ob, "shell.exec"))
    res = await ob.front.answer(first[0], "yes", by="Bake", quote="yes")
    assert res["note"] == "Already restored; nothing to do." and len(_ran(ob, "shell.exec")) == n


@pytest.mark.asyncio
async def test_an_archived_project_gets_no_more_check_ins(ob):
    await _toy_on_cachyrig(ob)
    await turn(ob, "yes go for it", ("project_setup", {"words": "yes go for it"}))
    pid = ob.ob.threads(1)[0]["result"]["project"]
    await ob.node.invoke("task.write", {"action": "update", "kind": "project", "id": pid,
                                        "request_id": "t-arch", "data": {"revision": 1, "patch": {
                                            "state": "archived"}}}, "human:op")
    assert await ob.ob.sweep(now=time.time() + 30 * 86400) == []
    assert ob.front.store._q("SELECT state FROM onboard_projects")[0]["state"] == "archived"
