"""The conference table and votes (docs/design/huddles.md 10,
rook/hub/plugins/huddle/table.py): artifacts pinned at an exact version that
every role reads through the hub; votes the chair calls, decided by the trust
map, gated by facts, cast blind by the required roles, bound to the version,
blocked by any no (couldn't verify counts as no; authors never vote), one
resolution round for the chair, then a one-line ask to the person; a passed
vote is carried out by the hub with its own credential.

Model turns run on test_huddle_chair's fake agent host, which calls the hub's
real per-turn tool server; GitHub is a fake behind the table's http hook."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from rook.hub import trust as T
from rook.hub.plugins.huddle import table as TB
from rook.hub.plugins.huddle.service import PERSON
from tests.test_huddle_chair import _call, _settle, _texts, hub  # noqa: F401
from tests.test_trust import seeded

REPO = "bake-ware/rook"
BASE_SHA = "b" * 40
SHA1 = "1" * 40
SHA2 = "2" * 40
TOKEN = "ghp_" + "T" * 36
DIFF_DOCS = ("diff --git a/docs/x.md b/docs/x.md\n--- a/docs/x.md\n+++ b/docs/x.md\n"
             "@@ -1 +1,2 @@\n line\n+a docs line\n")


class FakeGitHub:
    """Enough of GitHub's REST API for the table: one repo's PRs, commits,
    checks, diffs, files and merges."""

    def __init__(self):
        self.prs = {7: {"number": 7, "title": "Docs: a line", "body": "Adds a line.",
                        "state": "open", "merged": False, "draft": False, "mergeable": True,
                        "mergeable_state": "clean", "head": SHA1, "head_ref": "docs-line",
                        "base": "master", "base_sha": BASE_SHA, "author": "Bake-Ware"}}
        self.checks = {SHA1: [("pytest (Python 3.12)", "success"), ("generated docs", "success")],
                       SHA2: [("pytest (Python 3.12)", "success")]}
        self.diffs = {SHA1: DIFF_DOCS, SHA2: DIFF_DOCS + "+another line\n"}
        self.files = {(SHA1, "docs/x.md"): "line\na docs line\n"}
        self.calls = []

    def http(self, method, url, headers, body=None, raw=False):
        path = url.split("api.github.com", 1)[1]
        self.calls.append((method, path, headers.get("Authorization"), body))
        parts = path.split("?")[0].strip("/").split("/")
        repo = f"{parts[1]}/{parts[2]}".lower()
        assert repo == REPO, repo
        rest = parts[3:]
        if rest[0] == "pulls" and len(rest) == 2:
            p = self.prs[int(rest[1])]
            return {"number": p["number"], "title": p["title"], "body": p["body"],
                    "state": p["state"], "merged": p["merged"], "draft": p["draft"],
                    "mergeable": p["mergeable"], "mergeable_state": p["mergeable_state"],
                    "head": {"sha": p["head"], "ref": p["head_ref"]},
                    "base": {"ref": p["base"], "sha": p["base_sha"]},
                    "user": {"login": p["author"]},
                    "html_url": f"https://github.com/Bake-Ware/rook/pull/{p['number']}"}
        if rest[0] == "pulls" and rest[2] == "merge":
            p = self.prs[int(rest[1])]
            if body["sha"] != p["head"]:
                raise TB.GitHubError(409, "Head branch was modified")
            p.update(state="closed", merged=True)
            return {"merged": True, "sha": "m" * 40, "message": "Pull Request successfully merged"}
        if rest[0] == "compare":
            base, head = rest[1].split("...")
            if "diff" in headers["Accept"]:
                return self.diffs[head].encode()
            return {"files": [{"filename": "docs/x.md", "status": "modified", "additions": 1,
                               "deletions": 0}]}
        if rest[0] == "commits" and len(rest) == 3 and rest[2] == "check-runs":
            return {"check_runs": [{"name": n, "status": "completed" if c else "in_progress",
                                    "conclusion": c} for n, c in self.checks.get(rest[1], [])]}
        if rest[0] == "commits" and len(rest) == 3 and rest[2] == "status":
            return {"state": "success" if self.checks.get(rest[1]) else "pending", "statuses": []}
        if rest[0] == "commits":
            return {"sha": rest[1] if len(rest[1]) == 40 else SHA1, "commit": {"message": "m"},
                    "parents": [{"sha": BASE_SHA}], "files": []}
        if rest[0] == "contents":
            ref = path.split("ref=")[1]
            return self.files[(ref, "/".join(rest[1:]))].encode()
        if rest[0] == "issues" and rest[2] == "comments":
            if method == "POST":
                return {"id": 1}
            return [{"user": {"login": "skeptic-bot"}, "body": "Looks fine.",
                     "created_at": "2026-10-23T09:00:00Z"}]
        if rest[0] == "issues" and rest[2] == "labels":
            return [{"name": n} for n in body["labels"]]
        if rest[0] == "pulls" and rest[2] in ("reviews", "comments"):
            return []
        raise AssertionError(f"unexpected GitHub call {method} {path}")


ROLES = {"lead": "Lead developer", "qa": "QA engineer", "security": "Security reviewer",
         "skeptic": "Skeptic / red team", "architect": "Architect", "release": "Release manager",
         "product": "Product owner"}


@pytest.fixture
def table(hub):  # noqa: F811
    hub.node.settings.set("home.front_door", True, actor="human:op")
    hub.node.plugin("trust")._store = seeded()
    hub.node._vault.data["hub-github"] = TOKEN
    hub.node.settings.set("huddle.vote_github_secret", "hub-github", actor="human:op")
    hub.node.settings.set("huddle.vote_gate_wait_minutes", 0, actor="human:op")
    gh = FakeGitHub()
    t = hub.svc.table
    t.github = TB.GitHub(t._token, gh.http)
    hub.gh, hub.t = gh, t
    return hub


async def _team(hub, *ids):
    for rid in ids or ("lead", "qa", "security", "skeptic"):
        await _call(hub, "role.set", {"role": {"id": rid, "name": ROLES[rid],
                                               "charter": f"The {ROLES[rid]}.",
                                               "tools": {"allow": ["info.*"]}}})


async def _huddle(hub, roles=("lead", "qa", "security", "skeptic")):
    await _team(hub, *roles)
    return (await _call(hub, "huddle.start", {"title": "Ship docs", "roles": list(roles)}))["room"]


def _yes(read="diff", evidence="diff: only docs/x.md changed", **extra):
    tools = [("table_read", {"artifact": "A1", "what": read})] if read else []
    return {"tools": tools + [("vote", {"vote": "yes", "reasons": "Docs only, CI green.",
                                        "evidence": [evidence] if evidence else [], **extra})],
            "text": "Voted yes."}


def _no(reasons="It drops a test.", **extra):
    return {"tools": [("table_read", {"artifact": "A1", "what": "diff"}),
                      ("vote", {"vote": "no", "reasons": reasons, **extra})], "text": "No."}


def _script(hub, **turns):
    for kind, steps in turns.items():
        hub.band.script[kind.replace("_", "-")] = [dict(s) for s in
                                                   (steps if isinstance(steps, list) else [steps])]


def _all_yes(hub, **override):
    turns = {"vote_lead": _yes(), "vote_qa": _yes(read="checks",
                                                  evidence="checks: pytest 3.12 success"),
             "vote_security": _yes(), "vote_skeptic": _yes(risk="None: docs only, no code.")}
    turns.update(override)
    _script(hub, **turns)


async def _pin_and_vote(hub, room, kind="merge", **data):
    pinned = await hub.t.pin(room, "agent:home", {"kind": "pr", "repo": "Bake-Ware/rook",
                                                  "number": 7})
    out = await hub.t.call(room, "agent:home", {"kind": kind, "artifact": pinned["artifact"],
                                                "reason": "The docs PR is done.", **data})
    await _settle(hub)
    return pinned, hub.t.motion(out["motion"])


def _merges(hub):
    return [c for c in hub.gh.calls if c[0] == "PUT"]


# -- the table -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_roles_read_the_pinned_version_through_the_hub_not_the_moving_head(table):
    room = await _huddle(table)
    a = await table.t.pin(room, "agent:home", {"kind": "pr", "repo": "Bake-Ware/rook",
                                               "number": 7})
    assert a["version"] == SHA1 and a["n"] == 1
    assert "Pinned A1 [pr] bake-ware/rook#7: Docs: a line @ 1111111" in _texts(table, room)[-1][1]
    table.gh.prs[7]["head"] = SHA2                       # someone pushes after the pin
    d = await table.t.read(room, "A1", "diff")
    assert d["version"] == SHA1 and d["text"] == DIFF_DOCS     # the pinned commit, not SHA2
    s = await table.t.read(room, "A1", "summary")
    assert s["current_version"] == SHA2 and "moved on to 2222222" in s["stale"]
    f = await table.t.read(room, a["artifact"], "file", path="docs/x.md")
    assert f["text"] == "line\na docs line\n"
    c = await table.t.read(room, "A1", "checks")
    assert c["checks"]["runs"][0]["conclusion"] == "success"
    # A role in an ordinary round gets table_read too, with the table in its guide.
    _script(table, huddle_chair={"tools": [("call_on", {"calls": [{"role": "qa",
                                                                    "ask": "Read A1"}]})]},
            huddle_qa={"tools": [("table_read", {"artifact": "A1", "what": "checks"})],
                       "text": "CI green at 1111111."})
    await _call(table, "huddle.post", {"huddle": room, "text": "What's on the table?"})
    await _settle(table)
    chair = next(t for t in table.band.turns if t["kind"] == "huddle-chair")
    assert chair["listed"][-2:] == ["pin", "call_vote"]
    qa = next(t for t in table.band.turns if t["kind"] == "huddle-qa")
    assert "table_read" in qa["listed"] and "A1 (" in qa["args"]["system"]
    assert qa["seen"][0]["ok"] and qa["seen"][0]["version"] == SHA1
    # A document is pinned to its hash; the read checks it.
    doc = await table.t.pin(room, PERSON, {"kind": "doc", "text": "Rollout plan v1",
                                           "title": "Plan"})
    assert doc["version"] == TB.sha256("Rollout plan v1")
    assert (await table.t.read(room, "A2", "content"))["text"] == "Rollout plan v1"


@pytest.mark.asyncio
async def test_a_unanimous_vote_is_merged_by_the_hub_at_the_voted_sha(table):
    room = await _huddle(table)
    _all_yes(table)
    pinned, m = await _pin_and_vote(table, room)
    assert m["state"] == "executed", m
    assert [g["id"] for g in m["gates"]] == ["version", "mergeable", "ci", "tests", "secrets"]
    assert all(g["ok"] for g in m["gates"])
    assert m["decision"]["entry"] == "rook-master" and m["decision"]["level"] == "guarded"
    assert m["decision"]["mode"] == "auto"
    put, = _merges(table)
    assert put[1] == "/repos/bake-ware/rook/pulls/7/merge" and put[3]["sha"] == SHA1
    assert put[2] == f"Bearer {TOKEN}"                        # the hub's own credential
    assert f"vote {m['name']}" in put[3]["commit_title"]
    texts = [t for _, t in _texts(table, room)]
    assert any(t.startswith("Done: Merged bake-ware/rook#7 at 1111111 (after a unanimous vote (")
               for t in texts)
    ex = [r for r in table.node.journal.rows if r["cap"] == "huddle.vote.execute"]
    assert ex and ex[-1]["audit"]["version"] == SHA1 and len(ex[-1]["audit"]["ballots"]) == 4
    assert [g["id"] for g in ex[-1]["audit"]["gates"]][-1] == "secrets"
    digest = table.node.plugin("home").front.digest()
    assert "Merged bake-ware/rook#7 at 1111111" in digest["decisions"][0]["summary"]
    # Every voter read the pinned version; their ballots carry it.
    for b in table.t.ballots(m["id"]):
        assert b["vote"] == "yes" and b["version"] == SHA1 and b["reads"][0]["version"] == SHA1


@pytest.mark.asyncio
async def test_ballots_are_sealed_until_every_required_voter_has_voted(table):
    room = await _huddle(table)
    slow = dict(_yes(risk="None: docs only."), delay=0.3)
    _all_yes(table, vote_skeptic=slow)
    a = await table.t.pin(room, "agent:home", {"kind": "pr", "repo": "Bake-Ware/rook",
                                               "number": 7})
    out = await table.t.call(room, "agent:home", {"kind": "merge", "artifact": a["artifact"]})
    for _ in range(100):                                    # three of four are in
        if len(table.t.ballots(out["motion"])) == 3:
            break
        await asyncio.sleep(0.02)
    view = await _call(table, "huddle.table", {"huddle": room})
    seen = view["motions"][0]["ballots"]
    assert len(seen) == 3 and all(b["sealed"] and "vote" not in b and "reasons" not in b
                                  for b in seen)
    msgs = [t for _, t in _texts(table, room)]
    assert sum("has voted on" in t and "(sealed " in t for t in msgs) == 3
    assert not any("YES" in t for t in msgs)                 # nothing revealed yet
    # The slow voter's turn is asked only about the motion: no transcript, no ballots.
    await _settle(table)
    sk = next(t for t in table.band.turns if t["kind"] == "vote-skeptic")
    assert "Docs only, CI green" not in sk["args"]["prompt"]
    assert sk["listed"][-2:] == ["table_read", "vote"] and "propose" not in sk["listed"]
    view = await _call(table, "huddle.table", {"huddle": room})
    assert all(not b.get("sealed") and b["vote"] == "yes" for b in view["motions"][0]["ballots"])
    # The commitment posted when it was cast matches the revealed ballot.
    b = view["motions"][0]["ballots"][0]
    body = json.dumps({"voter": b["voter"], "vote": b["vote"], "reasons": b["reasons"],
                       "evidence": b["evidence"], "risk": b["risk"], "version": b["version"]},
                      sort_keys=True)
    assert TB.sha256(b["salt"] + body)[:16] == b["commitment"]


@pytest.mark.asyncio
async def test_a_new_commit_voids_the_vote_and_its_ballots(table):
    room = await _huddle(table)

    async def push():
        await asyncio.sleep(0.05)
        table.gh.prs[7]["head"] = SHA2                     # lands while the skeptic thinks
    _all_yes(table, vote_skeptic=dict(_yes(risk="None."), delay=0.3))
    a = await table.t.pin(room, "agent:home", {"kind": "pr", "repo": "Bake-Ware/rook",
                                               "number": 7})
    out = await table.t.call(room, "agent:home", {"kind": "merge", "artifact": a["artifact"]})
    await asyncio.gather(push(), _settle(table))
    m = table.t.motion(out["motion"])
    assert m["state"] == "void" and "2222222" in m["result"]["void"]
    assert not _merges(table)
    # Pinning the new version supersedes the old pin; a vote on it starts clean.
    table.band.script.clear()
    _all_yes(table)
    b = await table.t.pin(room, "agent:home", {"kind": "pr", "repo": "Bake-Ware/rook",
                                               "number": 7})
    assert b["version"] == SHA2 and table.t.artifact(a["artifact"])["state"] == "superseded"
    with pytest.raises(TB.TableError, match="superseded"):
        await table.t.call(room, "agent:home", {"kind": "merge", "artifact": a["artifact"]})
    # And a pin of a newer head voids a vote still open on the old one.
    table.band.script["vote-skeptic"] = [dict(_yes(risk="None."), delay=0.3)]
    out = await table.t.call(room, "agent:home", {"kind": "merge", "artifact": b["artifact"]})
    for _ in range(100):
        if table.t.motion(out["motion"])["state"] == "voting":
            break
        await asyncio.sleep(0.01)
    table.gh.prs[7]["head"] = SHA1
    table.gh.diffs[SHA1] = DIFF_DOCS
    await table.t.pin(room, "agent:home", {"kind": "pr", "repo": "Bake-Ware/rook", "number": 7})
    await _settle(table)
    assert table.t.motion(out["motion"])["state"] == "void"
    assert not _merges(table)


@pytest.mark.asyncio
async def test_a_veto_gets_one_resolution_round_then_a_one_line_ask_to_the_person(table):
    room = await _huddle(table)
    _all_yes(table, vote_skeptic=[_no("It deletes the only test of the merge path.",
                                      risk="The merge path loses its test."),
                                  _no("Still deletes the test.", risk="Same risk.")])
    _script(table, vote_chair={"tools": [("revote", {"note": "Skeptic: check the diff again; "
                                                             "it is docs only."})]})
    # The re-vote: everyone votes again, blind; the skeptic still says no.
    for r in ("lead", "qa", "security"):
        table.band.script[f"vote-{r}"].append(_yes())
    pinned, m = await _pin_and_vote(table, room)
    assert m["state"] == "asked" and m["round"] == 2
    assert m["result"]["resolution"]["action"] == "revote"
    front = table.node.plugin("home").front
    ask = front.store.ask(m["ask"])
    assert ask["kind"] == "vote" and len(ask["line"]) <= 160
    assert ask["line"].startswith("Merge bake-ware/rook#7") and "skeptic said no" in ask["line"]
    assert ask["explain"].startswith("For: lead: Docs only") and "Against: skeptic:" in ask["explain"]
    assert "Same risk" in ask["explain"]
    # It is in the relayed approvals list (the phone's card, rook_approvals).
    relay = table.node.plugin("approvals").relay
    item = next(i for i in relay.items() if i["kind"] == "house" and i["id"] == ask["id"])
    assert item["options"] == ["Yes", "No", "Explain"]
    assert not _merges(table)
    # Yes: the hub merges at the voted SHA, citing the person's yes.
    out = await front.answer(ask["id"], "yes", by="Bake", quote="Yes, merge it")
    assert out["note"].startswith("Done: Merged bake-ware/rook#7"), out
    assert _merges(table)[0][3]["sha"] == SHA1
    assert table.t.motion(m["id"])["result"]["by"] == "Bake"


@pytest.mark.asyncio
async def test_no_from_the_person_drops_it_and_a_chair_that_cannot_settle_escalates(table):
    room = await _huddle(table)
    _all_yes(table, vote_security=_no("A token is logged.", evidence=["diff line 3"]))
    _script(table, vote_chair={"fail": "model down"})         # the chair cannot decide
    _, m = await _pin_and_vote(table, room)
    assert m["state"] == "asked" and m["round"] == 1
    front = table.node.plugin("home").front
    await front.answer(m["ask"], "no", by="Bake", quote="No")
    m = table.t.motion(m["id"])
    assert m["state"] == "rejected" and not _merges(table)
    assert "Bake said no. Nothing was done." in _texts(table, room)[-2][1] + _texts(table, room)[-1][1]


@pytest.mark.asyncio
async def test_couldnt_verify_counts_as_no_and_a_yes_must_show_its_work(table):
    room = await _huddle(table)
    _all_yes(table,
             vote_lead={"text": "I ran out of time."},                       # never voted
             vote_qa=_yes(read=None),                                         # no read
             vote_security=_yes(evidence=None),                               # no evidence
             vote_skeptic=_yes())                                             # critic, no risk
    _script(table, vote_chair={"tools": [("escalate", {"note": "Nobody verified it."})]})
    _, m = await _pin_and_vote(table, room)
    got = {b["voter"]: b for b in table.t.ballots(m["id"])}
    assert {v: b["vote"] for v, b in got.items()} == {
        "lead": "cant_verify", "qa": "cant_verify", "security": "cant_verify",
        "skeptic": "cant_verify"}
    assert "did not vote" in got["lead"]["reasons"]
    assert "no read of the pinned version" in got["qa"]["note"]
    assert "needs evidence" in got["security"]["note"]
    assert "names a specific risk" in got["skeptic"]["note"]
    assert m["state"] == "asked" and not _merges(table)
    msgs = "\n".join(t for _, t in _texts(table, room))
    assert "COULDN'T VERIFY (counts as no)" in msgs


@pytest.mark.asyncio
async def test_authors_never_vote_on_their_own_work_and_a_stand_in_takes_the_seat(table):
    room = await _huddle(table, ("lead", "qa", "security", "skeptic", "architect"))
    roles = table.node.plugin("role")
    roles.sessions.add({"id": "s_1", "role": "lead", "room": room, "worker": "hosta",
                        "branch": "docs-line", "state": "done",
                        "pr_url": "https://github.com/Bake-Ware/rook/pull/7"})
    _all_yes(table, vote_architect=_yes())
    pinned, m = await _pin_and_vote(table, room)
    assert {"role": "lead", "why": "its session s_1 wrote it"} in m["excluded"]
    seat = m["seats"][0]
    assert seat["role"] == "architect" and seat["note"] == "stands in for lead"
    assert "vote-lead" not in [t["kind"] for t in table.band.turns]
    assert m["state"] == "executed"
    # No stand-in: the seat is empty, which counts as couldn't verify.
    table.node.settings.set("huddle.vote_alternates", {}, actor="human:op")
    table.gh.prs[7].update(state="open", merged=False)
    table.band.script.clear()
    _all_yes(table)
    _script(table, vote_chair={"tools": [("withdraw", {"reason": "Needs a reviewer."})]})
    a = await table.t.pin(room, "agent:home", {"kind": "pr", "repo": "Bake-Ware/rook",
                                               "number": 7})
    out = await table.t.call(room, "agent:home", {"kind": "merge", "artifact": a["artifact"]})
    await _settle(table)
    m = table.t.motion(out["motion"])
    assert m["seats"][0]["role"] is None and "lead wrote the work" in m["seats"][0]["note"]
    assert m["state"] == "withdrawn"
    assert len(_merges(table)) == 1


@pytest.mark.asyncio
async def test_gates_are_facts_not_votes(table):
    room = await _huddle(table)
    table.gh.checks[SHA1] = [("pytest (Python 3.12)", "failure"), ("docs", "success")]
    _, m = await _pin_and_vote(table, room)
    assert m["state"] == "gates_failed"
    ci = next(g for g in m["gates"] if g["id"] == "ci")
    assert not ci["ok"] and "pytest (Python 3.12): failure" in ci["detail"]
    assert not [t for t in table.band.turns if t["kind"].startswith("vote-")]   # nobody voted
    # No test run among the checks.
    table.gh.checks[SHA1] = [("lint", "success")]
    _, m = await _pin_and_vote(table, room)
    assert m["state"] == "gates_failed"
    assert "no test run" in next(g for g in m["gates"] if g["id"] == "tests")["detail"]
    # A secret in the diff.
    table.gh.checks[SHA1] = [("pytest", "success")]
    table.gh.diffs[SHA1] = DIFF_DOCS + "+token = " + "ghp_" + "x" * 36 + "\n"
    _, m = await _pin_and_vote(table, room)
    sec = next(g for g in m["gates"] if g["id"] == "secrets")
    assert not sec["ok"] and "looks like a credential" in sec["detail"] and "xxxx" not in sec["detail"]
    # Conflicts.
    table.gh.diffs[SHA1] = DIFF_DOCS
    table.gh.prs[7]["mergeable"] = False
    _, m = await _pin_and_vote(table, room)
    assert m["state"] == "gates_failed" and "conflicts" in m["gates"][1]["detail"]
    assert not _merges(table)


@pytest.mark.asyncio
async def test_the_trust_map_decides_whether_a_vote_is_needed_and_what_a_pass_does(table):
    room = await _huddle(table)
    st = table.node.plugin("trust")._store
    # ship-it: the chair may skip the vote; gates still run; the hub merges.
    st.change("set_entry", {"id": "rook-docs", "kind": "repo", "level": "ship-it",
                            "targets": ["repo:Bake-Ware/rook#docs-*"]},
              T.Actor("person", "Bake", words="docs branches just go"))
    table.gh.prs[7]["base"] = "docs-site"
    _, m = await _pin_and_vote(table, room, skip_vote=True)
    assert m["decision"]["level"] == "ship-it" and m["state"] == "executed" and not m["seats"]
    assert len(_merges(table)) == 1
    # guarded: a vote is required.
    table.gh.prs[7].update(base="master", state="open", merged=False)
    with pytest.raises(TB.TableError, match="a vote is required"):
        await _pin_and_vote(table, room, skip_vote=True)
    # sacred: a passed vote still asks the person.
    st.change("set_entry", {"id": "rook-release", "kind": "repo", "level": "sacred",
                            "targets": ["repo:Bake-Ware/rook#release"]},
              T.Actor("person", "Bake", words="release is mine"))
    table.gh.prs[7]["base"] = "release"
    table.band.script.clear()
    _all_yes(table)
    _, m = await _pin_and_vote(table, room)
    assert m["decision"]["mode"] == "person" and m["state"] == "asked"
    assert len(_merges(table)) == 1
    ask = table.node.plugin("home").front.store.ask(m["ask"])
    assert "The vote passed; sacred" in ask["line"]


@pytest.mark.asyncio
async def test_a_deploy_vote_runs_the_rollback_dry_run_and_still_asks_the_person(table):
    room = await _huddle(table, ("release", "qa", "security", "skeptic"))
    table.band.workers["w-box"]["caps"].append("shell.exec")
    table.node.settings.set("huddle.vote_deploy_targets", [{
        "id": "test-hub", "title": "the test hub",
        "dry_run": {"worker": "box", "cap": "shell.exec",
                    "args": {"argv": ["rollback.sh", "--dry-run"]}},
        "deploy": {"worker": "box", "cap": "shell.exec", "args": {"argv": ["deploy.sh", "{sha}"]}},
    }], actor="human:op")
    _all_yes(table, vote_release=_yes())
    table.gh.prs[7].update(state="closed", merged=True)
    _, m = await _pin_and_vote(table, room, kind="deploy", target="test-hub")
    assert [g["id"] for g in m["gates"]][-1] == "rollback" and all(g["ok"] for g in m["gates"])
    dry = [c for c in table.band.calls if c.cap == "shell.exec"]
    assert dry[0].args == {"argv": ["rollback.sh", "--dry-run"]}
    assert dry[0].identity == "agent:home"                    # as the house agent
    assert m["decision"]["mode"] == "ask" and m["state"] == "asked"   # the trust ramp
    front = table.node.plugin("home").front
    out = await front.answer(m["ask"], "yes", by="Bake", quote="go")
    assert out["note"].startswith("Done: Deployed 1111111 to the test hub"), out
    run = [c for c in table.band.calls if c.cap == "shell.exec"][-1]
    assert run.args == {"argv": ["deploy.sh", SHA1]}


@pytest.mark.asyncio
async def test_only_people_and_the_chair_call_votes_and_roles_never_pin_or_vote(table):
    room = await _huddle(table)
    a = await table.t.pin(room, PERSON, {"kind": "pr", "repo": "Bake-Ware/rook", "number": 7})
    res = await table.node.dispatch("huddle.vote", {"huddle": room, "kind": "merge",
                                                    "artifact": a["artifact"]},
                                    "agent:claude_laptop")
    assert not res["ok"] and "only a person or the house agent" in res["error"]
    roles = table.node.plugin("role")
    role = roles.role("qa")
    assert roles.scope_refusal(role, "huddle.vote", None) == "roles never call huddle.vote"
    assert roles.scope_refusal(role, "huddle.pin", None) == "roles never call huddle.pin"
    with pytest.raises(TB.TableError, match="kind: one of"):
        await table.t.call(room, "agent:home", {"kind": "yolo", "artifact": a["artifact"]})
    # MCP: rook_huddle table / pin / vote.
    tool, = table.node.plugin("huddle").mcp_tools(
        lambda cap, args: table.node.invoke(cap, args, PERSON))
    out = json.loads(await tool(action="table", data={"room": room}))
    assert out["ok"] and out["result"]["artifacts"][0]["version"] == SHA1
    out = json.loads(await tool(action="table", data={"room": room, "artifact": "A1",
                                                      "what": "diff"}))
    assert out["result"]["text"] == DIFF_DOCS


@pytest.mark.asyncio
async def test_a_vote_cut_off_by_a_restart_ends_failed_and_the_dashboard_can_call_one(table):
    room = await _huddle(table)
    a = await table.t.pin(room, PERSON, {"kind": "pr", "repo": "Bake-Ware/rook", "number": 7})
    mid = "v_restart01"
    table.t._x("INSERT INTO huddle_motions(id, room, kind, artifact, version, proposer, state, "
               "created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
               (mid, room, "merge", a["artifact"], SHA1, PERSON, "voting", time.time(),
                time.time()))
    table.t.resume()
    m = table.t.motion(mid)
    assert m["state"] == "failed" and "restarted" in m["result"]["error"]
    # The dashboard (the person) pins and calls a vote through the huddle API.
    import httpx
    from starlette.applications import Starlette
    from rook.hub.plugins.huddle.web import routes

    class Accounts:
        def session(self, token):
            return {"id": "1", "name": "Operator", "admin": True, "csrf": "tok"} \
                if token == "s" else None
    app = Starlette(routes=routes(lambda: table.node, Accounts()))
    _all_yes(table)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 cookies={"rook_account": "s"}) as c:
        r = await c.post("/huddle/account-api", json={
            "csrf": "tok", "action": "pin", "room": room,
            "data": {"kind": "pr", "repo": "Bake-Ware/rook", "number": 7}})
        assert r.status_code == 200 and r.json()["result"]["version"] == SHA1
        r = await c.post("/huddle/account-api", json={
            "csrf": "tok", "action": "vote", "room": room,
            "data": {"kind": "merge", "artifact": "A1", "reason": "Bake asked"}})
        assert r.status_code == 200, r.json()
        await _settle(table)
        r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "view",
                                                      "room": room})
        tb = r.json()["result"]["table"]
        assert tb["motions"][-1]["state"] == "executed" and tb["motions"][-1]["proposer"] == PERSON
