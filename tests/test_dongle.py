import asyncio
import json
import sys
from types import SimpleNamespace

from rook.worker.plugins import dongle


class Port:
    def __init__(self, device="rook-rp2040-zero"):
        self.device = device
        self.commands = []
        self.buffer = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def write(self, data):
        command = json.loads(data)
        self.commands.append(command)
        reply = {"id": command["id"], "ok": True, "protocol": 1, "device": self.device}
        self.buffer.extend(json.dumps(reply).encode() + b"\n")

    def read(self, _):
        result = self.buffer[:1]
        del self.buffer[:1]
        return result


def setup_port(monkeypatch, device="rook-rp2040-zero"):
    port = Port(device)
    monkeypatch.setenv("ROOK_DONGLE_PORT", "/test/rook")
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *a, **k: port))
    return port


def test_identify_before_hid_and_always_release(monkeypatch):
    port = setup_port(monkeypatch)
    result = dongle._exchange({"cmd": "keyboard", "keys": [4], "mods": 0})
    assert result["ok"]
    assert [c["cmd"] for c in port.commands] == ["status", "keyboard", "release"]


def test_refuse_wrong_device(monkeypatch):
    port = setup_port(monkeypatch, "unrelated-device")
    result = asyncio.run(dongle.DonglePlugin().keyboard([4]))
    assert not result["ok"]
    assert [c["cmd"] for c in port.commands] == ["status"]


def test_failed_action_not_retried_but_releases(monkeypatch):
    port = setup_port(monkeypatch)
    write = port.write

    def fail_action(data):
        command = json.loads(data)
        if command["cmd"] == "keyboard":
            port.commands.append(command)
            raise OSError("disconnected")
        write(data)

    port.write = fail_action
    result = asyncio.run(dongle.DonglePlugin().keyboard([4]))
    assert not result["ok"]
    assert [c["cmd"] for c in port.commands] == ["status", "keyboard", "release"]


def test_display_byte_limit():
    result = asyncio.run(dongle.DonglePlugin().display("é" * 41))
    assert not result["ok"]
