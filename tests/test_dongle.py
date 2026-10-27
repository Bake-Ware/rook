import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from rook.core.pluginkit import ArgError, CapError

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
    with pytest.raises(CapError, match="not a compatible"):
        asyncio.run(dongle.DonglePlugin().keyboard([4]))
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
    with pytest.raises(CapError, match="disconnected"):
        asyncio.run(dongle.DonglePlugin().keyboard([4]))
    assert [c["cmd"] for c in port.commands] == ["status", "keyboard", "release"]


def test_display_byte_limit():
    with pytest.raises(ArgError):
        asyncio.run(dongle.DonglePlugin().display("é" * 41))


def test_port_comes_from_settings_not_only_env(monkeypatch):
    """A port set in the settings UI (stored value) is used, and gates loading."""
    port = setup_port(monkeypatch)
    monkeypatch.delenv("ROOK_DONGLE_PORT")
    seen = []
    monkeypatch.setitem(sys.modules, "serial",
                        SimpleNamespace(Serial=lambda dev, *a, **k: seen.append(dev) or port))
    from rook.core.plugin import SettingsView
    p = dongle.DonglePlugin()
    assert not p.available()
    p.__dict__["_settings"] = SettingsView(p, {"port": "/dev/ttyACM7"})
    assert p.available()
    asyncio.run(p.status())
    assert seen == ["/dev/ttyACM7"]


def test_refuse_a_known_other_usb_product_before_writing(monkeypatch):
    """A twin-Pico HID bridge shares the RP2040 VID:PID; even `status` would be typed."""
    port = setup_port(monkeypatch)
    listed = [SimpleNamespace(device="/test/rook", product="HID Bridge")]
    tools = SimpleNamespace(list_ports=SimpleNamespace(comports=lambda: listed))
    monkeypatch.setitem(sys.modules, "serial.tools", tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", tools.list_ports)
    with pytest.raises(CapError, match="nothing was sent"):
        asyncio.run(dongle.DonglePlugin().status())
    assert port.commands == []
    listed[0].product = "Rook RP2040 Zero"
    assert asyncio.run(dongle.DonglePlugin().status())["device"] == "rook-rp2040-zero"
