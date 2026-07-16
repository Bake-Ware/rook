"""hid.* — virtual keyboard + mouse on the local machine.

Backend detection per platform:
  Linux   : xdotool (X11) → ydotool / wtype (Wayland) → evdev/uinput
  Windows : SendInput via ctypes (no extra deps)
  Android : ``input`` command via Termux (requires root for many devices)

All capabilities return {"ok": True, ...} or {"ok": False, "error": ...}.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from typing import Any

from ..plugin import Plugin, capability


def _is_termux() -> bool:
    return os.environ.get("PREFIX", "").startswith("/data/data/com.termux")


def _detect_backend() -> str:
    if sys.platform == "win32":
        return "win32"
    if _is_termux():
        return "android"
    if sys.platform.startswith("linux"):
        if shutil.which("xdotool"):
            return "xdotool"
        if shutil.which("ydotool"):
            return "ydotool"
        if shutil.which("wtype"):
            return "wtype"
        try:
            import evdev  # noqa: F401
            return "evdev"
        except ImportError:
            return "none"
    return "none"


async def _run(cmd: list[str], stdin: bytes | None = None,
               timeout: float = 10.0) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise
    return proc.returncode or 0, out, err


def _xdotool_combo(mods: list[str], key: str) -> str:
    """xdotool key spec: mod+mod+key (alt/ctrl/shift/super are accepted as-is)."""
    parts = [m.lower() for m in mods] + [key]
    return "+".join(parts)


# ---------- Windows (ctypes SendInput) -----------------------------------

_WIN_VK: dict[str, int] | None = None


def _win_vk_table() -> dict[str, int]:
    global _WIN_VK
    if _WIN_VK is not None:
        return _WIN_VK
    t = {
        "ctrl": 0x11, "control": 0x11,
        "alt": 0x12, "menu": 0x12,
        "shift": 0x10,
        "win": 0x5B, "super": 0x5B, "meta": 0x5B,
        "enter": 0x0D, "return": 0x0D,
        "esc": 0x1B, "escape": 0x1B,
        "tab": 0x09, "backspace": 0x08,
        "space": 0x20,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pagedown": 0x22,
        "insert": 0x2D, "delete": 0x2E,
    }
    for i in range(1, 13):
        t[f"f{i}"] = 0x6F + i  # F1=0x70
    for c in "abcdefghijklmnopqrstuvwxyz":
        t[c] = ord(c.upper())
    for c in "0123456789":
        t[c] = ord(c)
    _WIN_VK = t
    return t


def _win_send_input_sync(keys: list[tuple[int, bool]] | None = None,
                          text: str | None = None,
                          mouse: dict | None = None) -> dict:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return {"ok": False, "error": "ctypes not available"}

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    SM_CXSCREEN = 0
    SM_CYSCREEN = 1

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    inputs: list[INPUT] = []

    if text is not None:
        for ch in text:
            for flag in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                ki = KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flag,
                                time=0, dwExtraInfo=None)
                inp = INPUT(type=INPUT_KEYBOARD)
                inp.ki = ki
                inputs.append(inp)

    if keys:
        for vk, down in keys:
            flags = 0 if down else KEYEVENTF_KEYUP
            ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags,
                            time=0, dwExtraInfo=None)
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.ki = ki
            inputs.append(inp)

    if mouse:
        kind = mouse.get("kind")
        if kind == "move":
            sx = user32.GetSystemMetrics(SM_CXSCREEN) or 1
            sy = user32.GetSystemMetrics(SM_CYSCREEN) or 1
            ax = int(mouse["x"] * 65535 / max(sx - 1, 1))
            ay = int(mouse["y"] * 65535 / max(sy - 1, 1))
            mi = MOUSEINPUT(dx=ax, dy=ay, mouseData=0,
                            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                            time=0, dwExtraInfo=None)
            inp = INPUT(type=INPUT_MOUSE)
            inp.mi = mi
            inputs.append(inp)
        elif kind == "button":
            btn = mouse.get("button", 1)
            down_flag, up_flag = {
                1: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                2: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
                3: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            }.get(int(btn), (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
            for flag in (down_flag, up_flag) if mouse.get("press_release", True) \
                    else ((down_flag,) if mouse.get("down") else (up_flag,)):
                mi = MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag,
                                time=0, dwExtraInfo=None)
                inp = INPUT(type=INPUT_MOUSE)
                inp.mi = mi
                inputs.append(inp)

    if not inputs:
        return {"ok": True, "sent": 0}

    arr = (INPUT * len(inputs))(*inputs)
    n = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if n != len(inputs):
        return {"ok": False, "error": f"SendInput sent {n}/{len(inputs)}",
                "winerror": ctypes.get_last_error()}
    return {"ok": True, "sent": n}


# ---------- public plugin ---------------------------------------------------

class HidPlugin(Plugin):
    NAMESPACE = "hid"

    def available(self) -> bool:
        if _detect_backend() == "none":
            return False
        # On Linux, HID needs a graphical session just like screenshots — no
        # point injecting keystrokes/mouse on a headless box. Native win/android
        # backends imply a UI.
        if sys.platform.startswith("linux"):
            return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        return True

    def __init__(self) -> None:
        super().__init__()
        self._backend: str | None = None

    def _be(self) -> str:
        if self._backend is None:
            self._backend = _detect_backend()
        return self._backend

    # ---- typing --------------------------------------------------------

    @capability("type")
    async def _type(self, text: str) -> dict:
        """Type ``text`` as if on a real keyboard."""
        be = self._be()
        if be == "xdotool":
            code, _, err = await _run(["xdotool", "type", "--clearmodifiers", "--", text])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "typed": len(text)}
        if be == "ydotool":
            code, _, err = await _run(["ydotool", "type", "--", text])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "typed": len(text)}
        if be == "wtype":
            code, _, err = await _run(["wtype", "--", text])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "typed": len(text)}
        if be == "win32":
            res = await asyncio.to_thread(_win_send_input_sync, None, text, None)
            if res.get("ok"):
                res["typed"] = len(text)
            return res
        if be == "android":
            if not shutil.which("input"):
                return {"ok": False, "error": "android `input` command not available"}
            # `input text` doesn't accept arbitrary chars; spaces must be %s.
            code, _, err = await _run(["input", "text", text.replace(" ", "%s")])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "typed": len(text)}
        return {"ok": False, "error": f"no hid backend (detected: {be})"}

    # ---- key combos ----------------------------------------------------

    @capability("key_combo")
    async def _key_combo(self, key: str, modifiers: list[str] | None = None) -> dict:
        """Press a key with optional modifier list (e.g. ``["ctrl","shift"]``)."""
        mods = list(modifiers or [])
        be = self._be()
        if be == "xdotool":
            combo = _xdotool_combo(mods, key)
            code, _, err = await _run(["xdotool", "key", "--clearmodifiers", combo])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "sent": combo}
        if be == "ydotool":
            combo = "+".join([*[m.lower() for m in mods], key])
            code, _, err = await _run(["ydotool", "key", combo])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "sent": combo}
        if be == "wtype":
            # wtype: -M mod ... <key> -m mod ...
            argv = ["wtype"]
            for m in mods:
                argv += ["-M", m.lower()]
            argv += ["-k", key]
            for m in mods:
                argv += ["-m", m.lower()]
            code, _, err = await _run(argv)
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "sent": "+".join([*mods, key])}
        if be == "win32":
            table = _win_vk_table()
            seq: list[tuple[int, bool]] = []
            try:
                main_vk = table[key.lower()]
                mod_vks = [table[m.lower()] for m in mods]
            except KeyError as e:
                return {"ok": False, "error": f"unknown key name: {e.args[0]}"}
            for vk in mod_vks:
                seq.append((vk, True))
            seq.append((main_vk, True))
            seq.append((main_vk, False))
            for vk in reversed(mod_vks):
                seq.append((vk, False))
            res = await asyncio.to_thread(_win_send_input_sync, seq, None, None)
            if res.get("ok"):
                res["sent"] = "+".join([*mods, key])
            return res
        if be == "android":
            # Android `input` only takes a single keycode; modifiers ignored.
            if not shutil.which("input"):
                return {"ok": False, "error": "android `input` command not available"}
            code, _, err = await _run(["input", "keyevent", key.upper()])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "sent": key, "note": "modifiers ignored on android"}
        return {"ok": False, "error": f"no hid backend (detected: {be})"}

    # ---- mouse ---------------------------------------------------------

    @capability("mouse.move")
    async def _mouse_move(self, x: int, y: int) -> dict:
        """Move pointer to absolute pixel (x, y) — top-left origin."""
        x, y = int(x), int(y)
        be = self._be()
        if be == "xdotool":
            code, _, err = await _run(["xdotool", "mousemove", str(x), str(y)])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "x": x, "y": y}
        if be == "ydotool":
            code, _, err = await _run(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "x": x, "y": y}
        if be == "win32":
            res = await asyncio.to_thread(
                _win_send_input_sync, None, None, {"kind": "move", "x": x, "y": y})
            if res.get("ok"):
                res.update({"x": x, "y": y})
            return res
        if be == "android":
            # No native pointer-move; use a 1-px tap as a poor proxy.
            return {"ok": False, "error": "android does not support raw pointer-move; use mouse.click"}
        return {"ok": False, "error": f"no hid backend (detected: {be})"}

    @capability("mouse.click")
    async def _mouse_click(self, button: int = 1, x: int | None = None,
                           y: int | None = None) -> dict:
        """Click ``button`` (1=left, 2=middle, 3=right). If x/y given, move first."""
        be = self._be()
        if x is not None and y is not None:
            mv = await self._mouse_move(x=x, y=y)
            if not mv.get("ok") and be != "android":
                return mv
        if be == "xdotool":
            code, _, err = await _run(["xdotool", "click", str(int(button))])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "button": int(button)}
        if be == "ydotool":
            # ydotool click codes: 0xC0=left, 0xC1=right, 0xC2=middle (down+up).
            code_map = {1: "0xC0", 2: "0xC2", 3: "0xC1"}
            code, _, err = await _run(["ydotool", "click", code_map.get(int(button), "0xC0")])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "button": int(button)}
        if be == "win32":
            res = await asyncio.to_thread(
                _win_send_input_sync, None, None,
                {"kind": "button", "button": int(button), "press_release": True})
            if res.get("ok"):
                res["button"] = int(button)
            return res
        if be == "android":
            if not shutil.which("input"):
                return {"ok": False, "error": "android `input` command not available"}
            if x is None or y is None:
                return {"ok": False, "error": "android click requires x/y"}
            code, _, err = await _run(["input", "tap", str(int(x)), str(int(y))])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "button": int(button), "x": int(x), "y": int(y)}
        return {"ok": False, "error": f"no hid backend (detected: {be})"}

    @capability("mouse.drag")
    async def _mouse_drag(self, start_x: int, start_y: int,
                          end_x: int, end_y: int, button: int = 1,
                          duration_ms: int = 200) -> dict:
        """Press at (start_x, start_y), move to (end_x, end_y), release."""
        sx, sy, ex, ey = int(start_x), int(start_y), int(end_x), int(end_y)
        be = self._be()
        if be == "android":
            if not shutil.which("input"):
                return {"ok": False, "error": "android `input` command not available"}
            code, _, err = await _run(
                ["input", "swipe", str(sx), str(sy), str(ex), str(ey), str(int(duration_ms))])
            if code != 0:
                return {"ok": False, "error": err.decode(errors="replace").strip()}
            return {"ok": True, "from": [sx, sy], "to": [ex, ey]}
        if be == "xdotool":
            cmds = [
                ["xdotool", "mousemove", str(sx), str(sy)],
                ["xdotool", "mousedown", str(int(button))],
                ["xdotool", "mousemove", str(ex), str(ey)],
                ["xdotool", "mouseup", str(int(button))],
            ]
            for c in cmds:
                code, _, err = await _run(c)
                if code != 0:
                    return {"ok": False, "error": err.decode(errors="replace").strip(),
                            "step": c}
            return {"ok": True, "from": [sx, sy], "to": [ex, ey]}
        if be == "win32":
            try:
                import ctypes
            except ImportError:
                return {"ok": False, "error": "ctypes not available"}
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetCursorPos(sx, sy)
            res = await asyncio.to_thread(
                _win_send_input_sync, None, None,
                {"kind": "button", "button": int(button), "press_release": False, "down": True})
            if not res.get("ok"):
                return res
            await asyncio.sleep(max(duration_ms, 0) / 1000.0)
            user32.SetCursorPos(ex, ey)
            res = await asyncio.to_thread(
                _win_send_input_sync, None, None,
                {"kind": "button", "button": int(button), "press_release": False, "down": False})
            if not res.get("ok"):
                return res
            return {"ok": True, "from": [sx, sy], "to": [ex, ey]}
        return {"ok": False, "error": f"drag not implemented for backend {be}"}

    # ---- diagnostics ---------------------------------------------------

    @capability("backend")
    def _backend_info(self) -> dict[str, Any]:
        """Report which input backend was selected."""
        return {"backend": self._be(), "platform": sys.platform}


PLUGIN = HidPlugin
