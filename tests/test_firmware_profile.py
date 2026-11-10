"""The ESP32-S3 dongle firmware against the minimal worker profile
(docs/spec/worker-profile.md), without the hardware:

- the hashes its build writes into include/manifest.h are the hub's own
  hashes of rook/plugins/dongle-s3/plugin.yaml, so the two cannot drift;
- the firmware's cap table serves exactly that manifest's caps;
- firmware/src/profile.cpp (announce, heartbeat, calls, replies, fragment
  reassembly), compiled for this machine with g++ and ArduinoJson, joins a
  real hub over a real relay and answers its calls, fed the hub's own
  messages as carrier fragments. Its caps are stand-ins (the real ones need
  the hardware); the carrier crypto stays in Python (TelestheteLink)."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("telesthete")
from rook.core2 import wire  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, _entries_hash  # noqa: E402
from rook.core2.transport import TelestheteLink  # noqa: E402
from rook.kit.manifest import check  # noqa: E402
from test_core2_minimal_worker import Relay, door, until  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FIRMWARE = REPO / "firmware"
PLUGIN = REPO / "rook" / "plugins" / "dongle-s3"


def generated(tmp_path: Path) -> dict:
    out = tmp_path / "manifest.h"
    subprocess.run([sys.executable, str(FIRMWARE / "scripts" / "manifest_header.py"), str(PLUGIN / "plugin.yaml"),
                    str(out)], check=True)
    return dict(re.findall(r'#define (\w+) "([^"]*)"', out.read_text()))


def test_the_announced_hashes_are_the_hubs(tmp_path):
    raw, issues = check(PLUGIN)
    assert raw is not None and not [i for i in issues if i.level == "error"]
    got = generated(tmp_path)
    entries = [{"plugin": raw["plugin"], "version": raw["version"], "hash": wire.manifest_hash(raw)}]
    assert got == {"ROOK_MANIFEST_PLUGIN": raw["plugin"], "ROOK_MANIFEST_VERSION": raw["version"],
                   "ROOK_MANIFEST_HASH": wire.manifest_hash(raw), "ROOK_MANIFESTS_HASH": _entries_hash(entries)}


def test_the_firmware_serves_the_manifests_caps():
    raw, _ = check(PLUGIN)
    source = (FIRMWARE / "src" / "telesthete.cpp").read_text()
    table = source[source.index("static const Cap CAPS[]"):]
    table = table[:table.index("};")]
    assert sorted(re.findall(r'\{"([a-z.]+)",\s*cap_\w+\}', table)) == sorted(raw["caps"])


def arduinojson() -> Path | None:
    found = sorted(FIRMWARE.glob(".pio/libdeps/*/ArduinoJson/src/ArduinoJson.h"))
    return found[0].parent if found else None


def split(payload: bytes) -> list[bytes]:
    """The carrier's fragment envelope (core v2 2.1): at most 1003 data bytes each."""
    fid = secrets.token_bytes(16)
    pieces = [payload[i:i + 1003] for i in range(0, len(payload), 1003)] or [b""]
    return [b"\x01" + fid + i.to_bytes(2, "big") + len(pieces).to_bytes(2, "big") + p for i, p in enumerate(pieces)]


@pytest.fixture
def profile_host(tmp_path):
    """firmware/src/profile.cpp built for this machine (skipped without g++ or
    ArduinoJson, which a PlatformIO build of firmware/ downloads)."""
    include = arduinojson()
    if shutil.which("g++") is None or include is None:
        pytest.skip("needs g++ and ArduinoJson (build firmware/ with PlatformIO once)")
    generated(tmp_path)
    exe = tmp_path / "profile_host"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", f"-I{include}",
                    f"-I{FIRMWARE / 'include'}", f"-I{tmp_path}", str(FIRMWARE / "src" / "profile.cpp"),
                    str(FIRMWARE / "test" / "profile_host.cpp"), "-o", str(exe)], check=True)
    return exe


class Dongle:
    """The compiled profile on the band: the hub's payloads go in as carrier
    fragments (in reverse order, to exercise reassembly), its lines go out."""

    def __init__(self, exe: Path, psk: str, relay: str, name: str):
        self.id = secrets.token_hex(16)
        self.exe, self.name = exe, name
        self.link = TelestheteLink(psk, relay=relay)
        self.sent: list[dict] = []
        self.longest = 0

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(str(self.exe), self.id, self.name, stdin=subprocess.PIPE,
                                                         stdout=subprocess.PIPE)
        self.pump = asyncio.ensure_future(self._pump())
        await self.link.start(self.heard, lambda: self.line("announce"))

    async def stop(self):
        await self.link.stop()
        self.proc.stdin.close()
        await self.proc.wait()
        self.pump.cancel()

    async def line(self, text: str):
        self.proc.stdin.write(text.encode() + b"\n")
        await self.proc.stdin.drain()

    async def heard(self, payload: bytes):
        self.longest = max(self.longest, len(payload))
        for frag in reversed(split(payload)):
            await self.line(frag.hex())

    async def _pump(self):
        while line := await self.proc.stdout.readline():
            self.sent.append(json.loads(line))
            await self.link.send(line.strip())


def test_the_firmware_profile_joins_a_hub(tmp_path, profile_host):
    now = [time.time()]

    async def go():
        relay, _ = await asyncio.get_running_loop().create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
        addr = "127.0.0.1:%d" % relay.get_extra_info("socket").getsockname()[1]
        psk = secrets.token_hex(24)
        shutil.copytree(PLUGIN, tmp_path / "library" / "dongle-s3")
        hub = Hub(Config(data=tmp_path / "hub", library=tmp_path / "library", users={"bake": {"preset": "owner"}},
                         doors={"http": {"bind": "127.0.0.1:0"}},
                         transport={"bands": [{"psk": psk, "relay": addr}]}), clock=lambda: now[0])
        await hub.start()
        token = (hub.data / "owner.token").read_text().strip()
        d = Dongle(profile_host, psk, addr, "dongle-box")

        async def listed():
            got = (await door(hub, token, "/workers"))["workers"]
            return next((x for x in got if x["name"] == "dongle-box"), None)

        async def call(cap, args):
            return await door(hub, token, "/call", {"worker": "dongle-box", "cap": cap, "args": args,
                                                    "why": "Test the dongle firmware's profile."})
        try:
            await d.start()
            me = await until(listed)
            assert (me["id"], me["online"], me["plugins"], me["problem"]) == (d.id, True, ["dongle-s3"], None)
            announce = d.sent[0]
            assert set(announce) == {"kind", "worker_id", "name", "core", "version", "manifests", "manifests_hash"}

            out = await call("dongle-s3.info.ping", {"echo": "hello"})
            assert out["ok"] and out["result"] == {"echo": "hello"}, out
            assert d.longest > 1003         # the hub's call came as two fragments, reassembled
            out = await call("hid.status", {})                      # by the hid.status contract
            assert out["ok"] and out["result"] == {"enabled": True}, out
            out = await call("dongle-s3.hid.set", {"enabled": True})
            assert not out["ok"] and "hid_disabled" in out["error"], out
            assert d.sent[-1] == {"id": d.sent[-1]["id"], "from": d.id, "ok": False, "error": "hid_disabled"}
            assert not any("cap" in m for m in d.sent)              # it never calls anyone

            # Heartbeats keep it online; the hub never fetches the library's manifest.
            for _ in range(3):
                now[0] += 60
                await d.line("heartbeat")
                await until(lambda: _seen(listed, now[0]))
                hub._liveness()
                assert (await listed())["online"] is True
            assert d.sent[-1] == {"kind": "heartbeat", "worker_id": d.id, "core": "2.0",
                                  "manifests_hash": announce["manifests_hash"]}

            # A call it doesn't serve, core.manifests included, and a call to another worker.
            for cap, target in (("core.manifests", d.id), ("dongle-s3.info.ping", secrets.token_hex(16)),
                                ("dongle-s3.nope", d.id)):
                before = len(d.sent)
                await d.heard(json.dumps({"id": "a" * 32, "cap": cap, "target": target, "args": {},
                                          "ticket": {"pad": "x" * 2000}}).encode())
                await d.heard(json.dumps({"id": "b" * 32, "cap": "dongle-s3.info.ping", "target": d.id}).encode())
                replies = await until(lambda: _replies(d, before, 2 if target == d.id else 1))
                if target == d.id:
                    assert replies[0] == {"id": "a" * 32, "from": d.id, "ok": False,
                                          "error": f"unknown capability: {cap}"}
                assert replies[-1] == {"id": "b" * 32, "from": d.id, "ok": True, "result": {}}

        finally:
            await d.stop()
            await hub.stop()
            relay.close()
    asyncio.run(asyncio.wait_for(go(), 60))


def test_reassembly_and_hub_announces(profile_host):
    me = secrets.token_hex(16)

    def call(i: int, size: int) -> bytes:
        return json.dumps({"id": f"{i:032x}", "cap": "dongle-s3.info.ping", "target": me, "args": {"echo": str(i)},
                           "ticket": {"pad": "x" * size}}).encode()

    def hub(node: str) -> bytes:
        return json.dumps({"kind": "announce", "worker_id": node, "name": "rook", "hubkey": "x" * 600}).encode()

    a, b, c = split(call(1, 2500)), split(call(2, 1500)), split(call(3, 8100))
    assert (len(a), len(b), len(c)) == (3, 2, 9)
    lines = [a[2], b[1], a[0], b[0], a[1]]                  # two calls' fragments interleaved
    lines += c                                              # over 8 fragments: dropped
    lines += split(hub("1" * 32)) + split(hub("1" * 32)) + split(hub("2" * 32))
    lines += split(call(4, 10))
    out = subprocess.run([str(profile_host), me, "box"], input="".join(f.hex() + "\n" for f in lines),
                         capture_output=True, text=True, timeout=30, check=True).stdout
    got = [json.loads(line) for line in out.splitlines()]
    assert [m.get("result", {}).get("echo") or m.get("kind") for m in got] == ["2", "1", "announce", "announce", "4"]


async def _seen(listed, t):
    return (await listed())["last_seen"] == t


async def _replies(d: Dongle, before: int, n: int):
    got = [m for m in d.sent[before:] if "ok" in m]
    return got if len(got) >= n else None
