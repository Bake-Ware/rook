"""The Agents page on a hub with a worker: the page is listed under Agents and
declares only caps that exist, and the calls it makes work: the engines and
their models (Claude Code's without its worker, OpenCode's from ``opencode
models`` on the worker), the house agent's model and engine set the way the
page sets them reach the CLI on the next turn, the default engine, and the
attention pass on and off."""
from __future__ import annotations

import json
import re
import secrets
import sys

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")

from rook.core2.transport import Transport  # noqa: E402
from test_core2_worker_runtime import BAND, OP, PSK, REPO, ROOT, WID, go, until  # noqa: E402

FAKE_CLAUDE = """#!{python}
import json, sys
argv = sys.argv[1:]
sys.stdin.read()
with open({log!r}, "a") as f:
    f.write(json.dumps(["claude", *argv]) + "\\n")
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "claude here",
                  "session_id": "c-1"}}))
"""

FAKE_OPENCODE = """#!{python}
import json, sys
argv = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps(["opencode", *argv]) + "\\n")
if argv[:1] == ["models"]:
    print("opencode/big-pickle")
    print("opencode/beam-free")
    sys.exit(0)
sys.stdin.read()
for ev in ({{"type": "step_start"}}, {{"type": "text", "part": {{"text": "opencode here"}}}},
           {{"type": "step_finish", "part": {{"reason": "stop"}}}}):
    print(json.dumps({{**ev, "sessionID": "o-1"}}))
"""


def test_the_agents_page(tmp_path, monkeypatch):
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
    for name in ("agents", "agents-claude-code", "agents-opencode", "jobs", "attention"):
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
                              "agents-claude-code": {"worker": "kaiju", "claude": str(bins / "claude")},
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

        page = next(p for p in (await ok("page.list", "List pages."))["pages"] if p["page"] == "agent/agents")
        assert page["section"] == "Agents" and page["title"] == "Agents"
        served = hub.dispatcher.workers[hub.node].caps
        assert set(page["caps"]) <= set(served), set(page["caps"]) - set(served)
        grants += page["caps"]
        html = (REPO / "rook/plugins/agents/pages/agents.html").read_text()
        assert set(re.findall(r'(?<!data-target=)"([a-z]+\.[a-z][a-z.]*)"', html)) == set(page["caps"])

        # The engines: Claude Code the default, OpenCode not ready (no worker set yet).
        eng = await ok("agent.engines", "Show the engines.")
        assert eng["default"] == "agents-claude-code.turn"
        rows = {e["engine"]: e for e in eng["engines"]}
        assert rows["agents-claude-code.turn"]["ready"] is True
        assert rows["agents-opencode.turn"] == {"engine": "agents-opencode.turn", "title": "OpenCode backend",
                                                "ready": False,
                                                "why": "no worker is set for it (agents-opencode.worker)"}
        # Claude Code's list needs no worker; the default engine is asked when none is named.
        assert await ok("agent.models", "Models.") == {"engine": "agents-claude-code.turn", "default": "opus",
                                                       "models": ["sonnet", "opus", "haiku", "fable"]}
        assert not ran()

        # The house agent's model, the way the page sets it (its System Three kept).
        (house,) = await ok("agent.list", "Agents.", where={"name": "house"})

        async def update(why, **fields):           # as the page does: on the record as it is now
            now = await ok("agent.get", "The house agent now.", id=house["id"])
            return await ok("agent.update", why, id=now["id"], revision=now["revision"], **fields)
        house = await update("Set the model.", model={**(house.get("model") or {}), "two": "sonnet", "three": "haiku"})
        assert house["model"] == {"two": "sonnet", "three": "haiku"}
        out = await ok("agent.chat.send", "Talk to the house agent.", agent="house", text="hello")
        assert out["text"] == "claude here"
        argv = ran()[-1]
        assert argv[0] == "claude" and argv[argv.index("--model") + 1] == "sonnet"
        # A session from before engines were stored resolved ("default" for the
        # default engine): its conversation goes on, it doesn't start over.
        meta = hub.streams.streams.update(out["stream"], attrs={"harness_backend": "default"})
        assert meta.attrs["harness"] == "c-1"
        await ok("agent.chat.send", "Talk to the house agent.", agent="house", text="still there?")
        argv = ran()[-1]
        assert argv[0] == "claude" and argv[argv.index("--resume") + 1] == "c-1"
        assert hub.streams.streams.get(out["stream"]).attrs["harness_backend"] == "agents-claude-code.turn"

        # Its engine: OpenCode, once its worker is set; its models come from the worker.
        await ok("setting.set", "Name OpenCode's worker.", plugin="agents-opencode", key="worker", value="kaiju")
        listed = await ok("agent.models", "Models.", engine="agents-opencode.turn")
        assert listed == {"engine": "agents-opencode.turn", "default": "",
                          "models": ["opencode/big-pickle", "opencode/beam-free"]}
        assert ran()[-1] == ["opencode", "models"]
        house = await update("Switch the engine.", backend="agents-opencode.turn", model={"three": "haiku"})
        out = await ok("agent.chat.send", "Talk to the house agent.", agent="house", text="hello again")
        assert out["text"] == "opencode here"
        argv = ran()[-1]
        assert argv[:2] == ["opencode", "run"] and "--model" not in argv and "--session" not in argv
        house = await update("A model id.", model={"two": "opencode/beam-free", "three": "haiku"})
        await ok("agent.chat.send", "Talk to the house agent.", agent="house", text="once more")
        argv = ran()[-1]
        assert argv[argv.index("--model") + 1] == "opencode/beam-free" and argv[argv.index("--session") + 1] == "o-1"

        # Back on the default engine, then the default engine changed: a new
        # session in the new engine, not the other one's session id.
        house = await update("Back to the default.", backend="default", model={"three": "haiku"})
        await ok("agent.chat.send", "Talk.", agent="house", text="claude?")
        assert ran()[-1][0] == "claude" and "--resume" not in ran()[-1]
        await ok("setting.set", "Default engine.", plugin="agent", key="backend", value="agents-opencode.turn")
        assert (await ok("agent.engines", "Engines."))["default"] == "agents-opencode.turn"
        await ok("agent.chat.send", "Talk.", agent="house", text="opencode?")
        assert ran()[-1][:2] == ["opencode", "run"] and "--session" not in ran()[-1]
        # The setting blank (the first installed: Claude Code), then set again: each a change of engine.
        await ok("setting.set", "Default engine.", plugin="agent", key="backend", value="")
        assert (await ok("agent.engines", "Engines."))["default"] == "agents-claude-code.turn"
        await ok("agent.chat.send", "Talk.", agent="house", text="claude again?")
        assert ran()[-1][0] == "claude" and "--resume" not in ran()[-1]
        await ok("setting.set", "Default engine.", plugin="agent", key="backend", value="agents-opencode.turn")
        await ok("agent.chat.send", "Talk.", agent="house", text="opencode again?")
        assert ran()[-1][:2] == ["opencode", "run"] and "--session" not in ran()[-1]

        # The attention pass: the same calls as the first-run skill to turn it on, the job's own switch off.
        (tpl,) = [t for t in await ok("job.templates", "Ready-made jobs.") if t["template"] == "attention/attention"]
        assert tpl["installed"] is False and tpl["doc"]
        job = await ok("job.install", "Attention on.", template="attention/attention")
        assert job["enabled"] is True
        (found,) = await ok("job.list", "Find it.", where={"source": "attention/attention"}, limit=1)
        job = await ok("job.update", "Attention off.", id=found["id"], revision=found["revision"], enabled=False)
        assert job["enabled"] is False

        await hub.stop()
        await worker.stop()

    go(body, 90)
