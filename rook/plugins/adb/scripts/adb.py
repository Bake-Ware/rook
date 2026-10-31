"""Android devices over adb. Every cap but ``scan`` targets one appliance
instance (a device cabled to this worker) and checks it is attached and
authorized first. adb starts through the worker's launch path (spec 7.1).

Safety, as in 0.4: only the instance's serial is ever addressed (``adb -s``),
never "whatever is plugged in"; Rook never authorizes itself (a person taps
Allow on the device once); nothing runs ``adb root``, ``tcpip``, ``reboot``,
``install`` or ``kill-server``."""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import xml.etree.ElementTree as ET

from rook.core2.worker.security import LaunchRefused, launch
from rook.kit._types import parse_duration

OUTPUT_CAP = 1 << 20        # per stream; a band reply is not a file transfer
SHOT_CAP = 64 << 20
_SERIAL = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# What adb itself prints when the device goes away mid-command.
_GONE = re.compile(r"^(adb: |error: )(device .*(not found|offline)|no devices)", re.M)
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
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
}
MODS = {"ctrl": "KEYCODE_CTRL_LEFT", "alt": "KEYCODE_ALT_LEFT", "shift": "KEYCODE_SHIFT_LEFT",
        "meta": "KEYCODE_META_LEFT"}

# One input sequence per device at a time, so two callers' taps never
# interleave. Module state lives as long as this plugin load.
_input_locks: dict[str, asyncio.Lock] = {}


# -- caps ---------------------------------------------------------------------

async def scan(ctx):
    code, out, err, _ = await _adb(ctx, "devices", "-l", timeout=10)
    if code != 0:
        raise ctx.Error(_text(err or out)[:300] or f"adb devices failed (exit {code})")
    return {"items": [{"serial": s, **row} for s, row in sorted(parse_devices(_text(out)).items())]}


async def shell_exec(ctx, argv=None, cmd=None, stdin=None, cwd=None, env=None, timeout=30):
    if (argv is None) == (cmd is None):
        raise ctx.Error("give argv or cmd (one of them)")
    if argv is not None and not argv:
        raise ctx.Error("argv is empty")
    remote = shlex.join(argv) if argv is not None else cmd
    prefix = []
    if env is not None:
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise ctx.Error("env is an object of strings")
        for k, v in env.items():
            if not _ENV_KEY.fullmatch(k):
                raise ctx.Error(f"bad environment name {k!r}")
            prefix.append(f"export {k}={shlex.quote(v)}")
    if cwd:
        prefix.append(f"cd {shlex.quote(cwd)} || exit 1")
    if prefix:
        remote = "\n".join([*prefix, remote])
    serial = await _ready(ctx)
    # Killing the local adb ends the device-side command too (shell v2).
    code, out, err, cut = await _adb(ctx, "-s", serial, "shell", "-T", "--", remote,
                                     timeout=timeout, stdin=stdin, cap=OUTPUT_CAP,
                                     on_timeout="ended on the device; partial side effects may have landed")
    stderr = _text(err)
    if code == 1 and _GONE.search(stderr):
        # adb's own failure, not the command's exit 1: the cable went.
        raise ctx.Error(f"{_name(ctx)} left adb while the command ran; partial side effects may have landed")
    reply = {"code": code, "stdout": _text(out), "stderr": stderr}
    if cut:
        reply["truncated"] = True
    return reply


async def battery_status(ctx):
    out = await _sh(ctx, "dumpsys battery")
    f = dict(re.findall(r"^[ \t]*([A-Za-z ]+):[ \t]*(.+?)[ \t]*$", out, re.M))
    status = {"2": "charging", "3": "discharging", "4": "not charging", "5": "full"}
    plugged = [k.split()[0].lower() for k in ("AC powered", "USB powered", "Wireless powered")
               if f.get(k) == "true"]
    reply = {"percent": int(f["level"]) if f.get("level", "").isdigit() else None,
             "charging": f.get("status") == "2",
             "plugged": plugged[0] if plugged else None,
             "status": status.get(f.get("status", ""), "unknown")}
    if f.get("temperature", "").lstrip("-").isdigit():
        reply["temp_c"] = int(f["temperature"]) / 10
    return reply


async def screen_capture(ctx, quality=None):
    serial = await _ready(ctx)
    code, data, err, cut = await _adb(ctx, "-s", serial, "exec-out", "screencap", "-p",
                                      timeout=30, cap=SHOT_CAP)
    if code != 0 or cut or not data.startswith(b"\x89PNG"):
        raise ctx.Error(_text(err).strip()[:300] or "screencap failed")
    png = ctx.tmp / "screen.png"
    png.write_bytes(data)
    jpg = await _to_jpeg(ctx, png, 85 if quality is None else quality)
    # Bytes, not a path: ctx.tmp is gone once the call returns.
    if jpg is not None:
        return ctx.file(data=jpg.read_bytes(), name="screen.jpg", mime="image/jpeg")
    return ctx.file(data=data, name="screen.png", mime="image/png")


async def ui_screen(ctx, limit=None):
    serial = await _ready(ctx)
    for attempt in range(3):
        code, out, err, _ = await _adb(ctx, "-s", serial, "exec-out", "uiautomator", "dump",
                                       "/dev/tty", timeout=12, cap=OUTPUT_CAP * 8)
        out, err = _text(out), _text(err)
        start = out.find("<?xml")
        # uiautomator gives up while the screen animates ("null root node");
        # a dump only reads, so trying again is safe.
        if start >= 0 or "null root node" not in out + err or attempt == 2:
            break
        await asyncio.sleep(1)
    if code != 0 or start < 0:
        raise ctx.Error((err or out).strip()[:300] or "uiautomator dump failed")
    app, items = ui_items(ctx, out[start:out.rfind(">") + 1])
    return {"app": app, "items": items[:limit] if limit else items}


async def hid_type(ctx, text, enter=False):
    if not text.isascii():
        raise ctx.Error("the device types ASCII only; set other text with adb.shell.exec")
    commands = []
    for i, line in enumerate(text.split("\n")):
        if i:
            commands.append(("keyevent", "KEYCODE_ENTER"))
        if line:
            # `input text` reads %s as a space (and has no other escape).
            commands.append(("text", line.replace(" ", "%s")))
    if enter:
        commands.append(("keyevent", "KEYCODE_ENTER"))
    if commands:
        # `input text` types a few dozen characters a second.
        await _input(ctx, *commands, timeout=15 + len(text) / 10)
    return {"typed": len(text), "enter": enter}


async def hid_key(ctx, key, mods=()):
    code = KEYS.get(key.lower())
    if code is None and len(key) == 1 and key.isalnum():
        code = f"KEYCODE_{key.upper()}"
    if code is None and re.fullmatch(r"KEYCODE_[A-Z0-9_]+", key):
        code = key
    if code is None:
        raise ctx.Error(f"unknown key {key!r}")
    codes = [MODS[m] for m in dict.fromkeys(mods)] + [code]
    if len(codes) == 1:
        await _input(ctx, ("keyevent", code))
    else:
        await _input(ctx, ("keycombination", *codes))
    return {"keys": codes}


async def hid_click(ctx, x=None, y=None, button="left"):
    if x is None or y is None:
        raise ctx.Error("a touch screen needs x and y (ui.screen gives element centres)")
    if button != "left":
        raise ctx.Error(f"a touch screen has no {button} button")
    await _input(ctx, ("tap", str(x), str(y)))
    return {"x": x, "y": y}


async def hid_drag(ctx, x1, y1, x2, y2, duration="300ms", button="left"):
    if button != "left":
        raise ctx.Error(f"a touch screen has no {button} button")
    try:
        secs = parse_duration(duration)
    except ValueError:
        raise ctx.Error(f"duration {duration!r} is not a duration") from None
    ms = int(max(0.05, min(secs, 10.0)) * 1000)
    await _input(ctx, ("swipe", str(x1), str(y1), str(x2), str(y2), str(ms)))
    return {"from": [x1, y1], "to": [x2, y2], "ms": ms}


# -- parsing ------------------------------------------------------------------

def parse_devices(text: str) -> dict[str, dict]:
    """``adb devices -l`` output -> ``{serial: {state, model?}}``."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or line.startswith(("List of", "*")):
            continue
        row = {"state": parts[1]}
        for extra in parts[2:]:
            k, _, v = extra.partition(":")
            if k == "model" and v:
                row["model"] = v
        out[parts[0]] = row
    return out


def ui_items(ctx, xml: str) -> tuple[str | None, list[dict]]:
    """The app in front and the text a person would read, from a
    ``uiautomator dump``: ``[{text, x, y, clickable?}]`` top to bottom, x/y
    the centre of the element."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ctx.Error("the screen dump was not readable; try again", retry=True) from None
    app, items = None, []
    for node in root.iter("node"):
        app = app or node.get("package") or None
        text = (node.get("text") or "").strip() or (node.get("content-desc") or "").strip()
        m = _BOUNDS.fullmatch(node.get("bounds") or "")
        if not text or not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        item = {"text": text, "x": (x1 + x2) // 2, "y": (y1 + y2) // 2}
        if node.get("clickable") == "true":
            item["clickable"] = True
        items.append(item)
    items.sort(key=lambda i: (i["y"], i["x"]))
    return app, items


# -- adb ----------------------------------------------------------------------

def _serial(ctx) -> str:
    # The instance the hub carried with the call (spec 5.7): {name, serial}.
    a = getattr(ctx, "appliance", None) or {}
    serial = a.get("serial") or a.get("name") or ""
    if not _SERIAL.fullmatch(str(serial)):
        raise ctx.Error("adb: this call has no device instance (or its serial is not an adb serial)")
    return serial


def _name(ctx) -> str:
    a = getattr(ctx, "appliance", None) or {}
    return a.get("name") or _serial(ctx)


async def _ready(ctx) -> str:
    """The instance's serial, once the device is attached and authorized."""
    serial = _serial(ctx)
    code, out, err, _ = await _adb(ctx, "-s", serial, "get-state", timeout=8)
    state = _text(out).strip() if code == 0 else ""
    if not state:
        low = _text(err).lower()
        state = "unauthorized" if "unauthorized" in low else "offline" if "offline" in low else "missing"
    if state == "device":
        return serial
    if state == "unauthorized":
        raise ctx.Error(f"{_name(ctx)} has not allowed USB debugging from this computer: someone "
                        "has to tap Allow on it once")
    if state == "missing":
        raise ctx.Error(f"{_name(ctx)} ({serial}) is not connected to this worker", retry=True)
    raise ctx.Error(f"{_name(ctx)} is {state} on adb", retry=True)


async def _sh(ctx, remote: str, timeout: float = 15) -> str:
    """A fixed helper command on the device: its stdout, or an error."""
    serial = await _ready(ctx)
    code, out, err, _ = await _adb(ctx, "-s", serial, "shell", "-T", "--", remote,
                                   timeout=timeout, cap=OUTPUT_CAP)
    if code != 0:
        raise ctx.Error(_text(err or out).strip()[:300] or f"{remote.split()[0]} failed (exit {code})")
    return _text(out)


async def _input(ctx, *commands: tuple[str, ...], timeout: float = 15) -> None:
    """``input`` commands as one device-side script, one caller at a time."""
    serial = _serial(ctx)
    async with _input_locks.setdefault(serial, asyncio.Lock()):
        await _sh(ctx, " && ".join(shlex.join(("input", *c)) for c in commands), timeout=timeout)


async def _adb(ctx, *args: str, timeout: float, stdin: str | None = None,
               cap: int | None = None, on_timeout: str = "") -> tuple[int, bytes, bytes, bool]:
    """Run adb: ``(code, stdout, stderr, truncated)``. With ``cap`` each stream
    keeps its first ``cap`` bytes and adb is stopped past that (which ends the
    command on the device). Killed at ``timeout``, or when the call is cancelled."""
    binary = shutil.which("adb")
    if not binary:
        raise ctx.Error("adb is not installed on this worker")
    return await _run(ctx, [binary, *args], timeout=timeout, stdin=stdin, cap=cap,
                      what="adb", on_timeout=on_timeout)


async def _run(ctx, argv: list[str], *, timeout: float, stdin: str | None = None,
               cap: int | None = None, what: str, on_timeout: str = ""):
    payload = stdin.encode() if stdin is not None else None
    try:
        proc = await launch(
            argv, getattr(ctx, "lease", None),
            stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except LaunchRefused as e:
        raise ctx.Error(f"can't start {what}: {e}") from None
    except OSError as e:
        raise ctx.Error(f"can't start {what}: {e.strerror or e}") from None
    cut = False

    async def drain(stream) -> bytes:
        nonlocal cut
        buf = bytearray()
        while chunk := await stream.read(65536):
            if cap is not None and len(buf) + len(chunk) > cap:
                buf.extend(chunk[:max(0, cap - len(buf))])
                if proc.returncode is None:
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
    except TimeoutError:
        _kill(proc)
        await proc.wait()
        raise ctx.Error(f"{what} timed out after {timeout:g}s" + (f"; {on_timeout}" if on_timeout else "")) \
            from None
    except asyncio.CancelledError:
        _kill(proc)
        raise
    return proc.returncode or 0, out, err, cut


def _kill(proc) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _to_jpeg(ctx, png, quality: int):
    """Downscale and re-encode with ImageMagick when it's here; None if not."""
    conv = shutil.which("magick") or shutil.which("convert")
    if not conv:
        return None
    out = png.with_suffix(".jpg")
    try:
        code, _, _, _ = await _run(ctx, [conv, str(png), "-resize", "1600x1600>",
                                         "-quality", str(quality), str(out)],
                                   timeout=15, what="ImageMagick")
    except ctx.Error:
        return None
    return out if code == 0 and out.is_file() and out.stat().st_size else None


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
