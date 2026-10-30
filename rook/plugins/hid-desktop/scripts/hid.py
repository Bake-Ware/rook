"""The hid-desktop caps config can't say: key names and buttons differ per
input tool, and click/drag are sequences. Linux tools start through core's
launch path; Windows uses SendInput (ctypes, in process).

Tool order matches the config caps' try-chains: xdotool, then wtype
(keyboard only), then ydotool."""

from __future__ import annotations

import asyncio
import shutil
import sys

from rook.core2.worker.security import launch
from rook.kit._types import parse_duration

KEYBOARD = ("xdotool", "wtype", "ydotool")
POINTER = ("xdotool", "ydotool")
MODS = ("ctrl", "shift", "alt", "meta")

# Contract key names -> X keysyms (xdotool, wtype). Unknown names pass
# through as keysyms (the arg pattern keeps them to [A-Za-z0-9_]).
KEYSYM = {"enter": "Return", "return": "Return", "escape": "Escape", "esc": "Escape",
          "tab": "Tab", "backspace": "BackSpace", "space": "space", "delete": "Delete",
          "insert": "Insert", "home": "Home", "end": "End", "pageup": "Page_Up",
          "pagedown": "Page_Down", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
          **{f"f{i}": f"F{i}" for i in range(1, 13)}}
XDO_MOD = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "meta": "super"}
WTYPE_MOD = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "meta": "logo"}

# Linux input event codes (ydotool 1.x takes code:1 / code:0).
EVDEV = {"escape": 1, "esc": 1, "backspace": 14, "tab": 15, "enter": 28, "return": 28,
         "space": 57, "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106,
         "end": 107, "down": 108, "pagedown": 109, "insert": 110, "delete": 111,
         "ctrl": 29, "shift": 42, "alt": 56, "meta": 125,
         **{str(d): 2 + (d - 1) % 10 for d in range(10)},
         **{f"f{i}": 58 + i for i in range(1, 11)}, "f11": 87, "f12": 88}
for _row, _start in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
    EVDEV.update({c: _start + i for i, c in enumerate(_row)})

XDO_BUTTON = {"left": "1", "middle": "2", "right": "3"}
YDO_BUTTON = {"left": 0x00, "right": 0x01, "middle": 0x02}   # | 0x40 down, | 0x80 up

# Windows virtual-key codes.
VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "meta": 0x5B, "enter": 0x0D, "return": 0x0D,
      "escape": 0x1B, "esc": 0x1B, "tab": 0x09, "backspace": 0x08, "space": 0x20,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "delete": 0x2E,
      **{f"f{i}": 0x6F + i for i in range(1, 13)},
      **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"}}


def _tool(ctx, tools, what):
    for name in tools:
        if shutil.which(name):
            return name
    raise ctx.Error(f"no {what} tool on this worker: install {' or '.join(tools)}")


async def _run(ctx, argv, timeout=8.0):
    proc = await launch(argv, getattr(ctx, "lease", None), stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:   # timed out or the call was cancelled: don't leave it pressing keys
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise ctx.Error(f"{argv[0]} failed: {msg}")


async def key(ctx, key: str, mods=()):
    mods = list(dict.fromkeys(mods))
    name = key.lower() if len(key) > 1 else key
    if sys.platform == "win32":
        vks = [VK[m] for m in mods]
        if name.lower() not in VK:
            raise ctx.Error(f"unknown key: {key}")
        vk = VK[name.lower()]
        await asyncio.to_thread(_win_keys, [(v, True) for v in vks] + [(vk, True), (vk, False)]
                                + [(v, False) for v in reversed(vks)])
        return {"key": key, "mods": mods}
    tool = _tool(ctx, KEYBOARD, "keyboard")
    sym = KEYSYM.get(name, key)
    if tool == "xdotool":
        await _run(ctx, ["xdotool", "key", "--clearmodifiers", "+".join([*(XDO_MOD[m] for m in mods), sym])])
    elif tool == "wtype":
        argv = ["wtype"]
        for m in mods:
            argv += ["-M", WTYPE_MOD[m]]
        argv += ["-k", sym]
        for m in reversed(mods):
            argv += ["-m", WTYPE_MOD[m]]
        await _run(ctx, argv)
    else:
        code = EVDEV.get(name.lower())
        if code is None:
            raise ctx.Error(f"ydotool: unknown key {key} (use a contract key name)")
        codes = [EVDEV[m] for m in mods]
        await _run(ctx, ["ydotool", "key", *(f"{c}:1" for c in codes), f"{code}:1", f"{code}:0",
                         *(f"{c}:0" for c in reversed(codes))])
    return {"key": key, "mods": mods, "backend": tool}


async def click(ctx, button: str = "left", x: int | None = None, y: int | None = None):
    if (x is None) != (y is None):
        raise ctx.Error("give x and y together, or neither")
    if sys.platform == "win32":
        await asyncio.to_thread(_win_click, button, x, y)
        return {"button": button, "x": x, "y": y}
    tool = _tool(ctx, POINTER, "pointer")
    if tool == "xdotool":
        move = ["mousemove", str(x), str(y)] if x is not None else []
        await _run(ctx, ["xdotool", *move, "click", XDO_BUTTON[button]])
    else:
        if x is not None:
            await _run(ctx, ["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)])
        await _run(ctx, ["ydotool", "click", f"0x{YDO_BUTTON[button] | 0xC0:02X}"])
    return {"button": button, "x": x, "y": y, "backend": tool}


async def drag(ctx, x1: int, y1: int, x2: int, y2: int, duration="300ms", button: str = "left"):
    secs = parse_duration(duration)
    if sys.platform == "win32":
        await asyncio.to_thread(_win_drag, button, (x1, y1), (x2, y2), secs)
        return {"from": [x1, y1], "to": [x2, y2], "button": button}
    tool = _tool(ctx, POINTER, "pointer")
    if tool == "xdotool":
        b = XDO_BUTTON[button]
        await _run(ctx, ["xdotool", "mousemove", str(x1), str(y1), "mousedown", b, "sleep", f"{secs:g}",
                         "mousemove", str(x2), str(y2), "mouseup", b], timeout=secs + 8)
    else:
        b = YDO_BUTTON[button]
        await _run(ctx, ["ydotool", "mousemove", "--absolute", "-x", str(x1), "-y", str(y1)])
        await _run(ctx, ["ydotool", "click", f"0x{b | 0x40:02X}"])
        try:
            await asyncio.sleep(secs)
            await _run(ctx, ["ydotool", "mousemove", "--absolute", "-x", str(x2), "-y", str(y2)])
        finally:   # never leave the button held
            await _run(ctx, ["ydotool", "click", f"0x{b | 0x80:02X}"])
    return {"from": [x1, y1], "to": [x2, y2], "button": button, "backend": tool}


def status(ctx):
    if sys.platform == "win32":
        return {"backend": "sendinput", "pointer": "sendinput", "platform": sys.platform}
    pick = lambda tools: next((t for t in tools if shutil.which(t)), None)   # noqa: E731
    return {"backend": pick(KEYBOARD), "pointer": pick(POINTER), "platform": sys.platform}


# -- Windows: SendInput ---------------------------------------------------------

def _win_send(events):
    """events: ("key", vk, down) or ("mouse", flags)."""
    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    inputs = []
    for ev in events:
        if ev[0] == "key":
            inp = INPUT(type=1)
            inp.ki = KEYBDINPUT(wVk=ev[1], dwFlags=0 if ev[2] else 0x0002)
        else:
            inp = INPUT(type=0)
            inp.mi = MOUSEINPUT(dwFlags=ev[1])
        inputs.append(inp)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    sent = user32.SendInput(len(inputs), (INPUT * len(inputs))(*inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput sent {sent}/{len(inputs)} (error {ctypes.get_last_error()})")


_WIN_BUTTON = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


def _win_keys(seq):
    _win_send([("key", vk, down) for vk, down in seq])


def _win_move(x, y):
    import ctypes
    if not ctypes.WinDLL("user32").SetCursorPos(int(x), int(y)):
        raise OSError("SetCursorPos failed")


def _win_click(button, x, y):
    if x is not None:
        _win_move(x, y)
    down, up = _WIN_BUTTON[button]
    _win_send([("mouse", down), ("mouse", up)])


def _win_drag(button, start, end, secs):
    import time
    down, up = _WIN_BUTTON[button]
    _win_move(*start)
    _win_send([("mouse", down)])
    try:
        time.sleep(secs)
        _win_move(*end)
    finally:
        _win_send([("mouse", up)])
