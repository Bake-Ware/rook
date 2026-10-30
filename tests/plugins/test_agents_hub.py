"""The agents plugin on a real 0.5 hub (rook.core2.hub.runtime) with a fake
agent.backend provider in the library, called through the HTTP door: the
roles are made on start, a turn reaches the provider as the person who sent
the message (so a guest gets only a guest's rights), and the dream cycle
runs as the plugin."""

import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import PLUGINS  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the agents plugin on the hub."

BRAIN = """\
manifest: 1
plugin: agents-local
version: 1.0.0
doc: A fake local agent backend.
runs_on: hub
caps:
  turn:
    doc: One fake agent turn.
    risk: exec
    scope: local
    on: hub
    run: scripts/brain.py:turn
    args:
      prompt: { type: str, required: true }
      session: { type: str }
      cwd: { type: str }
      model: { type: str }
      effort: { type: str }
      system: { type: str }
      timeout: { type: duration }
    result:
      type: map
      fields:
        text: { type: str, required: true }
        session: { type: str, required: true }
        usage: { type: map, items: { type: int } }
provides:
  agent.backend: agents-local.turn
"""

BRAIN_PY = """\
import json

def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout=None):
    if system and "curate an agent's memory" in system:
        return {"text": json.dumps({"add": ["learned: " + model]}), "session": "dream"}
    return {"text": "brain: " + prompt.splitlines()[-1], "session": session or "s1"}
"""


async def http(hub, path, body=None, token=None):
    def go():
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{hub.doors[0].port}{path}", data=data,
                                     method="POST" if data else "GET")
        req.add_header("Authorization", f"Bearer {token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())
    return await asyncio.to_thread(go)


async def call(hub, token, cap, **args):
    return await http(hub, "/call", {"worker": "rook", "cap": cap, "args": args, "why": WHY}, token)


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_agents_on_the_hub(tmp_path):
    lib = tmp_path / "lib"
    (lib / "agents-local" / "scripts").mkdir(parents=True)
    (lib / "agents-local" / "plugin.yaml").write_text(BRAIN)
    (lib / "agents-local" / "scripts" / "brain.py").write_text(BRAIN_PY)
    (lib / "agents").symlink_to(PLUGINS / "agents", target_is_directory=True)
    cfg = Config(data=tmp_path / "data", band=BAND, library=lib,
                 users={"bake": {"preset": "owner"}, "guest": {"preset": "guest", "created": time.time()}},
                 settings={"agents": {"warm": "200ms"}}, doors={"http": {"bind": "127.0.0.1:0"}})

    async def body():
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            owner = (hub.data / "owner.token").read_text().strip()
            guest = hub.add_token(Caller(person="person:guest"), label="guest")
            assert {"agents", "agents-local"} <= set(hub.plugins.plugins)     # beside the builtin ones
            out = await call(hub, owner, "agents.list")
            assert out["ok"] and len(out["result"]) == 8, out

            out = await call(hub, owner, "agents.chat.send", agent="house", text="good morning")
            assert out["ok"] and out["result"]["text"] == "brain: good morning", out
            turn = hub.journal.query(cap="agents-local.turn", limit=1)[0]
            assert (turn["person"], turn["ok"]) == ("person:bake", True)      # as the sender

            # A guest may chat (agents.chat.*), but the turn runs with the
            # guest's rights, and core's guest preset grants no backend.
            out = await call(hub, guest, "agents.chat.send", agent="house", text="open the door")
            assert out == {"id": out["id"], "ok": False, "error": "house: denied: person: grants"}, out
            assert not (await call(hub, guest, "agents.list"))["ok"]
            out = await call(hub, guest, "agents.chat.read", agent="house")
            assert out["ok"] and "good morning" not in json.dumps(out["result"]), out

            out = await call(hub, owner, "agents.create", name="Bad Name")
            assert not out["ok"] and out["error"].startswith("bad args"), out

            # Idle: the house agent is always on; qa sleeps after warm and
            # dreams as the plugin itself.
            assert (await call(hub, owner, "agents.chat.send", agent="qa", text="tests?"))["ok"]
            assert (await call(hub, owner, "agents.note", agent="qa", text="CI is green on #12"))["ok"]
            await until(lambda: hub.journal.query(cap="agents-local.turn", agent="system:plugin.agents"))
            got = (await call(hub, owner, "agents.get", name="qa"))["result"]
            assert got["memory"][0]["text"] == "learned: haiku" and got["notepad"] == []
            assert got["awake"] is False
        finally:
            await hub.stop()
    asyncio.run(asyncio.wait_for(body(), 30))
