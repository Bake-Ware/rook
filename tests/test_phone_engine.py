"""A phone must not break the house agent (brief AP6). Every Python worker gets agents-claude-code,
and the Rook Android app's worker is a Python worker; with the agents plugin's `worker` setting
blank, the engine is the one online worker that announces agents-claude-code.turn. A phone cannot
run Claude Code, so on the app (where Chaquopy's `java` module imports) the plugin announces nothing,
and a hub with one PC worker and a phone still has one engine.

The phone here is a real worker process whose Python finds a stand-in `java` module, as the app's
does; the other is an ordinary worker in this process."""

import asyncio
import os
import signal
import subprocess
import sys
import types
from pathlib import Path

from rook_hub import Hub
from rook_hub.plugins import DEFAULTS
from rook_worker import Worker
from test_default_plugins import until

ROOT = Path(__file__).parent.parent
TURN = "agents-claude-code.turn"
WHY = "a test of the house agent's engine beside a phone"


def part(java):
    """agents-claude-code's worker part, run as a worker runs it, with `java` importable or not."""
    saved = sys.modules.pop("java", None)
    if java:
        sys.modules["java"] = types.ModuleType("java")
    try:
        namespace = {}
        exec(compile((DEFAULTS / "agents-claude-code" / "worker.py").read_text(), "worker.py", "exec"), namespace)
        return namespace["CAPS"]
    finally:
        sys.modules.pop("java", None)
        if saved is not None:
            sys.modules["java"] = saved


def test_on_the_app_agents_claude_code_announces_nothing():
    assert list(part(java=False)) == [TURN]
    assert part(java=True) == {}


def test_a_phone_beside_one_pc_leaves_the_house_agent_one_engine(tmp_path):
    asyncio.run(beside(tmp_path))


async def beside(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    url = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "a password", first=True)["guid"]
    pc = Worker(tmp_path / "pc", url, name="pc", macs=[], knock_every=0.02)
    task = asyncio.create_task(pc.run())
    fake = tmp_path / "chaquopy"
    fake.mkdir()
    (fake / "java.py").write_text('"""A stand-in for Chaquopy\'s java module: the worker is on a phone."""\n')
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(p) for p in (ROOT / "protocol/python", ROOT / "core/python", fake))}
    phone = subprocess.Popen([sys.executable, "-m", "rook_worker", url, "--state", str(tmp_path / "phone")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await until(lambda: len(hub.list_pending()) == 2, timeout=30)
        for knock in hub.list_pending():
            hub.approve(knock["key"], user=alice)

        def announced():
            return {w["name"]: {c["name"] for c in w["caps"] or []} for w in hub.list_workers() if w["online"]}

        # Both online, each announcing its default plugins (shell's, at least): only the PC the engine.
        await until(lambda: len(announced()) == 2 and all("shell.exec" in caps for caps in announced().values()),
                    timeout=30)
        caps = announced()
        assert TURN in caps["pc"] and [name for name, c in caps.items() if TURN in c] == ["pc"], caps

        # The house agent, with its `worker` setting blank, is ready: its one engine is the PC.
        reply = await hub.call_or_follow("hub", "agent.site", {}, WHY, user=alice)
        assert reply["type"] == "result" and reply["result"]["ready"] is True, reply
    finally:
        phone.send_signal(signal.SIGINT)
        phone.wait()
        pc.stop()
        await asyncio.gather(task, return_exceptions=True)
        await hub.stop_serving()
        hub.close()
