"""A hub that already exists, upgraded: agents-claude-code is now a hub
script (its CLI runs on the worker its ``worker`` setting names), while an
older worker keeps announcing the worker-run agents-claude-code 0.5.1
(tests/fixtures/agents-claude-code-0.5.1: that release's manifest). The hub
starts and takes the announce without an error, a turn goes to the hub's
plugin, the fake ``claude`` on the worker reaches Rook with the turn's token
from its environment, the journal holds the token's name only, and before
the ``worker`` setting is set the site says not ready and a turn fails
saying which setting to set."""
from __future__ import annotations

import json
import logging
import secrets
import sys
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")

from rook.core2.transport import Transport  # noqa: E402
from test_core2_worker_runtime import BAND, OP, PSK, REPO, ROOT, WID, go, until  # noqa: E402

FAKE_CLAUDE = """#!{python}
import json, os, sys, urllib.request
argv = sys.argv[1:]
prompt = sys.stdin.read()
with open({log!r}, "a") as f:
    f.write(json.dumps({{"argv": argv, "cwd": os.getcwd()}}) + "\\n")
cfg = json.load(open(argv[argv.index("--mcp-config") + 1]))["mcpServers"]["rook"]
auth = cfg["headers"]["Authorization"].replace("${{ROOK_DOOR_TOKEN}}", os.environ.get("ROOK_DOOR_TOKEN", ""))
req = urllib.request.Request(cfg["url"] + "/call", method="POST", data=json.dumps(
    {{"worker": "kaiju", "cap": "worker.ping", "why": "The turn checks its door."}}).encode())
req.add_header("Authorization", auth)
req.add_header("Content-Type", "application/json")
with urllib.request.urlopen(req, timeout=10) as r:
    ok = json.loads(r.read())["ok"]
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "door ok" if ok else "door refused", "session_id": "sess-1"}}))
"""


def test_an_older_worker_announcing_the_worker_run_engine(tmp_path, monkeypatch, caplog):
    from rook.core2.hub.identity import Caller as HubCaller
    from rook.core2.hub.runtime import Config as HubConfig, Hub
    from rook.core2.transport import LoopbackRelay
    from rook.core2.worker.runtime import Config, Worker
    from rook.core2 import wire

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    log = tmp_path / "claude.log"
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(log)))
    fake.chmod(0o755)

    local = tmp_path / "local"
    local.mkdir()
    for name in ("shell", "file"):
        (local / name).symlink_to(REPO / "rook/plugins" / name, target_is_directory=True)
    (local / "agents-claude-code").symlink_to(REPO / "tests/fixtures/agents-claude-code-0.5.1",
                                              target_is_directory=True)
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"hub": wire.key_id(bytes(ROOT.verify_key)), "root": wire.pub_text(ROOT),
                                  "band": BAND}))
    relay = LoopbackRelay()
    worker = Worker(Config(data=tmp_path / "wdata", anchor=anchor, name="kaiju", worker_id=WID,
                           bands=[{"psk": PSK}], plugins=[local]),
                    make_link=lambda spec: relay.link(spec["psk"]), transport_opts={"every": 0.05})

    library = tmp_path / "library"
    library.mkdir()
    for name in ("agents", "agents-claude-code"):
        (library / name).symlink_to(REPO / "rook/plugins" / name, target_is_directory=True)
    keys = tmp_path / "hub" / "keys"
    keys.mkdir(parents=True)
    (keys / "root.key").write_text(bytes(ROOT).hex())
    (keys / "op.key").write_text(bytes(OP).hex())
    node = secrets.token_hex(16)
    (tmp_path / "hub" / "hub.json").write_text(json.dumps({"node": node}))
    cfg = HubConfig(data=tmp_path / "hub", band=BAND, users={"bake": {"preset": "owner"}}, library=library,
                    doors={"http": {"bind": "127.0.0.1:0"}},
                    settings={"agent": {"backend": "agents-claude-code.turn"}})
    bake = HubCaller(person="person:bake")

    async def body():
        await worker.start()
        hub = Hub(cfg, transport=Transport(node, [relay.link(PSK)]))
        await hub.start()
        look, tokens = hub.identities.turn_token, []

        def spy(name):
            tokens.append(look(name))
            return tokens[-1]
        monkeypatch.setattr(hub.identities, "turn_token", spy)
        caps = lambda: set(getattr(hub.dispatcher.workers.get(WID), "caps", ()))   # noqa: E731
        await until(lambda: {"agents-claude-code.turn", "shell.open"} <= caps(), 10)
        assert hub.plugins.plugins["agents-claude-code"].manifest["runs_on"] == "hub"

        async def call(cap, args=None, worker="rook"):
            return await hub.call(bake, {"worker": worker, "cap": cap, "args": args,
                                         "why": "Checking an upgraded hub."}, "test")
        made = await call("agent.create", {"name": "tester", "title": "Tester", "grants": ["worker.ping"]})
        assert made["ok"], made
        await until(lambda: "tester" in hub.identities.run_by, 10)

        # Nobody has done the one-time steps yet: not ready, and a turn says which setting to set.
        assert (await call("agent.site"))["result"]["ready"] is False
        out = await call("agent.chat.send", {"agent": "tester", "text": "hello?"})
        assert "set agents-claude-code.worker" in json.dumps(out), out
        assert not log.exists()

        for key, value in (("worker", "kaiju"), ("claude", str(fake)),
                           ("door_url", f"http://127.0.0.1:{hub.doors[0].port}")):
            done = await call("setting.set", {"plugin": "agents-claude-code", "key": key, "value": value})
            assert done["ok"], done
        assert (await call("agent.site"))["result"]["ready"] is True       # its worker is online
        out = await call("agent.chat.send", {"agent": "tester", "text": "check your door"})
        assert out["ok"] and out["result"]["text"] == "door ok", out
        (ran,) = [json.loads(line) for line in log.read_text().splitlines()]
        assert ran["cwd"] == str(home / ".rook-agents" / "agents-claude-code" / "work")
        turn = hub.journal.query(cap="agents-claude-code.turn", limit=1)[0]
        assert turn["worker"] == node                                    # the hub's plugin, not the old one
        opened = hub.journal.query(cap="shell.open", limit=1)[0]
        assert opened["worker"] == WID and opened["args"]["env"]["ROOK_DOOR_TOKEN"].startswith("{{secret:turn-")
        back = hub.journal.query(cap="worker.ping", limit=1)[0]
        assert (back["person"], back["agent"], back["ok"]) == ("person:bake", "agent:tester", True)
        [token] = [t for t in tokens if t]                   # resolved once, on the hop to the worker
        assert not [e for e in hub.journal.query(limit=500) if token in json.dumps(e)]
        written = [p.read_text() for p in (home / ".rook-agents").rglob("*") if p.is_file()]
        assert any("${ROOK_DOOR_TOKEN}" in text for text in written) and not any(token in t for t in written)
        await hub.stop()
        await worker.stop()

    with caplog.at_level(logging.WARNING):
        go(body, 60)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
