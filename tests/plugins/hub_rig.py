"""A real 0.5 hub in process for the huddle and github plugin tests: a fake
GitHub (a local HTTP server speaking the REST endpoints the github plugin
uses), a vault seeded with its token, the workbench plugins, and a stand-in
for agent.chat.send that writes down what each agent was sent."""

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from port_helpers import PLUGINS
from rook.core2.hub.identity import Caller
from rook.core2.hub.runtime import Config, Hub, Loopback, _key
from rook.core2.hub.vault import Vault

BAND = "4a1e9b0c2d3f4e5a6b7c8d9e0f1a2b3c"
WHY = "Check the huddle and github plugins."
REPO = "Bake-Ware/rook"
HEAD = "a" * 40
BASE = "b" * 40
TOKEN = "ghp_" + "t" * 36


class FakeGitHub:
    """One repo's PRs, commits, checks, reviews and diffs in memory; records every request."""

    def __init__(self):
        self.prs = {7: self.new_pr(7)}
        self.runs = {HEAD: [{"name": "pytest (3.12)", "status": "completed", "conclusion": "success"},
                            {"name": "build", "status": "completed", "conclusion": "success"}]}
        self.diffs = {(BASE, HEAD): "diff --git a/x.py b/x.py\n+++ b/x.py\n+print('hi')\n"}
        self.reviews = {7: []}
        self.files = {(HEAD, "README.md"): "hello\n"}
        self.requests: list = []
        self.writes: list = []

    @staticmethod
    def new_pr(n, head=HEAD, author="claude-coder"):
        return {"number": n, "title": "Some work", "body": "R14", "state": "open", "merged": False,
                "draft": False, "mergeable": True, "mergeable_state": "clean",
                "head": {"sha": head, "ref": "c7/work"}, "base": {"sha": BASE, "ref": "castling"},
                "user": {"login": author}, "html_url": f"https://github.com/{REPO}/pull/{n}"}

    def review(self, n, login, state, commit=HEAD):
        self.reviews.setdefault(n, []).append({"user": {"login": login}, "state": state, "commit_id": commit,
                                               "body": ""})

    def handle(self, h: BaseHTTPRequestHandler, method: str):
        path, _, query = h.path.partition("?")
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"null")
        self.requests.append((method, path, h.headers.get("Authorization")))
        parts = path.strip("/").split("/")
        if [p.lower() for p in parts[:3]] != ["repos", *REPO.lower().split("/")]:
            return 404, {"message": "Not Found"}
        rest = parts[3:]
        if rest[0] == "pulls" and len(rest) == 2 and method == "GET":
            pr = self.prs.get(int(rest[1]))
            return (200, pr) if pr else (404, {"message": "Not Found"})
        if rest[0] == "pulls" and rest[2:] == ["reviews"] and method == "POST":
            self.writes.append(("review", int(rest[1]), body))
            return 200, {"id": 9, "html_url": f"https://github.com/{REPO}/pull/{rest[1]}#review-9"}
        if rest[0] == "pulls" and rest[2:] == ["reviews"]:
            return 200, self.reviews.get(int(rest[1]), [])
        if rest[0] == "pulls" and rest[2:] == ["merge"] and method == "PUT":
            pr = self.prs[int(rest[1])]
            self.writes.append(("merge", int(rest[1]), body))
            if body["sha"] != pr["head"]["sha"]:
                return 409, {"message": "Head branch was modified. Review and try the merge again."}
            pr.update(state="closed", merged=True, merge_commit_sha="c" * 40)
            return 200, {"merged": True, "sha": "c" * 40, "message": "Pull Request successfully merged"}
        if rest[0] == "commits" and len(rest) == 3 and rest[2] == "check-runs":
            return 200, {"check_runs": self.runs.get(rest[1], [])}
        if rest[0] == "commits" and len(rest) == 3 and rest[2] == "status":
            return 200, {"state": "success", "statuses": []}
        if rest[0] == "compare":
            base, _, head = rest[1].partition("...")
            return 200, self.diffs.get((base, head), "").encode()
        if rest[0] == "contents":
            text = self.files.get((query.partition("ref=")[2], "/".join(rest[1:])))
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
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self.url

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


AGENTS = """\
manifest: 1
plugin: agent
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
    if agent == "ghost":
        raise ctx.Error("no agent ghost")
    with open(ctx.data / "sent.jsonl", "a") as f:
        f.write(json.dumps({"agent": agent, "text": text, "from": [ctx.person, ctx.agent]}) + "\\n")
    return {"ok": True}
"""


def seed_vault(data: Path, **values) -> None:
    """Secrets in the hub's vault before it starts (as ``secret set`` would)."""
    (data / "keys").mkdir(parents=True, exist_ok=True)
    _key(data / "keys" / "root.key")
    v = Vault(data).unlock()
    for name, value in values.items():
        v.set(name, value, by="test")


def config(tmp_path: Path, gh: FakeGitHub, vault: bool = True, security: str = "on", **github) -> Config:
    """``security``: the hub.security setting (these tests check sign-off with
    it on; move-in is checked on its own)."""
    if vault:
        seed_vault(tmp_path / "data", **{"github-hub": TOKEN})
    lib = tmp_path / "lib"
    (lib / "agents" / "scripts").mkdir(parents=True)
    (lib / "agents" / "plugin.yaml").write_text(AGENTS)
    (lib / "agents" / "scripts" / "agents.py").write_text(AGENTS_PY)
    for name in ("huddle", "github", "tasks", "projects", "concepts"):
        (lib / name).symlink_to(PLUGINS / name, target_is_directory=True)
    return Config(data=tmp_path / "data", band=BAND, library=lib,
                  users={"bake": {"preset": "owner"}, "amy": {"preset": "adult"}},
                  agents={"coder": {"grants": ["*"]}, "qa": {"grants": ["*"]}},
                  settings={"github": {"api": gh.url, "token": "github-hub", **github},
                            "hub": {"security": security}},
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
                with urllib.request.urlopen(req, timeout=30) as r:
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
    async def start(self, cfg: Config) -> "Rig":
        self.hub = Hub(cfg, transport=Loopback())
        await self.hub.start()
        self.bake = Door(self.hub, (self.hub.data / "owner.token").read_text().strip())
        self.amy = Door(self.hub, self.hub.add_token(Caller(person="person:amy"), label="amy"))
        self.coder = Door(self.hub, self.hub.add_token(Caller(person="person:bake", agent="agent:coder"),
                                                       label="coder"))
        self.qa = Door(self.hub, self.hub.add_token(Caller(person="person:bake", agent="agent:qa"), label="qa"))
        return self

    def told(self) -> list[dict]:
        f = self.hub.data / "plugins" / "agent" / "sent.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def calls(self, cap: str) -> list[dict]:
        return list(reversed(self.hub.journal.query(cap=cap, limit=500)))


def on_rig(cfg, body, timeout=60):
    async def main():
        rig = await Rig().start(cfg)
        try:
            await body(rig)
        finally:
            await rig.hub.stop()

    async def bounded():
        await asyncio.wait_for(main(), timeout)
    asyncio.run(bounded())

