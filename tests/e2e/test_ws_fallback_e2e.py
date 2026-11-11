"""The band's web socket fallback with real processes: the real relay
(telesthitium, UDP and its ``/band`` web socket), the hub's door passing
``/band`` through to it, one worker on UDP and one whose UDP goes nowhere,
sharing the band. Needs ``ROOK_TELESTHETE_HUB``: a built ``telesthete-hub``
(``cargo build --release -p telesthitium`` in telesthete/rust)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys

import pytest
import yaml

from harness import REPO, Band, Proc, until

RELAY = os.environ.get("ROOK_TELESTHETE_HUB")
pytestmark = pytest.mark.skipif(not RELAY, reason="ROOK_TELESTHETE_HUB names no telesthete-hub binary")


def _free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WsBand(Band):
    """A Band whose relay is telesthitium with its web socket on, and whose
    hub door passes ``/band`` to it."""

    def start_relay(self) -> str:
        if not getattr(self, "udp_port", None):
            self.udp_port, self.ws_port = _free_port(socket.SOCK_DGRAM), _free_port(socket.SOCK_STREAM)
        self.env.update(HUB_BIND=f"127.0.0.1:{self.udp_port}", HUB_WS_BIND=f"127.0.0.1:{self.ws_port}",
                        RUST_LOG="info", NO_COLOR="1")
        self.relay = self._spawn("relay", [RELAY], self.root)
        until(lambda: any("ws transport listening" in line for line in self.relay.lines), 30, "the relay")
        self.relay_addr = f"127.0.0.1:{self.udp_port}"
        return self.relay_addr

    def write(self) -> None:
        super().write()
        path = self.hub_dir / "hub.yaml"
        cfg = json.loads(path.read_text())
        cfg["doors"]["http"]["band"] = f"ws://127.0.0.1:{self.ws_port}/band"
        path.write_text(json.dumps(cfg, indent=1))

    def add_worker(self, name: str, relay: str, kind: str = "python") -> Proc:
        """Another worker (python, rust or script), enrolled, whose UDP goes to ``relay``."""
        folder = self.root / name
        folder.mkdir()
        env, code = dict(self.env), self.owner.post("/enroll/code", {})
        if kind == "python":
            cfg = yaml.safe_load((self.worker_dir / "worker.yaml").read_text())
            (folder / "worker.yaml").write_text(json.dumps({k: cfg[k] for k in ("data", "plugins", "log")}
                                                           | {"name": name}))
            r = self.worker_cli("enroll", code["qr"], cwd=folder)
            argv, data = [sys.executable, "-m", "rook.core2.worker", "--config", "worker.yaml"], folder / "data"
        elif kind == "rust":
            subprocess.run(["cargo", "build", "--quiet", "--manifest-path", str(REPO / "worker/Cargo.toml")],
                           check=True, timeout=900)
            binary = str(REPO / "worker/target/debug/rook-worker")
            r = subprocess.run([binary, "--config", "worker.yaml", "enroll", self.url, "--code", code["code"],
                                "--name", name],
                               cwd=folder, env=env, capture_output=True, text=True, timeout=60)
            argv, data = [binary, "--config", "worker.yaml"], folder / "data"
        else:
            env["HOME"] = str(folder)
            script = str(REPO / "worker/rook_worker.py")
            r = subprocess.run([sys.executable, script, "enroll", self.url, "--code", code["code"], "--name", name],
                               cwd=folder, env=env, capture_output=True, text=True, timeout=60)
            argv, data = [sys.executable, script], folder / ".rook-worker" / "data"
        assert r.returncode == 0, r.stderr
        bands = json.loads((data / "bands.json").read_text())
        assert all(set(b) == {"psk", "relay"} for b in bands.values()), bands
        (data / "bands.json").write_text(json.dumps({k: {**b, "relay": relay} for k, b in bands.items()}))
        p = Proc(name, argv, folder, env, self.root / f"{name}.log")
        self.procs.append(p)
        if kind == "python":
            p.wait_log("worker started")
        return p


@pytest.fixture
def ws_band(request, tmp_path):
    b = WsBand(tmp_path)
    request.node._e2e_band = b
    b.write()
    try:
        yield b
    finally:
        b.close()


def _said(proc, text: str) -> int:
    return sum(text in line for line in proc.lines)


def _serves(band: WsBand, worker: str, tmp_path, size: int, terminal: bool = True) -> None:
    """shell.exec, a file read of ``size`` bytes and a terminal stream on ``worker``."""
    out = band.owner.call("shell.exec", {"argv": ["sh", "-c", "echo $0", worker]}, worker=worker)
    assert out["ok"] and out["result"]["stdout"].strip() == worker, out
    blob = os.urandom(size)
    (tmp_path / f"{worker}.bin").write_bytes(blob)
    got = band.owner.call("file.read", {"path": str(tmp_path / f"{worker}.bin"), "encoding": "base64"},
                          worker=worker)
    assert got["ok"] and base64.b64decode(got["result"]["content"]) == blob, str(got)[:300]
    if not terminal:
        return
    sid = band.owner.call("shell.open", {"argv": ["cat"], "pty": False}, worker=worker)["result"]["stream"]
    assert band.owner.call("stream.write", {"stream": sid, "body": {"text": "over the door\n"}}, worker=worker)["ok"]
    seen, after = b"", 0
    for _ in range(20):
        r = band.owner.call("stream.read", {"stream": sid, "after": after, "wait": 1}, worker=worker)
        after = r["result"]["next"]
        seen += b"".join(base64.b64decode(e["b64"]) for e in r["result"]["events"] if "b64" in e)
        if b"over the door" in seen:
            break
    assert b"over the door" in seen
    assert band.owner.call("stream.close", {"stream": sid}, worker=worker)["ok"]


def test_a_worker_without_udp_joins_through_the_hub_door(ws_band, tmp_path):
    band = ws_band
    band.start_hub()
    band.start_worker()                      # UDP works for this one
    band.wait_caps("e2e.ping")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as hole:     # takes datagrams, never answers
        hole.bind(("127.0.0.1", 0))
        blocked = band.add_worker("e2e-blocked", f"127.0.0.1:{hole.getsockname()[1]}")
        until(lambda: band.caps("e2e-blocked").get("online"), 60, "the blocked worker online")
        assert _said(blocked, "nothing heard over UDP") == 1 and _said(blocked, "WS connected") == 1
        _serves(band, "e2e-blocked", tmp_path, 3 << 20)
        _serves(band, "e2e-worker", tmp_path, 64 << 10)     # the UDP worker shares the band
        assert not _said(band.worker, "nothing heard over UDP") and not _said(band.worker, "WS connected")

        # The relay restarts: the web socket drops and comes back by itself.
        band.relay.kill()
        band.start_relay()
        until(lambda: _said(blocked, "WS connected") >= 2, 60, "the web socket back")
        until(lambda: band.owner.call("shell.exec", {"argv": ["true"]}, worker="e2e-blocked").get("ok"),
              60, "a call over the web socket again", every=1)
        _serves(band, "e2e-blocked", tmp_path, 1 << 20)
        assert _said(blocked, "nothing heard over UDP") == 1     # one fallback per run


def _online(band: WsBand, name: str) -> None:
    until(lambda: band.caps(name).get("online"), 60, f"{name} online")


@pytest.mark.skipif(not shutil.which("cargo"), reason="cargo is not installed")
def test_the_rust_worker_and_the_script_fall_back_too(ws_band, tmp_path):
    band = ws_band
    band.start_hub()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as hole:
        hole.bind(("127.0.0.1", 0))
        nowhere = f"127.0.0.1:{hole.getsockname()[1]}"
        rusty = band.add_worker("rusty-blocked", nowhere, "rust")
        scripty = band.add_worker("script-blocked", nowhere, "script")
        _online(band, "rusty-blocked")
        _online(band, "script-blocked")
        for p in (rusty, scripty):
            assert _said(p, "nothing heard over UDP") == 1 and _said(p, "on the band through") == 1, p.lines
        _serves(band, "rusty-blocked", tmp_path, 3 << 20)
        _serves(band, "script-blocked", tmp_path, 64 << 10, terminal=False)   # replies inline, at most 256 KB

        band.relay.kill()
        band.start_relay()
        for p in (rusty, scripty):
            until(lambda p=p: _said(p, "on the band through") >= 2, 60, f"{p.name} back on the web socket")
        for name in ("rusty-blocked", "script-blocked"):
            until(lambda name=name: band.owner.call("shell.exec", {"argv": ["true"]}, worker=name).get("ok"),
                  60, f"a call to {name} again", every=1)
        _serves(band, "rusty-blocked", tmp_path, 1 << 20)
