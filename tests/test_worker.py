"""Worker edges the lifecycle scenario does not reach."""

import asyncio
import json
import os
import pathlib
import shutil
import stat
import time

import pytest
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey

from memory_link import link_pair
from rook_protocol import decode, encode, validate, verify_link_key
import rook_worker
from rook_worker import Cap, Refused, Session, Worker


async def echo(args):
    return args


async def no_connect(key):
    raise AssertionError("not used")


def test_heartbeat_from_this_machine_is_valid(tmp_path):
    # H1, H4: whatever this machine reports must be a valid heartbeat.
    worker = Worker(tmp_path, "http://unused", no_connect, caps={"demo.echo": Cap("Echo.", {}, echo)})
    worker.health()                       # first CPU sample
    time.sleep(0.2)
    beat = worker.heartbeat()
    validate(beat)
    assert beat["identity"] == {"name": worker.name, "key": worker.key}
    assert set(beat["facts"]) == {"os", "arch", "hostname", "form", "version", "macs"}
    assert beat["health"]["disk"]["total"] > 0
    if os.path.exists("/proc/meminfo"):
        assert {"ram", "uptime", "cpu"} <= set(beat["health"])


def fake_sys(root, files):
    """A stand-in for /sys: each path (relative to it) holding its text."""
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text + "\n")
    return root


def test_heartbeat_reads_battery_temperature_and_gpu_where_linux_exposes_them(tmp_path, monkeypatch):
    # H4: read, never set. Two batteries and a mouse's; three sensors, one unreadable; an AMD GPU and,
    # through nvidia-smi, an NVIDIA one; a display output of the card, which is not a GPU of its own.
    monkeypatch.setattr(rook_worker, "SYS", fake_sys(tmp_path / "sys", {
        "class/power_supply/BAT0/type": "Battery", "class/power_supply/BAT0/capacity": "80",
        "class/power_supply/BAT1/type": "Battery", "class/power_supply/BAT1/capacity": "61",
        "class/power_supply/hidpp_battery_0/type": "Battery", "class/power_supply/hidpp_battery_0/scope": "Device",
        "class/power_supply/hidpp_battery_0/capacity": "5",
        "class/power_supply/AC/type": "Mains", "class/power_supply/AC/online": "1",
        "class/hwmon/hwmon0/temp1_input": "45850", "class/hwmon/hwmon1/temp1_input": "68000",
        "class/hwmon/hwmon1/temp2_input": "not a number",
        "class/drm/card0/device/gpu_busy_percent": "30",
        "class/drm/card0/device/mem_info_vram_total": str(8 << 30),
        "class/drm/card0/device/mem_info_vram_used": str(2 << 30),
        "class/drm/card0-DP-1/device/gpu_busy_percent": "99",
    }))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nvidia-smi").write_text("#!/bin/sh\necho '50, 8192, 4096'\n")
    (bin_dir / "nvidia-smi").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    health = Worker(tmp_path / "state", "http://unused", no_connect, macs=[]).heartbeat()["health"]
    assert health["battery"] == 70.5                         # the mean of the machine's two batteries
    assert health["temperature"] == 68.0                     # the hottest sensor
    assert health["gpu"] == 40.0                             # the mean of the two GPUs
    assert health["vram"] == {"total": (8 << 30) + (8192 << 20), "free": (6 << 30) + (4096 << 20)}
    validate(Worker(tmp_path / "state", "http://unused", no_connect, macs=[]).heartbeat())


def test_a_slow_reading_never_holds_up_a_heartbeat(tmp_path, monkeypatch):
    # H4 on a running worker: battery, temperature and GPU are read on a thread of their own and a heartbeat
    # carries the latest reading. Read on the event loop at every heartbeat, a few milliseconds each (a Wi-Fi
    # card's sensor, nvidia-smi) held up the link itself: test_sizes_and_order_over_the_real_carrier never ended.
    monkeypatch.setattr(rook_worker, "SYS", tmp_path / "no-sys")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nvidia-smi").write_text("#!/bin/sh\n/bin/sleep 1\necho '50, 8192, 4096'\n")
    (bin_dir / "nvidia-smi").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    worker = Worker(tmp_path / "state", "http://unused", no_connect, macs=[])

    async def go():
        started = time.monotonic()
        first = worker.heartbeat()["health"]
        assert time.monotonic() - started < 0.5 and "gpu" not in first      # no reading yet: not waited for
        while "gpu" not in worker.heartbeat()["health"]:
            await asyncio.sleep(0.05)
        assert worker.heartbeat()["health"]["gpu"] == 50.0
    asyncio.run(go())


def test_heartbeat_leaves_out_what_the_machine_does_not_expose(tmp_path, monkeypatch):
    # H4: no battery, no sensor, no GPU, and values that cannot be read: those fields are absent, nothing fails.
    monkeypatch.setattr(rook_worker, "SYS", fake_sys(tmp_path / "sys", {
        "class/power_supply/BAT0/type": "Battery", "class/power_supply/BAT0/capacity": "unknown",
        "class/power_supply/ucsi-source-psy-7-00081/type": "USB",
        "class/hwmon/hwmon2/name": "asus",
        "class/drm/card1/device/vendor": "0x10de",
    }))
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    beat = Worker(tmp_path / "state", "http://unused", no_connect, macs=[]).heartbeat()
    validate(beat)
    assert not {"battery", "temperature", "gpu", "vram"} & set(beat["health"])
    monkeypatch.setattr(rook_worker, "SYS", tmp_path / "no-sys")       # no /sys at all
    assert not {"battery", "temperature", "gpu", "vram"} & set(Worker(tmp_path / "state", "http://unused",
                                                                      no_connect, macs=[]).health())


def test_this_machine_reports_what_it_exposes(tmp_path):
    # H4 on the machine the tests run on (on cachyrig: k10temp and an NVIDIA GPU, no battery).
    health = Worker(tmp_path, "http://unused", no_connect, macs=[]).health()
    sys_dir = pathlib.Path("/sys/class")
    def readable(path):
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            return None
    assert ("temperature" in health) == any(readable(p) is not None for p in sys_dir.glob("hwmon/hwmon*/temp*_input"))
    if shutil.which("nvidia-smi") or any(sys_dir.glob("drm/card*/device/gpu_busy_percent")):
        assert "gpu" in health
    print("health on this machine:", json.dumps(health))


def test_key_is_private_to_the_worker(tmp_path):
    Worker(tmp_path, "http://unused", no_connect)
    assert stat.S_IMODE(os.stat(tmp_path / "key").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "link_key").st_mode) == 0o600


def test_knock_carries_the_link_key_signed_by_the_identity_key(tmp_path):
    # docs/link-design.md 2.3: the knock's link_key, signed by the identity key; kept across restarts (R5, W7).
    sent = []

    def post(body):
        sent.append(decode(body))
        return {"type": "error", "code": "unknown_key", "message": "stop here", "next": "nothing"}

    for _ in range(2):                                    # a second start reuses the same keys
        worker = Worker(tmp_path, "http://unused", no_connect)
        worker._post = post
        with pytest.raises(Refused):
            asyncio.run(worker.run())
    first, second = sent
    assert first["link_key"] == worker.link_secret.public_key.encode().hex() and verify_link_key(first)
    assert (second["key"], second["link_key"]) == (first["key"], first["link_key"])


def test_worker_answers_a_cap_it_does_not_serve(tmp_path):
    # W5: an unservable call fails with a reason and a next step.
    async def go():
        hub_end, worker_end = link_pair("k")

        async def connect(session):
            return worker_end

        async def open_session():          # no hub here: a session as an exchange would give it
            return Session(worker.key, "0" * 64, "ws://unused/band")

        worker = Worker(tmp_path, "http://unused", connect, caps={"demo.echo": Cap("Echo.", {}, echo)})
        worker.identity = {"guid": "g", "moniker": "m", "signature": "s"}
        worker._open_session = open_session
        running = asyncio.create_task(worker.run())
        assert decode(await hub_end.recv())["type"] == "heartbeat"
        await hub_end.send(encode({"type": "call", "id": "c1", "cap": "video.capture", "args": {}}))
        reply = decode(await hub_end.recv())
        assert reply["id"] == "c1" and reply["code"] == "unknown_cap" and reply["next"]
        await hub_end.send(encode({"type": "call", "id": "c2", "cap": "demo.echo", "args": {"a": 1}}))
        assert decode(await hub_end.recv()) == {"type": "result", "id": "c2", "result": {"a": 1}}
        worker.stop()
        await running
    asyncio.run(go())


def test_worker_keeps_no_identity_whose_signature_does_not_verify(tmp_path):
    # R5: the worker checks the hub's signature with hub_key before persisting.
    worker = Worker(tmp_path, "http://unused", no_connect)
    other = SigningKey.generate()
    forged = other.sign(b"rook-worker-key:" + bytes.fromhex(worker.key)).signature.hex()
    hub_key = SigningKey.generate().verify_key.encode().hex()   # not the key that signed
    worker._post = lambda body: {"type": "knock_reply", "protocol": 1, "status": "approved",
                                 "guid": "g", "moniker": "a-b-c", "signature": forged, "hub_key": hub_key}
    with pytest.raises(BadSignatureError):
        asyncio.run(worker.run())
    assert worker.identity is None and not (tmp_path / "identity.json").exists()

    real_key = other.verify_key.encode().hex()
    worker._post = lambda body: {"type": "knock_reply", "protocol": 1, "status": "approved",
                                 "guid": "g", "moniker": "a-b-c", "signature": forged, "hub_key": real_key}

    async def open_session():
        raise ConnectionError("stop after the identity is stored")
    worker._open_session = open_session
    with pytest.raises(ConnectionError):
        asyncio.run(worker.run())
    assert json.loads((tmp_path / "identity.json").read_text())["hub_key"] == real_key
