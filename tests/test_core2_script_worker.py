"""worker/rook_worker.py, the one-file Python worker: its carrier against
the conformance vectors, its manifests against their sources, and the file
run as a subprocess against a real hub over a UDP relay (enroll with a code,
join, caps, a command, files, the too-large error, heartbeats, offline)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "worker" / "rook_worker.py"
VECTORS = ROOT / "conformance" / "vectors"


def script():
    spec = importlib.util.spec_from_file_location("rook_worker_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def vectors(name):
    return json.loads((VECTORS / f"{name}.json").read_text())


def test_the_cipher_and_keys_match_the_vectors():
    rw = script()
    for c in vectors("band_crypto")["cases"]:
        key = rw.band_key(c["psk"])
        assert (key.hex(), rw.band_id(c["psk"]).hex()) == (c["key"], c["band_id"])
        sealed = rw.seal(key, int(c["sequence"]), bytes.fromhex(c["plaintext"]))
        assert sealed.hex() == c["ciphertext"]
        assert rw.open_(key, int(c["sequence"]), sealed) == bytes.fromhex(c["plaintext"])
        assert rw.open_(key, int(c["sequence"]) + 1, sealed) is None


def test_frames_and_fragments_match_the_vectors():
    rw = script()
    for c in vectors("frames")["cases"]:
        link = rw.Link.__new__(rw.Link)
        link.band, link.key = rw.band_id(c["psk"]), rw.band_key(c["psk"])
        chunks = rw.split(bytes.fromhex(c["message"]), bytes.fromhex(c["fragment_id"]))
        assert [link.frame(c["first_sequence"] + i, ch).hex() for i, ch in enumerate(chunks)] == c["frames"]
    f = vectors("fragments")
    for c in f["split"]:
        assert [x.hex() for x in rw.split(bytes.fromhex(c["payload"]), bytes.fromhex(c["fragment_id"]))] == c["chunks"]
    for c in f["parse"]:
        got = rw.parse_chunk(bytes.fromhex(c["chunk"]))
        assert (got is not None) == c["valid"], c["label"]
        if got:
            assert (got[0].hex(), got[1], got[2], got[3].hex()) == (c["fragment_id"], c["seq"], c["total"], c["data"])
    for c in f["reassemble"]:
        r = rw.Reassembler()
        emitted = [out.hex() for out in (r.feed(bytes.fromhex(x)) for x in c["feed"]) if out is not None]
        assert emitted == c["emits"], c["label"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh and process groups")
def test_exec_times_out_when_a_child_outlives_the_shell(tmp_path):
    rw, pidfile = script(), tmp_path / "pid"
    with pytest.raises(rw.Err, match="timed out after 0.5s"):
        rw.exec_(cmd=f"sleep 30 & echo $! > {pidfile}", timeout=0.5)
    pid = int(pidfile.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the background child outlived the timeout")


def test_the_web_socket_is_the_hubs_address_unless_the_band_names_another(tmp_path, monkeypatch):
    rw = script()
    monkeypatch.setattr(rw, "HOME", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "worker.yaml").write_text(json.dumps({"worker_id": "0" * 32, "name": "s"}))
    (tmp_path / "data" / "enrollment.json").write_text(json.dumps({"url": "https://rook.example"}))
    band = {"psk": "p", "relay": "127.0.0.1:9"}
    (tmp_path / "data" / "bands.json").write_text(json.dumps({"b": band}))
    assert rw.Worker().link.ws_url == "wss://rook.example/band"
    (tmp_path / "data" / "bands.json").write_text(json.dumps({"b": {**band, "ws_url": "ws://10.0.0.2:8740/band"}}))
    assert rw.Worker().link.ws_url == "ws://10.0.0.2:8740/band"


def test_the_embedded_manifests_are_the_sources():
    r = subprocess.run([sys.executable, str(ROOT / "worker" / "gen_script_manifests.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, "worker/rook_worker.py is stale: run python worker/gen_script_manifests.py"


def test_the_script_worker_on_a_real_hub(tmp_path):
    pytest.importorskip("telesthete")
    from rook.core2 import wire
    from rook.core2.hub.runtime import Config, Hub
    from rook.core2.transport import TelestheteLink
    from test_core2_minimal_worker import Relay, door, secret_env, until

    now = [time.time()]
    env = {**os.environ, "HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")}
    sources = {"worker": "rook/core2/builtin/worker.yaml", "shell": "rook/plugins/shell/plugin.yaml",
               "file": "rook/plugins/file/plugin.yaml"}

    async def go():
        relay, _ = await asyncio.get_running_loop().create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
        addr = "127.0.0.1:%d" % relay.get_extra_info("socket").getsockname()[1]
        psk = secrets.token_hex(24)
        hub = Hub(Config(data=tmp_path / "hub", users={"bake": {"preset": "owner"}},
                         doors={"http": {"bind": "127.0.0.1:0"}},
                         transport={"bands": [{"psk": psk, "relay": addr}]}), clock=lambda: now[0])
        await hub.start()
        token = (hub.data / "owner.token").read_text().strip()
        heard: list = []

        async def overheard(payload: bytes) -> None:
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            if isinstance(msg, dict) and msg.get("kind") == "announce" and msg.get("name") == "scripty":
                heard.append(msg)

        async def nothing() -> None:
            pass

        ear = TelestheteLink(psk, relay=addr)
        await ear.start(overheard, nothing)
        proc = None

        async def call(cap, worker="scripty", **args):
            return await door(hub, token, "/call", {"worker": worker, "cap": cap, "args": args,
                                                    "why": "Test the one-file worker."})

        async def listed():
            got = (await door(hub, token, "/workers"))["workers"]
            return next((x for x in got if x["name"] == "scripty"), None)
        try:
            code = (await door(hub, token, "/enroll/code", {}))["code"]
            url = f"http://127.0.0.1:{hub.doors[0].port}"
            p = await asyncio.create_subprocess_exec(sys.executable, str(SCRIPT), "enroll", url, "--code", code,
                                                     "--name", "scripty", env=env, stdout=asyncio.subprocess.PIPE)
            out, _ = await p.communicate()
            assert p.returncode == 0 and b"Enrolled scripty" in out, out
            home = tmp_path / "home" / ".rook-worker"
            assert json.loads((home / "anchor.json").read_text())["hub"] == hub.hub_id
            assert yaml.safe_load((home / "worker.yaml").read_text())["name"] == "scripty"     # JSON is YAML
            assert list(json.loads((home / "data" / "bands.json").read_text()).values())[0]["relay"] == addr

            proc = await asyncio.create_subprocess_exec(sys.executable, str(SCRIPT), env=env)
            me = await until(listed)
            assert me["online"] and {"shell", "file"} <= set(me["plugins"]), me
            assert me["unavailable"]["shell.open"].startswith("rook_worker.py has no streams"), me
            assert {e["plugin"]: e["hash"] for e in heard[0]["manifests"]} == \
                {p: wire.manifest_hash(yaml.safe_load((ROOT / f).read_text())) for p, f in sources.items()}

            async def learned():
                caps = (await door(hub, token, "/caps?worker=scripty"))["workers"].get("scripty", {}).get("caps", {})
                return {"worker.ping", "shell.exec", "file.read", "file.search"} <= set(caps)
            await until(learned)
            out = await call("shell.exec", argv=["echo", "hello"])
            assert out["result"] == {"ok": True, "code": 0, "stdout": "hello\n", "stderr": ""}, out
            out = await call("shell.exec", cmd="echo $((40+2)) >&2; exit 3")
            assert out["result"] == {"ok": False, "code": 3, "stdout": "", "stderr": "42\n"}, out
            await secret_env(hub, call)          # a secret reaches a command's environment by name
            target = tmp_path / "files" / "note.txt"
            out = await call("file.write", path=str(target), content="héllo\nworld\n", create_parents=True)
            assert out["result"] == {"path": str(target), "written": 13}, out
            out = await call("file.read", path=str(target))
            assert out["result"] == {"content": "héllo\nworld\n", "size": 13, "encoding": "utf-8"}, out
            out = await call("file.search", pattern="^wor", path=str(tmp_path / "files"))
            assert out["result"] == {"matches": [{"path": str(target), "line": 2, "text": "world"}]}, out
            assert (await call("file.stat", path=str(tmp_path / "nope")))["result"] == {"exists": False}
            out = await call("file.read", path=str(tmp_path / "nope"))
            assert not out["ok"] and out["error"] == f"not found: {tmp_path / 'nope'}", out
            (tmp_path / "files" / "big.bin").write_bytes(os.urandom(300 * 1024))
            out = await call("file.read", path=str(tmp_path / "files" / "big.bin"), encoding="base64")
            assert not out["ok"] and "result too large" in out["error"] and "262144" in out["error"], out
            out = await call("file.read", path=str(tmp_path / "files" / "big.bin"), encoding="base64",
                             max_bytes=100 * 1024)
            assert out["ok"] and out["result"]["truncated"], str(out)[:200]
            out = await call("shell.open")
            assert not out["ok"] and "no streams" in out["error"], out
            assert (await call("worker.ping"))["result"]["pong"] is True

            async def answered_hub():          # its announce on hearing the hub's: what follows is a heartbeat
                return len(heard) >= 2
            await until(answered_hub)
            now[0] += 60

            async def beat_heard():
                return (await listed())["last_seen"] == now[0]
            await until(beat_heard, 45)
            hub._liveness()
            assert (await listed())["online"] is True

            proc.terminate()
            await proc.wait()
            now[0] += 91
            hub._liveness()
            assert (await listed())["online"] is False
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            await ear.stop()
            await hub.stop()
            relay.close()
    asyncio.run(asyncio.wait_for(go(), 150))
