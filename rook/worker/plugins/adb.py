"""adb.* — Android phones and tablets cabled to this worker, as device workers.

This worker becomes the *delegate* for the Android devices listed in
``adb.devices``: each shows on the band as a worker of its own (``rook_workers``
lists ``playground-tablet`` with ``delegate`` naming this worker) and answers
with the same cap names the Rook Android app uses, so an agent drives it the
way it drives any phone: ``shell.exec``, ``screenshot.capture``, ``ui.text``,
``hid.type``, ``hid.key_combo``, ``hid.mouse.click``, ``hid.mouse.drag``,
``device.info`` and ``battery.status``. Nothing is installed on the device:
every call is the Android Debug Bridge (``adb``) over the USB cable, so this is
how a tablet with a broken screen becomes a headless playground. See
``docs/design/dongle-shell.md``.

Safety:
- Only the serials listed in ``adb.devices`` are ever addressed (``adb -s``),
  never "whatever is plugged in": a phone cabled up for charging or debugging
  stays off the band.
- Rook never authorizes itself. A device that has not allowed USB debugging
  from this computer reports ``unauthorized``; a person taps Allow on it once.
- Nothing here runs ``adb root``, ``tcpip``, ``reboot``, ``install`` or
  ``kill-server``. ``shell.exec`` runs as Android's ``shell`` user, which
  cannot read other apps' private data but can read and delete shared storage
  (photos, downloads), change settings and uninstall apps for the current
  user: it is ``exec`` risk, like ``shell.exec`` anywhere.

Config (settings, or env):
    ROOK_ADB_DEVICES   ``name=serial`` pairs, comma-separated, e.g.
                       ``playground-tablet=R52N30ABCDE``. Serials come from
                       ``adb devices`` or ``adb.scan``. Blank leaves adb.* off.
    ROOK_ADB_PATH      The adb binary; blank finds ``adb`` on PATH.

And on this worker: ``adb.list`` (every configured device and its state) and
``adb.scan`` (every device adb sees, to fill in ``adb.devices``).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shlex
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any

from ...core.devices import Device, valid_device_name
from ...core.plugin import cap_methods
from ...core.pluginkit import (ArgError, RunTimeout, fail, no_window, opt_int, require_int,
                               require_str, run, which)
from ..plugin import Plugin, capability, setting

log = logging.getLogger("rook.worker.adb")

POLL_SECS = 30.0
OUTPUT_CAP = 1 << 20        # per stream; a band reply is not a file transfer
_SERIAL = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
# What adb itself prints when the device goes away mid-command.
_GONE = re.compile(r"^(adb: |error: )(device .*(not found|offline)|no devices)", re.M)
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Friendly names for hid.key_combo, mapped to Android key codes.
KEYS = {
    "home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "recents": "KEYCODE_APP_SWITCH",
    "app_switch": "KEYCODE_APP_SWITCH", "menu": "KEYCODE_MENU", "power": "KEYCODE_POWER",
    "wake": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP", "enter": "KEYCODE_ENTER",
    "tab": "KEYCODE_TAB", "escape": "KEYCODE_ESCAPE", "esc": "KEYCODE_ESCAPE",
    "space": "KEYCODE_SPACE", "backspace": "KEYCODE_DEL", "delete": "KEYCODE_FORWARD_DEL",
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN", "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT", "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE",
    "notifications": "KEYCODE_NOTIFICATION", "search": "KEYCODE_SEARCH",
    "ctrl": "KEYCODE_CTRL_LEFT", "alt": "KEYCODE_ALT_LEFT", "shift": "KEYCODE_SHIFT_LEFT",
    "meta": "KEYCODE_META_LEFT",
}


def parse_devices(spec: str) -> list[tuple[str, str]]:
    """``"playground-tablet=R52N30ABCDE, emu=emulator-5554"`` ->
    ``[(name, serial), …]``. Malformed entries raise ValueError."""
    out = []
    for part in (spec or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, serial = part.partition("=")
        name, serial = name.strip(), serial.strip()
        if not sep or not valid_device_name(name):
            raise ValueError(f"bad device entry {part!r}: expected name=serial")
        if not _SERIAL.match(serial):
            raise ValueError(f"bad serial in {part!r}")
        out.append((name, serial))
    for i, what in ((0, "names"), (1, "serials")):
        seen = [e[i].lower() for e in out]
        if len(seen) != len(set(seen)):
            raise ValueError(f"device {what} must be unique")
    return out


def parse_adb_devices(text: str) -> dict[str, dict]:
    """``adb devices -l`` output -> ``{serial: {state, model?}}``."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or line.startswith(("List of", "*")):
            continue
        row: dict[str, Any] = {"state": parts[1]}
        for extra in parts[2:]:
            k, _, v = extra.partition(":")
            if k == "model" and v:
                row["model"] = v
        out[parts[0]] = row
    return out


def ui_items(xml: str) -> list[dict]:
    """The text a person would read on screen, from a ``uiautomator dump``:
    ``[{text, x, y, clickable?}]`` top to bottom, where x/y is the centre of
    the element (tap it with ``hid.mouse.click``)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        fail("the screen dump was not readable; try again")
    items = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip() or (node.get("content-desc") or "").strip()
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", node.get("bounds") or "")
        if not text or not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        item: dict[str, Any] = {"text": text, "x": (x1 + x2) // 2, "y": (y1 + y2) // 2}
        if node.get("clickable") == "true":
            item["clickable"] = True
        items.append(item)
    items.sort(key=lambda i: (i["y"], i["x"]))
    return items


class AndroidDevice(Device):
    """One Android device on this worker's USB, driven over adb."""

    NAMESPACE = "android"
    TYPE = "android"
    DRIVER = "adb"

    def __init__(self, plugin: "AdbPlugin", name: str, serial: str) -> None:
        super().__init__(key=serial.lower(), name=name,
                         description=f"Android device {serial} over adb")
        self.plugin = plugin
        self.serial = serial
        self.state: dict[str, Any] = {"adb": "missing"}
        # One input sequence at a time, so two callers' typing never interleaves.
        self._input_lock = asyncio.Lock()

    # -- device protocol ---------------------------------------------------
    # Caps use the Rook Android app's names (shell.exec, hid.type…), which
    # span several namespaces, so each @capability suffix is the full name.
    def caps(self):
        return {k.lstrip("."): v for k, v in cap_methods(self, "").items()}

    @classmethod
    def declared_caps(cls):
        return {k.lstrip("."): v for k, v in cap_methods(cls, "").items()}

    def info(self) -> dict:
        out = super().info()
        if self.state.get("model"):
            out["model"] = self.state["model"]
        return out

    def heartbeat(self) -> dict | None:
        s = self.state
        hb: dict[str, Any] = {"on": s.get("adb") == "device"}
        if s.get("adb") == "unauthorized":
            hb["paired"] = False
        if s.get("adb") != "device":
            hb["adb"] = s.get("adb")
        if s.get("battery") is not None:
            hb["battery"] = s["battery"]
        return hb

    # -- adb ---------------------------------------------------------------
    async def adb(self, *args: str, timeout: float = 15.0, stdin: str | None = None):
        return await self.plugin.adb("-s", self.serial, *args, timeout=timeout, stdin=stdin)

    async def ready(self) -> None:
        """Fail plainly unless the device is attached and authorized now."""
        code, out, err = await self.adb("get-state", timeout=8)
        state = out.strip() if code == 0 else ""
        if not state:
            low = err.lower()
            state = ("unauthorized" if "unauthorized" in low else
                     "offline" if "offline" in low else "missing")
        self.state["adb"] = state
        if state == "device":
            return
        if state == "unauthorized":
            fail(f"{self.name} has not allowed USB debugging from this computer: someone "
                 "has to tap Allow on it once (see the USB dongle manual page)")
        if state == "missing":
            fail(f"{self.name} ({self.serial}) is not connected to this worker")
        fail(f"{self.name} is {state} on adb")

    async def sh(self, remote: str, timeout: float = 15.0) -> str:
        """Run a fixed helper command; its stdout, or a failure with stderr."""
        await self.ready()
        code, out, err = await self.adb("shell", "-T", "--", remote, timeout=timeout)
        if code != 0:
            fail((err or out).strip()[:300] or f"{remote.split()[0]} failed (exit {code})")
        return out

    async def refresh(self) -> None:
        """Battery and model for the heartbeat; called by the plugin's poll."""
        try:
            out = await self.sh("getprop ro.product.model; dumpsys battery | grep -m1 ' level:'",
                                timeout=10)
        except Exception:
            return
        lines = out.splitlines()
        if lines and lines[0].strip():
            self.state["model"] = lines[0].strip()[:64]
        m = re.search(r"level:\s*(\d+)", out)
        if m:
            self.state["battery"] = int(m.group(1))

    # -- caps ----------------------------------------------------------------
    @capability("shell.exec", risk="exec", scope="multi-machine")
    async def shell_exec(self, cmd: str | None = None, argv: list | None = None,
                         stdin: str | None = None, timeout: float = 30.0,
                         cwd: str | None = None, env: dict | None = None,
                         shell: str | None = None) -> dict:
        """Run a command on the device as Android's shell user. Returns code/stdout/stderr.

        Pass EITHER ``argv`` (a list, quoted for you) or ``cmd`` (a string for
        the device's /system/bin/sh, with pipes and redirection). ``stdin`` is
        fed to the command; ``cwd`` and ``env`` apply on the device. A command
        that prints more than 1 MiB to a stream is stopped there
        (``truncated``), and one still running at ``timeout`` is ended on the
        device too.
        """
        if argv and cmd:
            raise ArgError("pass argv or cmd, not both")
        if not argv and not cmd:
            raise ArgError("argv or cmd required")
        if shell not in (None, "", "sh"):
            raise ArgError("the device has sh only")
        if argv:
            if not isinstance(argv, list) or not all(isinstance(a, (str, int, float)) for a in argv):
                raise ArgError("argv must be a list of strings")
            remote = shlex.join(str(a) for a in argv)
        else:
            remote = require_str(cmd, "cmd", strip=False)
        prefix = []
        for k, v in (env or {}).items():
            if not _ENV_KEY.match(str(k)):
                raise ArgError(f"bad environment name {k!r}")
            prefix.append(f"export {k}={shlex.quote(str(v))}")
        if cwd:
            prefix.append(f"cd {shlex.quote(require_str(cwd, 'cwd'))} || exit 1")
        if prefix:
            remote = "\n".join(prefix + [remote])
        t = max(1.0, min(float(timeout), 600.0))
        await self.ready()
        try:
            # Killing the local adb ends the device-side command too (shell v2).
            code, out, err, cut = await self.plugin.adb_bytes(
                "-s", self.serial, "shell", "-T", "--", remote, timeout=t, stdin=stdin,
                cap=OUTPUT_CAP)
        except RunTimeout:
            return {"ok": False, "error": "timeout", "timeout": t,
                    "note": "ended after timeout; partial side effects may have landed."}
        stderr = err.decode("utf-8", errors="replace")
        if code == 1 and _GONE.search(stderr):
            # adb's own failure, not the command's exit 1: the cable went.
            self.state["adb"] = "missing"
            return {"ok": False, "error": "disconnected",
                    "note": f"{self.name} left adb while the command ran; "
                            "partial side effects may have landed.", "stderr": stderr}
        reply: dict[str, Any] = {"ok": code == 0 and not cut, "code": code,
                                 "stdout": out.decode("utf-8", errors="replace"), "stderr": stderr}
        if cut:
            reply.update(code=None, truncated=True,
                         note="stopped at the 1 MiB output limit; redirect big output to a file")
        return reply

    @capability("device.info", risk="read", scope="multi-machine")
    async def device_info(self) -> dict:
        """Model, Android version, screen size, whether it is awake, uptime, adb state."""
        out = await self.sh(
            "getprop ro.product.manufacturer; getprop ro.product.model; "
            "getprop ro.build.version.release; getprop ro.build.version.sdk; "
            "wm size | tail -n1; dumpsys power | grep -m1 mWakefulness=; cat /proc/uptime")
        lines = (out.splitlines() + [""] * 7)[:7]
        size = re.search(r"(\d+)x(\d+)", lines[4])
        info: dict[str, Any] = {
            "name": self.name, "serial": self.serial, "adb": self.state.get("adb"),
            "manufacturer": lines[0].strip(), "model": lines[1].strip(),
            "android": lines[2].strip(), "sdk": int(lines[3]) if lines[3].strip().isdigit() else None,
            "awake": "Awake" in lines[5],
        }
        if size:
            info["screen"] = {"width": int(size.group(1)), "height": int(size.group(2))}
        if lines[6].split():
            info["uptime_s"] = int(float(lines[6].split()[0]))
        if info["model"]:
            self.state["model"] = info["model"][:64]
        return info

    @capability("battery.status", risk="read", scope="multi-machine")
    async def battery_status(self) -> dict:
        """Current battery: ``{percent, charging, plugged, status, temp_c?}``."""
        out = await self.sh("dumpsys battery")
        f = dict(re.findall(r"^[ \t]*([A-Za-z ]+):[ \t]*(.+?)[ \t]*$", out, re.M))
        status = {"2": "charging", "3": "discharging", "4": "not charging", "5": "full"}
        plugged = [k.split()[0].lower() for k in ("AC powered", "USB powered", "Wireless powered")
                   if f.get(k) == "true"]
        level = int(f["level"]) if f.get("level", "").isdigit() else None
        reply: dict[str, Any] = {"percent": level, "charging": f.get("status") == "2",
                                 "plugged": plugged[0] if plugged else None,
                                 "status": status.get(f.get("status", ""), "unknown")}
        if f.get("temperature", "").lstrip("-").isdigit():
            reply["temp_c"] = int(f["temperature"]) / 10
        if level is not None:
            self.state["battery"] = level
        return reply

    @capability("screenshot.capture", risk="read", tags=("sensitive",), scope="multi-machine",
                timeout=60)
    async def screenshot_capture(self, quality: int = 85) -> dict:
        """Capture the device's screen (works with a dead display). JPEG when
        ImageMagick is on this worker, PNG otherwise."""
        from .screenshot import _shrink
        quality = opt_int(quality, "quality", 85, min=10, max=100)
        await self.ready()
        code, data, err, _ = await self.plugin.adb_bytes(
            "-s", self.serial, "exec-out", "screencap", "-p", timeout=30, cap=64 << 20)
        if code != 0 or not data.startswith(b"\x89PNG"):
            fail(err.decode(errors="replace").strip()[:300] or "screencap failed")
        fd, path = tempfile.mkstemp(suffix=".png", prefix="rook-adb-shot-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            await _shrink(path, quality)
            with open(path, "rb") as f:
                data = f.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        fmt = "jpeg" if data.startswith(b"\xff\xd8") else "png"
        return {"ok": True, "format": fmt, "data": base64.b64encode(data).decode("ascii"),
                "bytes": len(data)}

    @capability("ui.text", risk="exec", tags=("sensitive",), scope="multi-machine", timeout=40)
    async def ui_text(self) -> dict:
        """All visible text on the current screen (top-to-bottom), with the
        centre of each element to tap: ``{text, items: [{text, x, y, clickable?}]}``."""
        await self.ready()
        for attempt in range(3):
            code, out, err = await self.adb("exec-out", "uiautomator", "dump", "/dev/tty",
                                            timeout=12)
            start = out.find("<?xml")
            # uiautomator gives up while the screen animates ("null root
            # node"); a dump only reads, so trying again is safe.
            if start >= 0 or "null root node" not in out + err or attempt == 2:
                break
            await asyncio.sleep(1)
        if code != 0 or start < 0:
            fail((err or out).strip()[:300] or "uiautomator dump failed")
        end = out.rfind(">")
        items = ui_items(out[start:end + 1])
        return {"text": "\n".join(i["text"] for i in items), "items": items}

    async def _input(self, *commands: tuple[str, ...], timeout: float = 15.0) -> dict:
        """Run ``input`` commands as one device-side script, one caller at a
        time, so another session's taps never land in the middle."""
        async with self._input_lock:
            await self.sh(" && ".join(shlex.join(("input",) + c) for c in commands),
                          timeout=timeout)
        return {"ok": True}

    @capability("hid.type", risk="exec", tags=("physical",), scope="multi-machine")
    async def hid_type(self, text: str) -> dict:
        """Type text into whatever has focus on the device. ASCII only (an
        Android limit); a newline presses Enter."""
        text = require_str(text, "text", strip=False, max_len=2000)
        if not text.isascii():
            raise ArgError("the device types ASCII only; set other text with shell.exec")
        commands = []
        for i, line in enumerate(text.split("\n")):
            if i:
                commands.append(("keyevent", "KEYCODE_ENTER"))
            if line:
                # `input text` reads %s as a space (and has no other escape).
                commands.append(("text", line.replace(" ", "%s")))
        if commands:
            # `input text` types a few dozen characters a second.
            await self._input(*commands, timeout=15 + len(text) / 10)
        return {"ok": True, "typed": len(text)}

    @capability("hid.key_combo", risk="exec", tags=("physical",), scope="multi-machine")
    async def hid_key_combo(self, keys: str) -> dict:
        """Press a key or a combination: ``home``, ``back``, ``recents``,
        ``power``, ``wake``, ``enter``, ``ctrl+a``, or any ``KEYCODE_*`` name."""
        keys = require_str(keys, "keys", max_len=100)
        codes = []
        for k in keys.replace(" ", "").split("+"):
            code = KEYS.get(k.lower())
            if code is None and len(k) == 1 and k.isalnum():
                code = f"KEYCODE_{k.upper()}"
            if code is None and re.match(r"^KEYCODE_[A-Z0-9_]+$", k):
                code = k
            if code is None:
                raise ArgError(f"unknown key {k!r}")
            codes.append(code)
        if len(codes) == 1:
            return await self._input(("keyevent", codes[0]))
        return await self._input(("keycombination", *codes))

    @capability("hid.mouse.click", risk="exec", tags=("physical",), scope="multi-machine")
    async def hid_mouse_click(self, x: int, y: int) -> dict:
        """Tap the screen at pixel (x, y) (``ui.text`` gives element centres)."""
        x = require_int(x, "x", min=0, max=20000)
        y = require_int(y, "y", min=0, max=20000)
        return await self._input(("tap", str(x), str(y)))

    @capability("hid.mouse.drag", risk="exec", tags=("physical",), scope="multi-machine")
    async def hid_mouse_drag(self, x1: int, y1: int, x2: int, y2: int,
                             duration: float = 0.3) -> dict:
        """Swipe from (x1, y1) to (x2, y2) over ``duration`` seconds."""
        pts = [str(require_int(v, n, min=0, max=20000))
               for v, n in ((x1, "x1"), (y1, "y1"), (x2, "x2"), (y2, "y2"))]
        ms = int(max(0.05, min(float(duration), 10.0)) * 1000)
        return await self._input(("swipe", *pts, str(ms)))


class AdbPlugin(Plugin):
    NAMESPACE = "adb"
    SETTINGS = (
        setting("devices", str, default="", scope="worker", env="ROOK_ADB_DEVICES",
                apply="reload", label="Android devices",
                help="name=serial pairs, comma-separated (playground-tablet=R52N30ABCDE). "
                     "Each becomes a device worker this worker answers for, over adb. "
                     "Blank leaves adb.* off."),
        setting("path", "path", default="", scope="worker", env="ROOK_ADB_PATH",
                apply="reload", label="adb binary", advanced=True,
                help="Blank finds adb on PATH."),
    )

    def __init__(self) -> None:
        super().__init__()
        self._devices: dict[str, AndroidDevice] = {}
        self._task: asyncio.Task | None = None

    def _spec(self) -> list[tuple[str, str]]:
        try:
            return parse_devices(self.settings.get("devices") or "")
        except ValueError as e:
            log.warning("adb.devices: %s", e)
            return []

    def _binary(self) -> str | None:
        path = self.settings.get("path") or ""
        if path:
            return path if os.access(path, os.X_OK) else None
        return which("adb")

    def available(self) -> bool:
        return bool(self._spec()) and bool(self._binary())

    def unavailable_reason(self) -> str:
        if not self._binary():
            return "Needs adb (Android platform-tools) on this worker, or adb.path."
        return "No Android devices configured: set adb.devices (name=serial pairs)."

    async def adb(self, *args: str, timeout: float = 15.0, stdin: str | None = None):
        binary = self._binary()
        if not binary:
            fail("adb is not installed on this worker")
        return await run([binary, *args], timeout=timeout, stdin=stdin)

    async def adb_bytes(self, *args: str, timeout: float = 15.0, stdin: str | None = None,
                        cap: int | None = None) -> tuple[int, bytes, bytes, bool]:
        """Like :meth:`adb` but keeps stdout/stderr as bytes (screencap, big
        output): ``(code, out, err, truncated)``. With ``cap``, each stream
        keeps its first ``cap`` bytes; past that adb is stopped (which ends
        the command on the device), so a command that prints without end
        neither fills the worker's memory nor runs out the timeout."""
        binary = self._binary()
        if not binary:
            fail("adb is not installed on this worker")
        payload = stdin.encode() if stdin is not None else None
        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **no_window())
        cut = False

        async def drain(stream) -> bytes:
            nonlocal cut
            buf = bytearray()
            while chunk := await stream.read(65536):
                if cap is not None and len(buf) + len(chunk) > cap:
                    buf.extend(chunk[:max(0, cap - len(buf))])
                    if not cut and proc.returncode is None:
                        proc.kill()
                    cut = True
                else:
                    buf.extend(chunk)
            return bytes(buf)

        async def feed() -> None:
            if payload is not None:
                try:
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                proc.stdin.close()

        async def both():
            _, out, err = await asyncio.gather(feed(), drain(proc.stdout), drain(proc.stderr))
            await proc.wait()
            return out, err

        try:
            out, err = await asyncio.wait_for(both(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RunTimeout(f"adb timed out after {timeout:g}s") from None
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            raise
        return proc.returncode or 0, out, err, cut

    async def _seen(self) -> dict[str, dict]:
        code, out, _ = await self.adb("devices", "-l", timeout=10)
        return parse_adb_devices(out) if code == 0 else {}

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        for name, serial in self._spec():
            dev = AndroidDevice(self, name, serial)
            self._devices[dev.key] = dev
        self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._devices = {}

    async def _poll(self) -> None:
        """Keep each device's adb state (and battery) fresh for the heartbeat."""
        refreshed: dict[str, float] = {}
        while True:
            try:
                seen = await self._seen()
                for dev in list(self._devices.values()):
                    row = seen.get(dev.serial) or {}
                    was = dev.state.get("adb")
                    dev.state["adb"] = row.get("state", "missing")
                    if row.get("model") and not dev.state.get("model"):
                        dev.state["model"] = row["model"][:64]
                    if dev.state["adb"] == "device" and (
                            was != "device" or time.monotonic() - refreshed.get(dev.key, 0) > 300):
                        refreshed[dev.key] = time.monotonic()
                        await dev.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("adb poll failed", exc_info=True)
            await asyncio.sleep(POLL_SECS)

    def devices(self) -> list[Device]:
        return list(self._devices.values())

    # -- caps on the delegate -----------------------------------------------
    @capability("list", risk="read", scope="multi-machine")
    async def _list(self) -> dict:
        """Every configured Android device: ``{items: [{name, serial, adb, model, battery}]}``."""
        return {"items": [{"name": d.name, "serial": d.serial, "adb": d.state.get("adb"),
                           "model": d.state.get("model"), "battery": d.state.get("battery")}
                          for d in self._devices.values()]}

    @capability("scan", risk="read", scope="multi-machine")
    async def _scan(self) -> dict:
        """Every device adb sees on this worker (attached or emulated), for adding
        to adb.devices: serial, state and model, including phones that are only
        charging there. Lists only; it never authorizes or changes a device."""
        known = {d.serial: d.name for d in self._devices.values()}
        return {"items": [{"serial": s, **row, "configured_as": known.get(s)}
                          for s, row in sorted((await self._seen()).items())]}


PLUGIN = AdbPlugin
