"""The Rust worker (worker/) against a real hub over a real UDP relay, from
the same files enrollment writes for a Python worker: it joins, shows in the
worker list with its caps, runs a command, writes and reads a file, and goes
offline when stopped. Its manifest hashes are the Python worker's."""
from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("telesthete")
from rook.core2 import wire  # noqa: E402
from rook.core2.hub.runtime import Config, Hub  # noqa: E402
from rook.core2.transport import TelestheteLink, band_of  # noqa: E402

from test_core2_minimal_worker import Relay, door, until  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = {"worker": ROOT / "rook/core2/builtin/worker.yaml", "shell": ROOT / "rook/plugins/shell/plugin.yaml",
             "file": ROOT / "rook/plugins/file/plugin.yaml"}


@pytest.fixture(scope="module")
def binary() -> Path:
    if not shutil.which("cargo"):
        pytest.skip("cargo is not installed")
    subprocess.run(["cargo", "build", "--quiet", "--manifest-path", str(ROOT / "worker/Cargo.toml")],
                   check=True, timeout=900)
    return ROOT / "worker/target/debug/rook-worker"


def enrolled(home: Path, psk: str, relay: str, worker_id: str) -> Path:
    """What `python -m rook.core2.worker enroll` leaves: worker.yaml and <data>/bands.json."""
    (home / "data").mkdir(parents=True)
    (home / "data" / "bands.json").write_text(json.dumps({band_of(psk): {"psk": psk, "relay": relay}}))
    config = home / "worker.yaml"
    config.write_text(yaml.safe_dump({"data": "data", "anchor": "anchor.json", "name": "rusty", "worker_id": worker_id}))
    return config


def test_the_rust_worker_on_a_real_hub(tmp_path, binary):
    now = [time.time()]

    async def go():
        relay, _ = await asyncio.get_running_loop().create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
        addr = "127.0.0.1:%d" % relay.get_extra_info("socket").getsockname()[1]
        psk = secrets.token_hex(24)
        hub = Hub(Config(data=tmp_path / "hub", users={"bake": {"preset": "owner"}},
                         doors={"http": {"bind": "127.0.0.1:0"}},
                         transport={"bands": [{"psk": psk, "relay": addr}]}), clock=lambda: now[0])
        await hub.start()
        token = (hub.data / "owner.token").read_text().strip()
        wid = secrets.token_hex(16)
        heard: list[dict] = []

        async def overheard(payload: bytes) -> None:
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            if isinstance(msg, dict) and msg.get("kind") == "announce" and msg.get("worker_id") == wid:
                heard.append(msg)

        async def nothing() -> None:
            pass

        ear = TelestheteLink(psk, relay=addr)
        await ear.start(overheard, nothing)
        proc = await asyncio.create_subprocess_exec(str(binary), "--config", str(enrolled(tmp_path / "w", psk, addr, wid)))

        async def listed():
            got = (await door(hub, token, "/workers"))["workers"]
            return next((x for x in got if x["name"] == "rusty"), None)

        async def call(cap, **args):
            return await door(hub, token, "/call", {"worker": "rusty", "cap": cap, "args": args,
                                                    "why": "Test the Rust worker."})

        async def beat_heard():
            return (await listed())["last_seen"] == now[0]
        try:
            me = await until(listed)
            assert (me["id"], me["online"], me["problem"]) == (wid, True, None)
            assert {"shell", "file"} <= set(me["plugins"]), me
            assert me["unavailable"]["worker.log"].startswith("the hub's journal"), me

            # The same manifests, the same hashes as a Python worker announces.
            entries = {e["plugin"]: e["hash"] for e in heard[0]["manifests"]}
            assert entries == {p: wire.manifest_hash(yaml.safe_load(f.read_text())) for p, f in MANIFESTS.items()}

            async def learned():        # the hub has fetched its manifests (core.manifests)
                caps = (await door(hub, token, "/caps?worker=rusty"))["workers"].get("rusty", {}).get("caps", {})
                return {"worker.ping", "shell.exec", "file.read"} <= set(caps)
            await until(learned)
            out = await call("worker.ping")
            assert out["ok"] and out["result"]["pong"] is True, out
            out = await call("shell.exec", argv=["echo", "hello"])
            assert out["ok"] and out["result"] == {"ok": True, "code": 0, "stdout": "hello\n", "stderr": ""}, out
            out = await call("shell.exec", cmd="echo $((40+2)) >&2; exit 3")
            assert out["result"] == {"ok": False, "code": 3, "stdout": "", "stderr": "42\n"}, out
            out = await call("shell.which", name="sh")
            assert out["result"] and out["result"].endswith("/sh"), out

            target = tmp_path / "files" / "note.txt"
            out = await call("file.write", path=str(target), content="héllo\nworld\n", create_parents=True)
            assert out["result"] == {"path": str(target), "written": 13}, out
            out = await call("file.read", path=str(target))
            assert out["result"] == {"content": "héllo\nworld\n", "size": 13, "encoding": "utf-8"}, out
            out = await call("file.search", pattern="^wor", path=str(tmp_path / "files"))
            assert out["result"] == {"matches": [{"path": str(target), "line": 2, "text": "world"}]}, out
            out = await call("file.list", path=str(tmp_path / "files"))
            assert [(e["name"], e["type"], e["size"]) for e in out["result"]["entries"]] == [("note.txt", "file", 13)]
            out = await call("file.stat", path=str(tmp_path / "nope"))
            assert out["result"] == {"exists": False}, out
            out = await call("file.read", path=str(tmp_path / "nope"))
            assert not out["ok"] and "not found" in out["error"], out
            out = await call("worker.log")
            assert not out["ok"] and "journal" in out["error"], out

            async def answered_hub():   # its announce on first hearing the hub's, so what comes next is a heartbeat
                return len(heard) >= 2
            await until(answered_hub)
            now[0] += 60            # a minute of hub time: its next heartbeat (24-36 s) keeps it online
            await until(beat_heard, 40)
            hub._liveness()
            assert (await listed())["online"] is True

            proc.terminate()
            await proc.wait()
            await asyncio.sleep(0.5)
            now[0] += 91
            hub._liveness()
            assert (await listed())["online"] is False
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await ear.stop()
            await hub.stop()
            relay.close()
    asyncio.run(asyncio.wait_for(go(), 180))
