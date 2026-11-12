"""The Engines page on a hub with a worker: the page is listed under Agents
and declares only caps that exist, and the calls it makes work with its
grants: each engine's settings and models, the workers it can run on, an
engine's worker set the way the page sets it turns it ready, and its model
set the way the page sets it reaches the CLI on a turn of an agent on the
default model."""
from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import sys

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")

from rook.core2.transport import Transport  # noqa: E402
from test_core2_agents_page import FAKE_CLAUDE, FAKE_OPENCODE  # noqa: E402
from test_core2_worker_runtime import BAND, OP, PSK, REPO, ROOT, WID, go, until  # noqa: E402

PAGE = REPO / "rook/plugins/agents/pages/engines.html"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_engines_page_script_parses(tmp_path):
    for page in (PAGE, REPO / "rook/plugins/agents/pages/agents.html"):
        script = re.search(r"<script>(.*)</script>", page.read_text(), re.S).group(1)
        (tmp_path / "page.js").write_text(script)
        subprocess.run(["node", "--check", str(tmp_path / "page.js")], check=True)


def test_the_engines_page(tmp_path, monkeypatch):
    from rook.core2.hub.identity import Caller as HubCaller
    from rook.core2.hub.runtime import Config as HubConfig, Hub
    from rook.core2.transport import LoopbackRelay
    from rook.core2.worker.runtime import Config, Worker
    from rook.core2 import wire

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    log = tmp_path / "cli.log"
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, text in (("claude", FAKE_CLAUDE), ("opencode", FAKE_OPENCODE)):
        (bins / name).write_text(text.format(python=sys.executable, log=str(log)))
        (bins / name).chmod(0o755)

    local = tmp_path / "local"
    local.mkdir()
    for name in ("shell", "file"):
        (local / name).symlink_to(REPO / "rook/plugins" / name, target_is_directory=True)
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"hub": wire.key_id(bytes(ROOT.verify_key)), "root": wire.pub_text(ROOT),
                                  "band": BAND}))
    relay = LoopbackRelay()
    worker = Worker(Config(data=tmp_path / "wdata", anchor=anchor, name="kaiju", worker_id=WID,
                           bands=[{"psk": PSK}], plugins=[local]),
                    make_link=lambda spec: relay.link(spec["psk"]), transport_opts={"every": 0.05})

    library = tmp_path / "library"
    library.mkdir()
    for name in ("agents", "agents-claude-code", "agents-opencode"):
        (library / name).symlink_to(REPO / "rook/plugins" / name, target_is_directory=True)
    keys = tmp_path / "hub" / "keys"
    keys.mkdir(parents=True)
    (keys / "root.key").write_text(bytes(ROOT).hex())
    (keys / "op.key").write_text(bytes(OP).hex())
    node = secrets.token_hex(16)
    (tmp_path / "hub" / "hub.json").write_text(json.dumps({"node": node}))
    cfg = HubConfig(data=tmp_path / "hub", band=BAND, users={"bake": {"preset": "owner"}}, library=library,
                    doors={"http": {"bind": "127.0.0.1:0"}},
                    settings={"agent": {"backend": "agents-claude-code.turn"},
                              "agents-claude-code": {"worker": "", "claude": str(bins / "claude")},
                              "agents-opencode": {"worker": "", "opencode": str(bins / "opencode")}})
    bake = HubCaller(person="person:bake")

    def ran() -> list:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    async def body():
        await worker.start()
        hub = Hub(cfg, transport=Transport(node, [relay.link(PSK)]))
        await hub.start()

        grants: list = []

        async def ok(cap, why, **args):         # the page's calls carry its grants, as the site sends them
            layers = [["page", {"grants": grants}]] if cap in grants else None
            out = await hub.call(bake, {"worker": "rook", "cap": cap, "args": args, "why": why}, "test", layers=layers)
            assert out["ok"], (cap, out)
            return out["result"]
        await until(lambda: "shell.open" in set(getattr(hub.dispatcher.workers.get(WID), "caps", ())), 10)

        sidebar = await ok("page.list", "List pages.")
        page = next(p for s in sidebar["segments"] for p in s["pages"] if p["page"] == "agent/engines")
        assert page["segment"] == "Agents" and page["title"] == "Engines"
        served = hub.dispatcher.workers[hub.node].caps
        assert set(page["caps"]) <= set(served), set(page["caps"]) - set(served)
        grants += page["caps"]
        html = PAGE.read_text()
        assert set(re.findall(r'(?<!data-target=)"([a-z]+\.[a-z][a-z.]*)"', html)) == set(page["caps"])
        assert "rook.open(\"setting/settings\")" in html and "rook.open(\"agent/agents\")" in html
        assert "rook.open(\"agent/engines\")" in (REPO / "rook/plugins/agents/pages/agents.html").read_text()

        # What the page reads: the engines (neither has a worker yet), the
        # workers but the hub, and each engine's worker, model and light model.
        eng = await ok("agent.engines", "Show the engines on the Engines page.")
        assert eng["default"] == "agents-claude-code.turn"
        rows = {e["engine"]: e for e in eng["engines"]}
        assert rows["agents-claude-code.turn"] == {"engine": "agents-claude-code.turn", "title": "Claude Code",
                                                   "ready": False,
                                                   "why": "no worker is set for it (agents-claude-code.worker)"}
        assert rows["agents-opencode.turn"]["title"] == "OpenCode" and rows["agents-opencode.turn"]["ready"] is False
        workers = [w for w in (await ok("hub.workers", "List the machines."))["workers"] if not w.get("hub")]
        assert [(w["name"], w["online"]) for w in workers] == [("kaiju", True)]
        for plugin in ("agents-claude-code", "agents-opencode"):
            mine = {s["key"]: s for s in (await ok("setting.list", "Settings.", plugin=plugin))["settings"]}
            assert {"worker", "model", "model_three"} <= set(mine) and mine["worker"]["plugin_doc"]
            assert "blank: " in mine["model"]["doc"] and "blank: " in mine["model_three"]["doc"]
        assert (await ok("agent.models", "Models.", engine="agents-claude-code.turn"))["default"] == "opus"

        # The worker, the way the page sets it: the engine is ready on the next ask.
        await ok("setting.set", "Run Claude Code on kaiju.", plugin="agents-claude-code", key="worker", value="kaiju")
        rows = {e["engine"]: e for e in (await ok("agent.engines", "Ready now?"))["engines"]}
        assert rows["agents-claude-code.turn"]["ready"] is True and rows["agents-claude-code.turn"]["why"] is None
        await ok("setting.set", "Run OpenCode on kaiju.", plugin="agents-opencode", key="worker", value="kaiju")
        assert all(e["ready"] for e in (await ok("agent.engines", "Ready now?"))["engines"])
        assert (await ok("agent.models", "Models.", engine="agents-opencode.turn"))["models"] == \
            ["opencode/big-pickle", "opencode/beam-free"]

        # The model: a turn of an agent on the default model runs the CLI with it.
        (house,) = await ok("agent.list", "Agents.", where={"name": "house"})
        if (house.get("model") or {}).get("two"):
            house = await ok("agent.update", "Default model.", id=house["id"], revision=house["revision"], model={})
        await ok("setting.set", "Set Claude Code's model.", plugin="agents-claude-code", key="model", value="sonnet")
        assert (await ok("agent.models", "Models.", engine="agents-claude-code.turn"))["default"] == "sonnet"
        out = await ok("agent.chat.send", "Talk to the house agent.", agent="house", text="hello")
        assert out["text"] == "claude here"
        argv = ran()[-1]
        assert argv[0] == "claude" and argv[argv.index("--model") + 1] == "sonnet"
        # Another model id, then blank: Claude Code's own default (no --model).
        await ok("setting.set", "A model id.", plugin="agents-claude-code", key="model", value="claude-x-1")
        await ok("agent.chat.send", "Talk.", agent="house", text="again")
        argv = ran()[-1]
        assert argv[argv.index("--model") + 1] == "claude-x-1"
        await ok("setting.set", "Blank.", plugin="agents-claude-code", key="model", value="")
        await ok("agent.chat.send", "Talk.", agent="house", text="once more")
        assert "--model" not in ran()[-1]
        await ok("setting.set", "Light model.", plugin="agents-claude-code", key="model_three", value="sonnet")
        mine = {s["key"]: s for s in (await ok("setting.list", "Settings.", plugin="agents-claude-code"))["settings"]}
        assert mine["model_three"]["value"] == "sonnet" and mine["model"]["value"] == ""

        # Not set again: not ready, and why.
        await ok("setting.set", "Unset the worker.", plugin="agents-claude-code", key="worker", value="")
        rows = {e["engine"]: e for e in (await ok("agent.engines", "Ready now?"))["engines"]}
        assert rows["agents-claude-code.turn"]["why"] == "no worker is set for it (agents-claude-code.worker)"

        await hub.stop()
        await worker.stop()

    go(body, 90)
