"""dongle through the worker host, against a fake Rook RP2040 on a pty pair
(a thread on the master side answers the firmware's JSON-line protocol)."""

import asyncio
import fcntl
import json
import os
import select
import sys
import threading
import time
import tty

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX serial (pty) only")


class FakeDongle:
    """Answers like firmware-rp2040: ``{"ok": true, "id": ...}`` per line."""

    def __init__(self, device="rook-rp2040-zero", protocol=1, delay=0.0, refuse=(), silent=()):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.device, self.protocol, self.delay = device, protocol, delay
        self.refuse, self.silent = set(refuse), set(silent)
        self.seen: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def cmds(self):
        return [c["cmd"] for c in self.seen]

    def _serve(self):
        buf = b""
        while not self._stop.is_set():
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            try:
                buf += os.read(self.master, 4096)
            except OSError:
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                req = json.loads(line)
                self.seen.append(req)
                if req["cmd"] in self.silent:
                    continue
                reply = {"id": req["id"], "ok": req["cmd"] not in self.refuse}
                if req["cmd"] == "status":
                    reply.update(device=self.device, protocol=self.protocol, version="0.1.0",
                                 usb_mounted=True, oled_connected=False)
                elif req["cmd"] == "display_probe":
                    reply.update(oled_connected=True, oled_address=60)
                if not reply["ok"]:
                    reply["error"] = "invalid keyboard report"
                if req["cmd"] in ("keyboard", "mouse") and self.delay:
                    time.sleep(self.delay)
                os.write(self.master, b"noise\n" + json.dumps(reply).encode() + b"\n")

    def close(self):
        self._stop.set()
        self._thread.join(2)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def dongle():
    d = FakeDongle()
    yield d
    d.close()


def calls(port, *calls, timeout=20):
    """Run (cap, args) calls on one host, concurrently; each result or CallError."""
    async def body():
        host = Host(os_name="linux")
        await host.set_settings("dongle", local={"port": port})
        await host.load(PLUGINS / "dongle")
        try:
            return await asyncio.gather(*(host.call(f"dongle.{c}", a, ME) for c, a in calls),
                                        return_exceptions=True)
        finally:
            await host.close()
    return go(body, timeout)


def one(port, cap, args=None):
    (res,) = calls(port, (cap, args or {}))
    if isinstance(res, BaseException):
        raise res
    return res


def test_status_checks_identity_first(dongle):
    out = one(dongle.path, "status")
    assert out["device"] == "rook-rp2040-zero" and "id" not in out
    assert dongle.cmds() == ["status"]
    out = one(dongle.path, "status", {"probe_display": True})
    assert out["oled_connected"] is True and out["oled_address"] == 60
    assert dongle.cmds() == ["status", "status", "display_probe"]


def test_key_maps_names_and_always_releases(dongle):
    one(dongle.path, "hid.key", {"key": "c", "mods": ["ctrl", "shift"]})
    one(dongle.path, "hid.key", {"key": "enter"})
    one(dongle.path, "hid.key", {"key": "volume_up"})
    assert dongle.cmds() == ["status", "keyboard", "release"] * 2 + ["status", "consumer", "release"]
    assert dongle.seen[1]["keys"] == [6] and dongle.seen[1]["mods"] == 3
    assert dongle.seen[4]["keys"] == [40] and dongle.seen[4]["mods"] == 0
    assert dongle.seen[7]["usage"] == 0xE9
    with pytest.raises(CallError, match="media key: it takes no mods"):
        one(dongle.path, "hid.key", {"key": "mute", "mods": ["ctrl"]})
    with pytest.raises(CallError, match="unknown key: printer"):
        one(dongle.path, "hid.key", {"key": "printer"})


def test_keycodes(dongle):
    one(dongle.path, "keycodes", {"keys": [4, 5], "mods": 2})
    one(dongle.path, "keycodes", {"usage": 0xCD})
    assert dongle.seen[1] | {"id": 0} == {"cmd": "keyboard", "keys": [4, 5], "mods": 2, "id": 0}
    assert dongle.seen[4]["usage"] == 0xCD
    with pytest.raises(CallError, match="not both"):
        one(dongle.path, "keycodes", {"keys": [4], "usage": 1})
    with pytest.raises(CallError, match="bad args"):
        one(dongle.path, "keycodes", {"keys": [1, 2, 3, 4, 5, 6, 7]})


def test_move_splits_into_steps(dongle):
    one(dongle.path, "hid.move", {"x": 300, "y": -10})
    moves = [(c["x"], c["y"]) for c in dongle.seen if c["cmd"] == "mouse"]
    assert moves == [(127, -10), (127, 0), (46, 0)]
    assert dongle.cmds()[-1] == "release"


def test_click_scroll_release(dongle):
    one(dongle.path, "hid.click", {"button": "right"})
    one(dongle.path, "hid.scroll", {"wheel": -3})
    one(dongle.path, "hid.release")
    mice = [c for c in dongle.seen if c["cmd"] == "mouse"]
    assert mice[0]["buttons"] == 2 and mice[1]["wheel"] == -3
    assert dongle.cmds()[-2:] == ["status", "release"]


def test_click_at_a_pixel_is_refused_before_sending(dongle):
    with pytest.raises(CallError, match="relative mouse"):
        one(dongle.path, "hid.click", {"x": 10, "y": 10})
    assert dongle.seen == []


def test_display(dongle):
    one(dongle.path, "display", {"text": "héllo"})
    assert dongle.seen[1]["text"] == "héllo"
    with pytest.raises(CallError, match="80 UTF-8 bytes"):
        one(dongle.path, "display", {"text": "é" * 41})


def test_wrong_device_gets_nothing_but_status():
    d = FakeDongle(device="some-other-board")
    try:
        with pytest.raises(CallError, match="not a compatible Rook RP2040"):
            one(d.path, "hid.key", {"key": "a"})
        assert d.cmds() == ["status"]
    finally:
        d.close()


def test_refused_command_is_an_error_and_still_releases():
    d = FakeDongle(refuse={"keyboard"})
    try:
        with pytest.raises(CallError, match="dongle: invalid keyboard report"):
            one(d.path, "keycodes", {"keys": [4]})
        assert d.cmds() == ["status", "keyboard", "release"]
    finally:
        d.close()


def test_no_reply_is_not_retried():
    d = FakeDongle(silent={"keyboard"})
    try:
        with pytest.raises(CallError, match="didn't answer in time; the command was not retried"):
            one(d.path, "hid.key", {"key": "a"})
        assert d.cmds().count("keyboard") == 1
    finally:
        d.close()


def test_calls_take_turns():
    # Each keypress holds the port ~0.3 s; without exclusive: dongle the second
    # call would find the port flocked and fail with "in use".
    d = FakeDongle(delay=0.3)
    try:
        res = calls(d.path, ("hid.key", {"key": "a"}), ("hid.key", {"key": "b"}))
        assert not any(isinstance(r, BaseException) for r in res), res
        assert d.cmds() == ["status", "keyboard", "release"] * 2
    finally:
        d.close()


def test_another_program_holding_the_port(dongle):
    fd = os.open(dongle.path, os.O_RDWR | os.O_NOCTTY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(CallError, match="in use by another program"):
            one(dongle.path, "status")
    finally:
        os.close(fd)
    assert dongle.seen == []


def test_missing_port_is_a_clean_error(tmp_path):
    with pytest.raises(CallError, match="can't open"):
        one(str(tmp_path / "ttyACM9"), "status")


def test_off_without_a_port():
    async def body():
        host = Host(os_name="linux")
        p = await host.load(PLUGINS / "dongle")
        try:
            assert set(p.unavailable) == set(p.manifest["caps"])
        finally:
            await host.close()
    go(body)
