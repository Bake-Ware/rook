"""Landing work on 0.5 (gate R14, castling-workbench: sign-off replaces merge
votes): github.merge merges a pull request for a task only when the task's
sign-off is satisfied for its project's trust level and the gates pass, at
the head the caller names, with the hub's token from the vault. Real hub,
fake GitHub, hard timeouts."""

import sys

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from hub_rig import BASE, HEAD, REPO, TOKEN, FakeGitHub, config, on_rig  # noqa: E402
from port_helpers import PLUGINS  # noqa: E402
from rook.kit.ctx import load_run  # noqa: E402
from rook.kit.manifest import check  # noqa: E402

mod = sys.modules[load_run(PLUGINS / "github", "scripts/github.py:merge").__module__]
LOGINS = {"claude-coder": "agent:coder", "claude-qa": "agent:qa", "Bake-Ware": "person:bake"}


@pytest.fixture
def gh():
    fake = FakeGitHub()
    fake.start()
    yield fake
    fake.stop()


def merge(rig, task, door="coder", sha=HEAD, number=7):
    return getattr(rig, door).call("github.merge", repo=REPO, number=number, sha=sha, task=task)


async def task_in(rig, level=None, concepts=None):
    pid = None
    if level or concepts:
        p = await rig.bake.ok("project.create", title=f"P {level}", slug=f"p-{level or 'c'}",
                              **({"concepts": concepts} if concepts else {}))
        pid = p["id"]
        if level:
            await rig.bake.ok("project.trust.create", project=pid, level=level)
    t = await rig.coder.ok("task.create", title="Land the thing", **({"project": pid} if pid else {}))
    return t["id"]


# -- without a hub ---------------------------------------------------------------------

def test_manifest_is_clean():
    raw, issues = check(PLUGINS / "github")
    assert raw is not None and not issues, [str(i) for i in issues]


def test_signoff_rules():
    row = lambda who, state="approved": {"reviewer": who, "by": who, "state": state, "note": ""}  # noqa: E731
    rev = lambda login, state="APPROVED", commit=HEAD, who=None: {"login": login, "state": state,  # noqa: E731
                                                                 "commit": commit, "who": who}
    owners = ["person:bake"]

    def problems(level, rows=(), revs=(), pool=None, author="agent:coder", caller="agent:coder"):
        return mod.signoff_problems(level, pool, list(rows), list(revs), HEAD, author, caller, owners)[0]
    assert problems("ship-it") == []
    assert "no sign-off yet" in problems("normal")[0]
    assert problems("normal", [row("agent:qa")]) == []
    assert "no sign-off yet" in problems("normal", [row("agent:coder")])[0]          # not your own work
    assert problems("normal", [row("agent:qa"), row("agent:pm", "requested")]) == ["agent:pm has not signed off yet"]
    assert "declined" in problems("ship-it", [row("agent:qa", "declined")])[0]       # a no blocks even ship-it
    assert "an owner" in problems("sacred", [row("agent:qa")])[0]
    assert problems("sacred", [row("agent:qa"), row("person:bake")]) == []
    assert "concept asks for the user" in problems("normal", [row("agent:qa")], pool="user")[0]
    assert problems("ship-it", pool="none") == []
    # GitHub reviews: the latest per login, approvals only at the head and only by known logins
    assert problems("normal", revs=[rev("claude-qa", who="agent:qa")]) == []
    assert problems("normal", revs=[rev("claude-qa", commit=BASE, who="agent:qa")])
    assert problems("normal", revs=[rev("stranger")])
    assert problems("normal", revs=[rev("claude-qa", who="agent:qa"), rev("claude-qa", "CHANGES_REQUESTED",
                                                                         who="agent:qa")])
    assert problems("normal", revs=[rev("claude-qa", "CHANGES_REQUESTED", who="agent:qa"),
                                    rev("claude-qa", who="agent:qa")]) == []


# -- on the hub ---------------------------------------------------------------------------

def test_a_ship_it_task_lands_with_the_gates_alone(tmp_path, gh):
    async def body(rig):
        tid = await task_in(rig, "ship-it")
        out = await merge(rig, tid)
        assert out["ok"], out
        r = out["result"]
        assert r["merged"] and r["sha"] == "c" * 40 and r["signoff"]["need"] == "none"
        assert r["gates"] == ["version", "mergeable", "ci", "tests", "secrets"]
        assert gh.writes[0][2]["sha"] == HEAD and gh.writes[0][2]["commit_title"].startswith(
            "Merge pull request #7 from c7/work (task ")
        assert all(auth == f"Bearer {TOKEN}" for *_, auth in gh.requests)       # the hub's token, on every call
        entry = rig.calls("github.merge")[0]
        assert entry["agent"] == "agent:coder" and entry["person"] == "person:bake" and entry["decision"] == "allow"
        t = await rig.bake.ok("task.get", id=tid)
        assert any(link["kind"] == "commit" and link["ref"] == "c" * 40 for link in t["links"])
    on_rig(config(tmp_path, gh), body)


def test_a_normal_task_needs_a_reviewer(tmp_path, gh):
    async def body(rig):
        tid = await task_in(rig)                                     # no project: normal
        out = await merge(rig, tid)
        assert not out["ok"] and "no sign-off yet" in out["error"] and not gh.writes
        await rig.coder.ok("task.request_signoff", id=tid, reviewers=["agent:qa", "agent:coder"])
        await rig.coder.ok("task.signoff", id=tid, approve=True)            # the author: doesn't count
        out = await merge(rig, tid)
        assert "agent:qa has not signed off yet" in out["error"] and "no sign-off yet" in out["error"]
        await rig.qa.ok("task.signoff", id=tid, approve=False, note="tests miss the restart")
        assert "agent:qa declined: tests miss the restart" in (await merge(rig, tid))["error"]
        await rig.qa.ok("task.signoff", id=tid, approve=True)
        out = await merge(rig, tid)
        assert out["ok"], out
        assert out["result"]["signoff"]["signers"] == ["agent:qa"]
        assert "signed: agent:qa" in gh.writes[0][2]["commit_title"]
    on_rig(config(tmp_path, gh), body)


def test_sacred_needs_the_owner(tmp_path, gh):
    async def body(rig):
        tid = await task_in(rig, "sacred")
        await rig.coder.ok("task.request_signoff", id=tid, reviewers=["agent:qa", "person:bake"])
        await rig.qa.ok("task.signoff", id=tid, approve=True)
        out = await merge(rig, tid)
        assert "person:bake has not signed off yet" in out["error"] and "must sign off too" in out["error"]
        # nobody signs for the person through an agent
        assert not (await rig.coder.call("task.signoff", id=tid, approve=True, reviewer="person:bake"))["ok"]
        await rig.bake.ok("task.signoff", id=tid, approve=True)
        out = await merge(rig, tid)
        assert out["ok"] and set(out["result"]["signoff"]["signers"]) == {"agent:qa", "person:bake"}
    on_rig(config(tmp_path, gh), body)


def test_a_concept_can_ask_for_the_user(tmp_path, gh):
    async def body(rig):
        await rig.bake.ok("concept.create", title="Hardware", slug="hardware", signoff="user", roles=["qa"])
        tid = await task_in(rig, concepts=["hardware"])               # normal project, user sign-off norm
        await rig.coder.ok("task.request_signoff", id=tid, reviewers=["agent:qa"])
        await rig.qa.ok("task.signoff", id=tid, approve=True)
        assert "a concept asks for the user" in (await merge(rig, tid))["error"]
    on_rig(config(tmp_path, gh), body)


def test_github_reviews_are_sign_off(tmp_path, gh):
    async def body(rig):
        tid = await task_in(rig)
        gh.review(7, "claude-qa", "APPROVED", commit=BASE)            # an older commit: not this head
        gh.review(7, "stranger", "APPROVED")                          # not a known login
        gh.review(7, "claude-coder", "APPROVED")                      # the author
        assert "no sign-off yet" in (await merge(rig, tid))["error"]
        gh.review(7, "claude-qa", "CHANGES_REQUESTED")
        assert "claude-qa requested changes on GitHub" in (await merge(rig, tid))["error"]
        gh.review(7, "claude-qa", "APPROVED")
        out = await merge(rig, tid)
        assert out["ok"] and out["result"]["signoff"]["signers"] == ["agent:qa"], out
        reviews = await rig.amy.ok("github.reviews", repo=REPO, number=7)
        assert {r["login"]: r["who"] for r in reviews}["claude-qa"] == "agent:qa"
    on_rig(config(tmp_path, gh, logins=LOGINS), body)


def test_the_gates(tmp_path, gh):
    async def body(rig):
        tid = await task_in(rig, "ship-it")

        async def refused(*want, sha=HEAD):
            out = await merge(rig, tid, sha=sha)
            assert not out["ok"], out
            for w in want:
                assert w in out["error"], (w, out["error"])
            return out["error"]
        await refused("version: the head is aaaaaaa, not eeeeeee", sha="e" * 40)
        gh.runs[HEAD] = [{"name": "pytest", "status": "in_progress", "conclusion": None}]
        await refused("ci: still running: pytest; call again when CI is done", "tests did not pass: pytest")
        gh.runs[HEAD] = [{"name": "pytest", "status": "completed", "conclusion": "failure"},
                         {"name": "build", "status": "completed", "conclusion": "success"}]
        await refused("CI is not green: pytest: failure", "tests did not pass: pytest")
        gh.runs[HEAD] = [{"name": "build", "status": "completed", "conclusion": "success"}]
        await refused("tests: no test run among the checks")
        gh.runs[HEAD] = []
        await refused("ci: no CI ran on aaaaaaa")
        gh.runs[HEAD] = [{"name": "pytest", "status": "completed", "conclusion": "success"}]
        gh.diffs[(BASE, HEAD)] = f"+++ b/x\n+token = '{TOKEN}'\n"
        err = await refused("secrets: the diff has secrets in it")
        assert TOKEN not in err
        gh.diffs[(BASE, HEAD)] = "+fine\n"
        gh.prs[7]["mergeable"], gh.prs[7]["mergeable_state"] = False, "dirty"
        await refused("mergeable: conflicts with castling (dirty); rebase it")
        gh.prs[7]["mergeable"], gh.prs[7]["draft"] = True, True
        await refused("version: the pull request is a draft")
        gh.prs[7]["draft"] = False
        assert not gh.writes
        # the head moves between the gates and the merge: GitHub refuses
        real = gh.handle

        def moving(h, method):
            if method == "PUT":
                gh.prs[7]["head"]["sha"] = "f" * 40
            return real(h, method)
        gh.handle = moving
        await refused("GitHub 409")
        gh.handle = real
        gh.prs[7]["head"]["sha"] = HEAD
        assert (await merge(rig, tid))["ok"]
        await refused("version: the pull request is merged")
    on_rig(config(tmp_path, gh), body)


def test_no_token_in_the_vault_says_so(tmp_path, gh):
    async def body(rig):
        tid = await task_in(rig, "ship-it")
        out = await merge(rig, tid)
        assert not out["ok"] and "github-hub" in out["error"] and "vault" in out["error"], out
        assert not gh.requests
    on_rig(config(tmp_path, gh, vault=False), body)
