"""Huddles on 0.5 (rook/plugins/huddle + rook/plugins/github, gate R14) on a
real hub in process, driven through the HTTP door, against a fake GitHub (a
local HTTP server speaking the few REST endpoints the github plugin uses).

The 0.4 landing flow, kept: the chair (agent:house) or a person pins a pull
request at its head, the author calls a merge with a self-review, the gates
run (version, mergeable, ci, tests, secrets) and a pass merges through the
github plugin with the hub's token at the pinned SHA. Also: who may pin,
superseding pins, gate failures, a moved head, sacred projects asking an
owner, qa, deploys as change records, the table, the thread, the chair being
told, and a restart.

Every test body runs under a hard timeout; HTTP requests have socket timeouts."""

import asyncio
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import PLUGINS  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from rook.kit.ctx import Caller as KitCaller  # noqa: E402
from rook.kit.ctx import load_run  # noqa: E402
from rook.kit.manifest import check  # noqa: E402

BAND = "4a1e9b0c2d3f4e5a6b7c8d9e0f1a2b3c"
WHY = "Check the huddle plugin."
REPO = "Bake-Ware/rook"
HEAD = "a" * 40
BASE = "b" * 40
TOKEN = "ghp_" + "t" * 36
mod = sys.modules[load_run(PLUGINS / "huddle", "scripts/huddle.py:secret_hits").__module__]
REVIEW = ("Read the full diff: one new plugin and its tests, nothing else touched. Could break: the "
          "merge path if the fake differs from GitHub. Safe: gates run first, merge is pinned to the SHA.")


# -- a fake GitHub -----------------------------------------------------------------------

class FakeGitHub:
    """PRs, commits, checks and diffs in memory; records every request."""

    def __init__(self):
        self.prs = {7: {"number": 7, "title": "Huddles on 0.5", "body": "R14", "state": "open", "merged": False,
                        "draft": False, "mergeable": True, "mergeable_state": "clean",
                        "head": {"sha": HEAD, "ref": "c7/huddles"}, "base": {"sha": BASE, "ref": "castling"},
                        "user": {"login": "claude"}, "html_url": f"https://github.com/{REPO}/pull/7"}}
        self.commits = {HEAD: {"sha": HEAD, "commit": {"message": "huddles\n\nbody"}, "author": {"login": "claude"},
                               "parents": [{"sha": BASE}], "files": [{"filename": "rook/plugins/huddle/x.py",
                                                                      "status": "added", "additions": 3,
                                                                      "deletions": 0}]}}
        self.runs = {HEAD: [{"name": "pytest (3.12)", "status": "completed", "conclusion": "success"},
                            {"name": "build", "status": "completed", "conclusion": "success"}]}
        self.diffs = {(BASE, HEAD): "diff --git a/x.py b/x.py\n+++ b/x.py\n+print('hi')\n"}
        self.files = {(HEAD, "README.md"): "hello\n"}
        self.requests: list = []
        self.writes: list = []

    def handle(self, h: BaseHTTPRequestHandler, method: str):
        path, _, query = h.path.partition("?")
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"null")
        self.requests.append((method, path, h.headers.get("Authorization")))
        parts = path.strip("/").split("/")
        if parts[:3] != ["repos", *REPO.split("/")]:
            return 404, {"message": "Not Found"}
        rest = parts[3:]
        if rest[0] == "pulls" and len(rest) == 2 and method == "GET":
            pr = self.prs.get(int(rest[1]))
            return (200, pr) if pr else (404, {"message": "Not Found"})
        if rest[0] == "pulls" and rest[2:] == ["merge"] and method == "PUT":
            pr = self.prs[int(rest[1])]
            self.writes.append(("merge", int(rest[1]), body))
            if body["sha"] != pr["head"]["sha"]:
                return 409, {"message": "Head branch was modified. Review and try the merge again."}
            pr.update(state="closed", merged=True, merge_commit_sha="c" * 40)
            return 200, {"merged": True, "sha": "c" * 40, "message": "Pull Request successfully merged"}
        if rest[0] == "issues" and method == "POST":
            self.writes.append((rest[2], int(rest[1]), body))
            return 200, {"id": 1, "html_url": "x"} if rest[2] == "comments" else [{"name": "x"}]
        if rest[0] == "commits" and len(rest) == 2:
            c = self.commits.get(rest[1])
            return (200, c) if c else (404, {"message": "No commit found"})
        if rest[0] == "commits" and rest[2] == "check-runs":
            return 200, {"check_runs": self.runs.get(rest[1], [])}
        if rest[0] == "commits" and rest[2] == "status":
            return 200, {"state": "success", "statuses": []}
        if rest[0] == "compare":
            base, _, head = rest[1].partition("...")
            if "diff" in (h.headers.get("Accept") or ""):
                return 200, self.diffs.get((base, head), "").encode()
            return 200, {"files": [{"filename": "x.py", "status": "added", "additions": 1, "deletions": 0}]}
        if rest[0] == "contents":
            ref = query.partition("ref=")[2]
            text = self.files.get((ref, "/".join(rest[1:])))
            return (200, text.encode()) if text is not None else (404, {"message": "Not Found"})
        return 404, {"message": "Not Found"}

    def start(self) -> str:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                code, out = fake.handle(self, method)
                data = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._go("GET")

            def do_PUT(self):
                self._go("PUT")

            def do_POST(self):
                self._go("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.timeout = 5
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# -- the rig -------------------------------------------------------------------------------

PROBE = """\
manifest: 1
plugin: probe
version: 1.0.0
doc: What a deploy changes.
runs_on: hub
caps:
  set:
    doc: Set a value.
    risk: write
    scope: self
    on: hub
    run: scripts/probe.py:set_
    args: { key: { type: str, required: true }, value: { type: str, required: true } }
  get:
    doc: Read a value.
    risk: read
    scope: self
    on: hub
    run: scripts/probe.py:get
    args: { key: { type: str, required: true } }
"""

PROBE_PY = """\
import json

def _load(ctx):
    f = ctx.data / "state.json"
    return json.loads(f.read_text()) if f.exists() else {}

def set_(ctx, key, value):
    s = _load(ctx)
    s[key] = value
    (ctx.data / "state.json").write_text(json.dumps(s))
    return {"ok": True}

def get(ctx, key):
    return {"value": _load(ctx).get(key)}
"""

# The chair's inbox: agents.chat.send from the agents package, written down.
AGENTS = """\
manifest: 1
plugin: agents
version: 1.0.0
doc: Stands in for the agents package.
runs_on: hub
caps:
  chat.send:
    doc: Message an agent.
    risk: write
    scope: multi-user
    on: hub
    run: scripts/agents.py:chat_send
    args:
      agent: { type: str, required: true }
      text: { type: str, required: true }
      wait: { type: bool, default: true }
"""

AGENTS_PY = """\
import json

def chat_send(ctx, agent, text, wait=True):
    with open(ctx.data / "sent.jsonl", "a") as f:
        f.write(json.dumps({"agent": agent, "text": text, "from": [ctx.person, ctx.agent]}) + "\\n")
    return {"ok": True}
"""


def plugin(lib: Path, name: str, text: str, scripts: dict) -> None:
    (lib / name / "scripts").mkdir(parents=True)
    (lib / name / "plugin.yaml").write_text(text)
    for f, body in scripts.items():
        (lib / name / "scripts" / f).write_text(body)


@pytest.fixture
def gh():
    fake = FakeGitHub()
    fake.url = fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def cfg(tmp_path, gh, monkeypatch):
    monkeypatch.setenv("ROOK_SECRET_GITHUB_HUB", TOKEN)
    lib = tmp_path / "lib"
    plugin(lib, "probe", PROBE, {"probe.py": PROBE_PY})
    plugin(lib, "agents", AGENTS, {"agents.py": AGENTS_PY})
    for name in ("huddle", "github", "changes", "projects"):
        (lib / name).symlink_to(PLUGINS / name, target_is_directory=True)
    return Config(data=tmp_path / "data", band=BAND, library=lib,
                  users={"bake": {"preset": "owner"}, "amy": {"preset": "adult"}},
                  agents={"house": {"grants": ["*"]}, "coder": {"grants": ["*"]}},
                  settings={"github": {"api": gh.url, "token": "github-hub"},
                            "huddle": {"poll": "50ms", "gate_wait": "1s"},
                            "change": {"rejoin": "1s"}},
                  doors={"http": {"bind": "127.0.0.1:0"}})


class Door:
    def __init__(self, hub: Hub, token: str):
        self.hub, self.token = hub, token

    async def request(self, method: str, path: str, body: dict | None = None):
        def go():
            req = urllib.request.Request(f"http://127.0.0.1:{self.hub.doors[0].port}{path}", method=method,
                                         data=None if body is None else json.dumps(body).encode())
            req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())
        return await asyncio.to_thread(go)

    async def call(self, cap: str, **args):
        return await self.request("POST", "/call", {"worker": "rook", "cap": cap, "args": args, "why": WHY})

    async def ok(self, cap: str, **args):
        out = await self.call(cap, **args)
        assert out["ok"], out
        return out["result"]


class Rig:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events: list = []

    async def start(self) -> "Rig":
        self.hub = Hub(self.cfg, transport=Loopback())
        self.hub.listeners.append(lambda e, p, s: self.events.append((e, p)))
        await self.hub.start()
        self.bake = Door(self.hub, (self.hub.data / "owner.token").read_text().strip())
        self.amy = Door(self.hub, self.hub.add_token(Caller(person="person:amy"), label="amy"))
        self.house = Door(self.hub, self.hub.add_token(Caller(person="person:bake", agent="agent:house"),
                                                       label="house"))
        self.coder = Door(self.hub, self.hub.add_token(Caller(person="person:bake", agent="agent:coder"),
                                                       label="coder"))
        return self

    async def stop(self) -> None:
        await self.hub.stop()

    def motion(self, mid: str) -> dict:
        return self.hub.records.type("huddle.motion").get(KitCaller(band=BAND, owner=True), mid)

    def told(self) -> list[dict]:
        f = self.hub.data / "plugins" / "agents" / "sent.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def probe(self) -> dict:
        f = self.hub.data / "plugins" / "probe" / "state.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def calls(self, cap: str) -> list[dict]:
        return list(reversed(self.hub.journal.query(cap=cap, limit=500)))


async def until(pred, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        await asyncio.sleep(0.03)
    raise AssertionError(f"{what} not met in {timeout}s")


def on_rig(cfg, body, timeout=60):
    async def main():
        rig = await Rig(cfg).start()
        try:
            await body(rig)
        finally:
            await rig.stop()

    async def bounded():
        await asyncio.wait_for(main(), timeout)
    asyncio.run(bounded())


def ended(rig, mid):
    return lambda: rig.motion(mid)["state"] not in mod.OPEN and rig.motion(mid)


async def huddle_with_pr(rig, **start):
    h = await rig.bake.ok("huddle.start", title="Castling wave 7", **start)
    pin = await rig.house.ok("huddle.pin", huddle=h["huddle"], kind="pr", repo=REPO, number=7, sha=HEAD)
    return h, pin


# -- without a hub ----------------------------------------------------------------------------

def test_manifests_are_clean():
    for name in ("huddle", "github"):
        raw, issues = check(PLUGINS / name)
        assert raw is not None and not issues, [str(i) for i in issues]


def test_secret_hits_only_added_lines():
    diff = (f"--- a/x\n+++ b/x\n-old {TOKEN}\n context\n+new = '{TOKEN}'\n+fine\n"
            "+key = '-----BEGIN RSA PRIVATE KEY-----'\n")
    hits = mod.secret_hits(diff)
    assert [h.split(":")[0] for h in hits] == ["line 5", "line 7"] and TOKEN not in " ".join(hits)
    assert mod.fill({"argv": ["deploy", "{version}"], "n": 1}, HEAD) == {"argv": ["deploy", HEAD], "n": 1}


# -- on the hub ---------------------------------------------------------------------------------

def test_the_landing_flow_pin_call_gates_merge(cfg, gh):
    async def body(rig):
        h, pin = await huddle_with_pr(rig)
        assert pin["version"] == HEAD and pin["kind"] == "pr" and pin["title"].startswith(f"{REPO}#7")
        out = await rig.coder.ok("huddle.vote", huddle=h["huddle"], action="call", kind="merge",
                                 artifact=pin["artifact"], reason="Castling wave 7: merge only",
                                 self_review=REVIEW)
        assert out["state"] == "gating" and out["level"] == "normal" and out["version"] == HEAD
        m = await until(ended(rig, out["motion"]), what="the merge")
        assert m["state"] == "merged", m
        assert [(g["id"], g["ok"]) for g in m["gates"]] == [(g, True) for g in mod.GATES["merge"]]
        assert m["creator"] == "agent:coder" and m["person"] == "person:bake" and m["self_review"] == REVIEW
        merge = [w for w in gh.writes if w[0] == "merge"]
        assert merge == [("merge", 7, {"sha": HEAD, "merge_method": "merge", "commit_title":
                                       f"Merge pull request #7 from c7/huddles (vote {out['motion']}: "
                                       "no vote, normal)"})]
        # the hub's token, never in a reply, on every GitHub request
        assert all(auth == f"Bearer {TOKEN}" for _, _, auth in gh.requests)
        # journaled: the merge call, by the plugin, with the motion in its why
        entry = rig.calls("github.merge")[0]
        assert entry["agent"] == "system:plugin.huddle" and out["motion"] in entry["why"]
        assert entry["decision"] == "allow"
        # the thread tells the story
        thread = await rig.amy.ok("huddle.read", huddle=h["huddle"])
        texts = [e["data"]["text"] for e in thread["events"]]
        assert any("pinned pr" in t for t in texts) and any("Self-review: Read the full diff" in t for t in texts)
        assert texts[-1].startswith(f"merge {out['motion']}: merged.")
        st = await rig.coder.ok("huddle.vote", huddle=h["huddle"], action="status", motion=out["motion"])
        assert st["state"] == "merged"
    on_rig(cfg, body)


def test_only_a_person_or_the_chair_pins(cfg, gh):
    async def body(rig):
        h = await rig.bake.ok("huddle.start", title="Who pins")
        out = await rig.coder.call("huddle.pin", huddle=h["huddle"], kind="pr", repo=REPO, number=7)
        assert not out["ok"] and "only a person or the chair (agent:house)" in out["error"]
        assert (await rig.bake.ok("huddle.pin", huddle=h["huddle"], kind="pr", repo=REPO, number=7))["version"] == HEAD
        # a pin at a sha that isn't the head is refused (the chair pins what was asked, or nothing)
        out = await rig.house.call("huddle.pin", huddle=h["huddle"], kind="pr", repo=REPO, number=7, sha="d" * 40)
        assert not out["ok"] and "pin the head" in out["error"]
        # github writes are the huddle's alone
        out = await rig.bake.call("github.merge", repo=REPO, number=7, sha=HEAD, title="sneaky")
        assert not out["ok"] and "only the huddle" in out["error"] and not gh.writes
    on_rig(cfg, body)


def test_a_new_pin_supersedes_and_a_moved_head_voids(cfg, gh):
    async def body(rig):
        h, first = await huddle_with_pr(rig)
        again = await rig.house.ok("huddle.pin", huddle=h["huddle"], kind="pr", repo=REPO, number=7)
        assert again["already"] and again["artifact"] == first["artifact"]
        # the author pushes: the old pin can't be called, the new one supersedes it
        new = "e" * 40
        gh.prs[7]["head"]["sha"] = new
        gh.runs[new] = gh.runs[HEAD]
        gh.diffs[(BASE, new)] = gh.diffs[(BASE, HEAD)]
        out = await rig.coder.call("huddle.vote", huddle=h["huddle"], kind="merge", artifact=first["artifact"],
                                   self_review=REVIEW)
        assert out["ok"]       # still pinned until someone pins again: the version gate catches it
        m = await until(ended(rig, out["result"]["motion"]), what="the stale call")
        assert m["state"] == "gates_failed" and "the PR moved to eeeeeee" in m["outcome"]
        second = await rig.house.ok("huddle.pin", huddle=h["huddle"], kind="pr", repo=REPO, number=7)
        assert second["superseded"] == [first["artifact"]] and second["version"] == new
        out = await rig.coder.call("huddle.vote", huddle=h["huddle"], kind="merge", artifact=first["artifact"],
                                   self_review=REVIEW)
        assert not out["ok"] and f"call on the newest pin ({second['artifact']})" in out["error"]
        table = await rig.amy.ok("huddle.table", huddle=h["huddle"])
        assert [a["id"] for a in table["artifacts"]] == [second["artifact"]]
        # the head moves between the gates and the merge: GitHub's 409 voids it
        out = await rig.coder.ok("huddle.vote", huddle=h["huddle"], kind="merge", artifact=second["artifact"],
                                 self_review=REVIEW)
        real = gh.handle

        def moving(hh, method):
            if method == "PUT":
                gh.prs[7]["head"]["sha"] = "f" * 40
            return real(hh, method)
        gh.handle = moving
        m = await until(ended(rig, out["motion"]), what="the void")
        assert m["state"] == "void" and "no longer eeeeeee" in m["outcome"] and not gh.prs[7]["merged"]
    on_rig(cfg, body)


def test_the_gates_stop_it(cfg, gh):
    async def body(rig):
        h, pin = await huddle_with_pr(rig)
        a = pin["artifact"]
        out = await rig.coder.call("huddle.vote", huddle=h["huddle"], kind="merge", artifact=a, self_review="lgtm")
        assert not out["ok"] and "self_review" in out["error"]

        async def call():
            got = await rig.coder.ok("huddle.vote", huddle=h["huddle"], kind="merge", artifact=a, self_review=REVIEW)
            return await until(ended(rig, got["motion"]), what="the gates")

        gh.runs[HEAD] = [{"name": "pytest (3.12)", "status": "completed", "conclusion": "failure"}]
        m = await call()
        assert m["state"] == "gates_failed" and "tests failed: pytest (3.12)" in m["outcome"]
        assert "CI is not green" in m["outcome"]
        gh.runs[HEAD] = [{"name": "pytest (3.12)", "status": "in_progress", "conclusion": None}]
        m = await call()                                # still running after gate_wait (1 s)
        assert m["state"] == "gates_failed" and "in_progress" in m["outcome"]
        gh.runs[HEAD] = [{"name": "build", "status": "completed", "conclusion": "success"}]
        m = await call()
        assert "no test run among the checks" in m["outcome"]
        gh.runs[HEAD] = [{"name": "pytest", "status": "completed", "conclusion": "success"}]
        gh.diffs[(BASE, HEAD)] = f"+++ b/x\n+token = '{TOKEN}'\n"
        m = await call()
        assert m["state"] == "gates_failed" and "secrets" in m["outcome"] and TOKEN not in m["outcome"]
        gh.diffs[(BASE, HEAD)] = "+fine\n"
        gh.prs[7]["mergeable"], gh.prs[7]["mergeable_state"] = False, "dirty"
        m = await call()
        assert "conflicts with castling (dirty); rebase it" in m["outcome"]
        gh.prs[7]["mergeable"], gh.prs[7]["draft"] = True, True
        m = await call()
        assert "draft" in m["outcome"] and [g["id"] for g in m["gates"]] == ["version"]
        assert not gh.writes
    on_rig(cfg, body)


def test_a_sacred_project_asks_an_owner(cfg, gh):
    async def body(rig):
        careful = await rig.bake.ok("project.create", title="Careful", slug="careful")
        await rig.bake.ok("project.trust.create", project=careful["id"], level="sacred")
        h, pin = await huddle_with_pr(rig, project="careful")
        out = await rig.coder.ok("huddle.vote", huddle=h["huddle"], kind="merge", artifact=pin["artifact"],
                                 self_review=REVIEW)
        assert out["level"] == "sacred" and out["after"] == "an owner's card"
        m = await until(lambda: (r := rig.motion(out["motion"]))["state"] == "asked" and r, what="the card")
        assert m["hold"].startswith("h_") and not gh.writes
        # carry is the plugin's alone
        direct = await rig.bake.call("huddle.carry", motion=out["motion"])
        assert not direct["ok"]
        assert not (await rig.amy.request("POST", f"/holds/{m['hold']}", {"decision": "approve"}))["ok"]
        assert (await rig.bake.request("POST", f"/holds/{m['hold']}", {"decision": "approve"}))["ok"]
        m = await until(ended(rig, out["motion"]), what="the merge after the yes")
        assert m["state"] == "merged" and gh.prs[7]["merged"]
        # a no ends it
        gh.prs[8] = json.loads(json.dumps(gh.prs[7]) .replace('"merged": true', '"merged": false'))
        gh.prs[8].update(number=8, state="open", merged=False)
        pin8 = await rig.house.ok("huddle.pin", huddle=h["huddle"], kind="pr", repo=REPO, number=8)
        out = await rig.coder.ok("huddle.vote", huddle=h["huddle"], kind="merge", artifact=pin8["artifact"],
                                 self_review=REVIEW)
        m = await until(lambda: (r := rig.motion(out["motion"]))["state"] == "asked" and r, what="the card")
        assert (await rig.bake.request("POST", f"/holds/{m['hold']}", {"decision": "refuse"}))["ok"]
        m = await until(ended(rig, out["motion"]), what="declined")
        assert m["state"] == "declined" and len([w for w in gh.writes if w[0] == "merge"]) == 1
    on_rig(cfg, body)


def test_qa_and_deploy(cfg, gh):
    async def body(rig):
        h, pin = await huddle_with_pr(rig)
        out = await rig.coder.ok("huddle.vote", huddle=h["huddle"], kind="qa", artifact=pin["artifact"],
                                 self_review=REVIEW)
        m = await until(ended(rig, out["motion"]), what="qa")
        assert m["state"] == "done" and [g["id"] for g in m["gates"]] == list(mod.GATES["qa"])
        assert [w[0] for w in gh.writes] == ["labels", "comments"] and gh.writes[0][2] == {"labels": ["ready-for-qa"]}
        # a deploy of a pinned commit: a change record as the caller, {version} filled in
        commit = await rig.house.ok("huddle.pin", huddle=h["huddle"], kind="commit", repo=REPO, sha=HEAD)
        bad = await rig.coder.call("huddle.vote", huddle=h["huddle"], kind="deploy", artifact=commit["artifact"],
                                   self_review=REVIEW)
        assert not bad["ok"] and "change" in bad["error"]
        step = {"worker": "rook", "cap": "probe.set", "args": {"key": "deployed", "value": "{version}"}}
        change = {"risk": "The probe shows the new version.", "plan": "Set it, read it back, set it to old to undo.",
                  "scripts": [step], "tests": [{"worker": "rook", "cap": "probe.get", "args": {"key": "deployed"},
                                                "expect": HEAD}],
                  "rollback": {"worker": "rook", "cap": "probe.set", "args": {"key": "deployed", "value": "old"}}}
        out = await rig.coder.ok("huddle.vote", huddle=h["huddle"], kind="deploy", artifact=commit["artifact"],
                                 self_review=REVIEW, change=change)
        m = await until(ended(rig, out["motion"]), what="deploy")
        assert m["state"] == "done", m
        cid = m["result"]["change"]
        rec = await until(lambda: (r := rig.hub.records.type("change.change").get(
            KitCaller(band=BAND, owner=True), cid))["state"] == "kept" and r, what="the change kept")
        assert rec["creator"] == "agent:coder" and rec["person"] == "person:bake"
        assert rig.probe()["deployed"] == HEAD and rec["what"].startswith("Deploy Bake-Ware/rook@aaaaaaa")
    on_rig(cfg, body)


def test_the_table_the_thread_and_the_chair(cfg, gh):
    async def body(rig):
        h, pin = await huddle_with_pr(rig)
        hid, a = h["huddle"], pin["artifact"]
        summary = await rig.amy.ok("huddle.table", huddle=hid, action="read", artifact=a)
        assert summary["now"]["head"] == HEAD and summary["body"] == "R14"
        diff = await rig.amy.ok("huddle.table", huddle=hid, action="read", artifact=a, what="diff")
        assert "+print('hi')" in diff["text"] and diff["next_offset"] is None
        checks = await rig.amy.ok("huddle.table", huddle=hid, action="read", artifact=a, what="checks")
        assert {r["name"] for r in checks["checks"]["runs"]} == {"pytest (3.12)", "build"}
        f = await rig.amy.ok("huddle.table", huddle=hid, action="read", artifact=a, what="file", path="README.md")
        assert f["text"] == "hello\n"
        doc = await rig.bake.ok("huddle.pin", huddle=hid, kind="doc", text="Release notes\n" + "x" * 20000)
        page = await rig.amy.ok("huddle.table", huddle=hid, action="read", artifact=doc["artifact"], what="content")
        assert len(page["text"]) == mod.READ_CHARS and page["next_offset"] == mod.READ_CHARS
        assert len(doc["version"]) == 64
        fpin = await rig.bake.ok("huddle.pin", huddle=hid, kind="file", repo=REPO, sha=HEAD, path="README.md")
        got = await rig.amy.ok("huddle.table", huddle=hid, action="read", artifact=fpin["artifact"], what="content")
        assert got["text"] == "hello\n"
        # posts; naming the chair tells it, as the poster
        await rig.coder.ok("huddle.post", huddle=hid, text="Tests are green.")
        told = await rig.coder.ok("huddle.post", huddle=hid, text="@house please pin #7 at its head")
        assert told["chair"] == "told"
        sent = rig.told()
        assert len(sent) == 1 and sent[0]["agent"] == "house" and "please pin #7" in sent[0]["text"]
        assert sent[0]["from"] == ["person:bake", "agent:coder"]
        await rig.house.ok("huddle.post", huddle=hid, text="@house noted")          # the chair isn't told itself
        assert len(rig.told()) == 1
        thread = await rig.amy.ok("huddle.read", huddle=hid, after=0, limit=500)
        posts = [(e["by"], e["data"]["text"]) for e in thread["events"] if e["type"] == "text"]
        assert posts[0] == ("agent:coder", "Tests are green.")
        # closing: the creator or an owner; the thread stays readable
        assert not (await rig.amy.call("huddle.close", huddle=hid))["ok"]
        assert (await rig.bake.ok("huddle.close", huddle=hid))["state"] == "closed"
        assert not (await rig.coder.call("huddle.post", huddle=hid, text="late"))["ok"]
        assert (await rig.amy.ok("huddle.read", huddle=hid))["closed"]
    on_rig(cfg, body)


def test_after_a_restart_open_calls_end(cfg, gh):
    async def body():
        rig = await Rig(cfg).start()
        try:
            gh.runs[HEAD] = [{"name": "pytest", "status": "in_progress", "conclusion": None}]
            h, pin = await huddle_with_pr(rig)
            out = await rig.coder.ok("huddle.vote", huddle=h["huddle"], kind="merge", artifact=pin["artifact"],
                                     self_review=REVIEW)
        finally:
            await rig.stop()
        rig = await Rig(cfg).start()
        try:
            m = rig.motion(out["motion"])
            assert m["state"] == "failed" and "restarted" in m["outcome"]
            gh.runs[HEAD] = [{"name": "pytest", "status": "completed", "conclusion": "success"}]
            again = await rig.coder.ok("huddle.vote", huddle=h["huddle"], kind="merge", artifact=pin["artifact"],
                                       self_review=REVIEW)
            assert (await until(ended(rig, again["motion"]), what="the merge"))["state"] == "merged"
            thread = await rig.amy.ok("huddle.read", huddle=h["huddle"])
            assert thread["events"]            # the thread survived the restart
        finally:
            await rig.stop()

    async def bounded():
        await asyncio.wait_for(body(), 60)
    asyncio.run(bounded())
