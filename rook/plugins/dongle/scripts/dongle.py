"""The Rook RP2040 dongle over its serial port (protocol 1, firmware-rp2040).

One JSON line each way: ``{"cmd": ..., "id": token}`` -> ``{"ok": ..., "id":
token, ...}``. Each call opens the port exclusively (flock, like pyserial's
``exclusive=True``), checks the device's identity with ``status``, sends its
command(s) and, after any HID command, ``release`` so no key or button stays
held. A failed command is never retried. The functions are sync (the host
runs them in a thread); the cap's ``exclusive: dongle`` lock keeps calls on
this worker one at a time, the flock keeps other programs off the port.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import select
import termios
import time
import tty
import uuid
from contextlib import contextmanager
from pathlib import Path

PRODUCT = "Rook RP2040 Zero"
DEVICE, PROTOCOL = "rook-rp2040-zero", 1
REPLY_WAIT = 3.0       # seconds per command
LINE_MAX = 1024        # the firmware's line buffer
REPLY_MAX = 2048

USAGE = {"enter": 40, "escape": 41, "esc": 41, "backspace": 42, "tab": 43, "space": 44,
         "printscreen": 70, "insert": 73, "home": 74, "pageup": 75, "delete": 76, "end": 77,
         "pagedown": 78, "right": 79, "left": 80, "down": 81, "up": 82, "menu": 101,
         **{chr(ord("a") + i): 4 + i for i in range(26)},
         **{str(d): 30 + (d - 1) % 10 for d in range(10)},
         **{f"f{i}": 57 + i for i in range(1, 13)}}
CONSUMER = {"volume_up": 0xE9, "volume_down": 0xEA, "mute": 0xE2, "play_pause": 0xCD,
            "next": 0xB5, "previous": 0xB6, "stop": 0xB7}
MOD_BITS = {"ctrl": 1, "shift": 2, "alt": 4, "meta": 8}
BUTTONS = {"left": 1, "right": 2, "middle": 4}
STEP = 127             # the firmware's per-report mouse range


def status(ctx, probe_display: bool = False):
    with _port(ctx) as port:
        out = port.identity
        if probe_display:
            probe = port.send({"cmd": "display_probe"})
            out = {**out, "oled_connected": probe.get("oled_connected"),
                   "oled_address": probe.get("oled_address")}
    return _clean(out)


def display(ctx, text: str):
    if len(text.encode("utf-8")) > 80:
        raise ctx.Error("text must be at most 80 UTF-8 bytes")
    with _port(ctx) as port:
        return _clean(port.send({"cmd": "display", "text": text}))


def key(ctx, key: str, mods=()):
    if key in CONSUMER:
        if mods:
            raise ctx.Error(f"{key} is a media key: it takes no mods")
        return _hid(ctx, [{"cmd": "consumer", "usage": CONSUMER[key]}])
    if key not in USAGE:
        raise ctx.Error(f"unknown key: {key}")
    bits = 0
    for m in mods:
        bits |= MOD_BITS[m]
    return _hid(ctx, [{"cmd": "keyboard", "keys": [USAGE[key]], "mods": bits}])


def keycodes(ctx, keys=None, mods: int = 0, usage: int | None = None):
    if (keys is None) == (usage is None):
        raise ctx.Error("give keys (keyboard usage codes) or usage (a consumer usage), not both")
    if usage is not None:
        return _hid(ctx, [{"cmd": "consumer", "usage": usage}])
    return _hid(ctx, [{"cmd": "keyboard", "keys": list(keys), "mods": mods}])


def move(ctx, x: int = 0, y: int = 0):
    steps = []
    while x or y:
        dx, dy = max(-STEP, min(STEP, x)), max(-STEP, min(STEP, y))
        steps.append(_mouse(x=dx, y=dy))
        x, y = x - dx, y - dy
    if not steps:
        raise ctx.Error("give x and/or y (relative counts)")
    return _hid(ctx, steps)


def click(ctx, button: str = "left", x: int | None = None, y: int | None = None):
    if x is not None or y is not None:
        raise ctx.Error("the dongle is a relative mouse: it can't click at a pixel; "
                        "move with dongle.hid.move, then click without x and y")
    return _hid(ctx, [_mouse(buttons=BUTTONS[button])])


def scroll(ctx, wheel: int = 0, pan: int = 0):
    if not wheel and not pan:
        raise ctx.Error("give wheel and/or pan")
    return _hid(ctx, [_mouse(wheel=wheel, pan=pan)])


def release(ctx):
    with _port(ctx) as port:
        return _clean(port.send({"cmd": "release"}))


# -- the port -------------------------------------------------------------------

def _mouse(x=0, y=0, buttons=0, wheel=0, pan=0):
    return {"cmd": "mouse", "x": x, "y": y, "buttons": buttons, "wheel": wheel, "pan": pan}


def _hid(ctx, commands):
    with _port(ctx) as port:
        try:
            for c in commands:
                reply = port.send(c)
        finally:   # taps, never a held key or button
            port.send({"cmd": "release"}, check=False)
    return _clean(reply)


def _clean(reply: dict) -> dict:
    return {k: v for k, v in reply.items() if k != "id"}


class _Port:
    def __init__(self, ctx, fd: int):
        self.ctx, self.fd = ctx, fd
        self.identity: dict = {}

    def send(self, payload: dict, check: bool = True) -> dict:
        token = uuid.uuid4().hex
        line = json.dumps({**payload, "id": token}, ensure_ascii=True).encode() + b"\n"
        if len(line) > LINE_MAX:
            raise self.ctx.Error("command is longer than the dongle's line limit")
        deadline = time.monotonic() + REPLY_WAIT
        self._write(line, deadline)
        buf = bytearray()
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                raise self.ctx.Error("the dongle didn't answer in time; the command was not retried")
            chunk = os.read(self.fd, 256)
            if not chunk:
                raise self.ctx.Error("the dongle's port closed")
            buf += chunk
            while b"\n" in buf:
                raw, _, rest = bytes(buf).partition(b"\n")
                buf = bytearray(rest)
                try:
                    reply = json.loads(raw)
                except ValueError:
                    continue   # boot noise or a partial line from before we opened
                if isinstance(reply, dict) and reply.get("id") == token:
                    if check and reply.get("ok") is False:
                        raise self.ctx.Error(f"dongle: {reply.get('error') or 'command refused'}")
                    return reply
            if len(buf) > REPLY_MAX:
                raise self.ctx.Error("the dongle's reply is too long")

    def _write(self, data: bytes, deadline: float) -> None:
        while data:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [self.fd], [], left)[1]:
                raise self.ctx.Error("writing to the dongle timed out")
            data = data[os.write(self.fd, data):]


@contextmanager
def _port(ctx):
    path = ctx.settings["port"]
    if not path:
        raise ctx.Error("dongle.port is not set")
    product = _usb_product(path)
    # Some RP2040 serial bridges type what they receive on another machine:
    # refuse a port whose USB product is known and isn't ours, before writing.
    if product and product != PRODUCT:
        raise ctx.Error(f"{path} is a {product!r}, not a {PRODUCT}; nothing was sent")
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        raise ctx.Error(f"can't open {path}: {e.strerror}", retry=e.errno in (errno.ENOENT, errno.EBUSY))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ctx.Error(f"{path} is in use by another program", retry=True) from None
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
        port = _Port(ctx, fd)
        port.identity = port.send({"cmd": "status"}, check=False)
        if port.identity.get("device") != DEVICE or port.identity.get("protocol") != PROTOCOL:
            raise ctx.Error(f"{path} is not a compatible Rook RP2040 dongle; nothing else was sent")
        yield port
    finally:
        os.close(fd)


def _usb_product(path: str) -> str | None:
    """The USB product name Linux reports for a tty device, if it knows."""
    name = Path(os.path.realpath(path)).name
    for p in (f"/sys/class/tty/{name}/device/../product", f"/sys/class/tty/{name}/device/product"):
        try:
            return Path(p).read_text().strip() or None
        except OSError:
            continue
    return None
