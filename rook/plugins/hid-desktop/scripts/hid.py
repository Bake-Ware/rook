"""hid-desktop on the worker it is called on: the input tools run there
through shell.exec. Key names and buttons differ per tool and click/drag are
sequences, all worked out here.

Linux: xdotool, then wtype (keyboard only), then ydotool. hid.type and
hid.move try each in turn (a failing one hands over to the next); key, click
and drag use the first one installed. Windows: PowerShell, with user32's
keybd_event, mouse_event and SetCursorPos for keys, clicks and drags."""

import asyncio

from rook.kit._types import parse_duration

KEYBOARD = ("xdotool", "wtype", "ydotool")
POINTER = ("xdotool", "ydotool")

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

# Windows virtual-key codes, and mouse_event's (down, up) flags.
VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "meta": 0x5B, "enter": 0x0D, "return": 0x0D,
      "escape": 0x1B, "esc": 0x1B, "tab": 0x09, "backspace": 0x08, "space": 0x20,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "delete": 0x2E,
      **{f"f{i}": 0x6F + i for i in range(1, 13)},
      **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"}}
WIN_BUTTON = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}

PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
WIN_TYPE = ("Add-Type -AssemblyName System.Windows.Forms; [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false; "
            "$t = [Console]::In.ReadToEnd() -replace '[+^%~(){}\\[\\]]', '{$0}'; $t = $t -replace \"`r?`n\", '{ENTER}'; "
            "[System.Windows.Forms.SendKeys]::SendWait($t)")
WIN_MOVE = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point([int]$env:ROOK_X, [int]$env:ROOK_Y)")
# ROOK_SEQ: space-separated steps k:<vk>:<1 down|0 up>, m:<mouse_event flags>, p:<x>:<y>, s:<ms>.
# The last step that lifts a button runs even when an earlier one fails.
WIN_SEQ = (
    "$u = Add-Type -Namespace RookHid -Name U -PassThru -MemberDefinition '"
    "[DllImport(\"user32.dll\")] public static extern void keybd_event(byte v, byte s, uint f, UIntPtr e); "
    "[DllImport(\"user32.dll\")] public static extern void mouse_event(uint f, int x, int y, uint d, UIntPtr e); "
    "[DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int x, int y);'; "
    "$steps = $env:ROOK_SEQ.Split(' '); try { foreach ($s in $steps[0..($steps.Length - 2)]) { $a = $s.Split(':'); switch ($a[0]) { "
    "'k' { $u::keybd_event([byte]$a[1], 0, $(if ($a[2] -eq '1') { 0 } else { 2 }), [UIntPtr]::Zero) } "
    "'m' { $u::mouse_event([uint32]$a[1], 0, 0, 0, [UIntPtr]::Zero) } "
    "'p' { [void]$u::SetCursorPos([int]$a[1], [int]$a[2]) } "
    "'s' { Start-Sleep -Milliseconds ([int]$a[1]) } } } } finally { $a = $steps[-1].Split(':'); "
    "if ($a[0] -eq 'k') { $u::keybd_event([byte]$a[1], 0, 2, [UIntPtr]::Zero) } "
    "elseif ($a[0] -eq 'm') { $u::mouse_event([uint32]$a[1], 0, 0, 0, [UIntPtr]::Zero) } }")


def _windows(ctx):
    return ctx.worker["os"] == "windows"


async def _exec(ctx, argv, *, stdin=None, env=None, timeout=8.0, failed="failed"):
    req = {"argv": argv, "timeout": timeout}
    if stdin is not None:
        req["stdin"] = stdin
    if env:
        req["env"] = env
    out = await ctx.call_on(ctx.worker["id"], "shell.exec", **req)
    if out["code"] != 0:
        msg = out["stderr"].strip()
        if failed == "exited":
            raise ctx.Error(f"{argv[0]} exited {out['code']}" + (f": {msg[-2000:]}" if msg else ""))
        raise ctx.Error(f"{argv[0]} failed: {msg or 'exit ' + str(out['code'])}")
    return out["stdout"].rstrip("\r\n")


async def _try(ctx, tries, timeout, **kw):
    """The first of ``tries`` that runs; a missing or failing one hands over to the next."""
    missing, failed = [], None
    for argv in tries:
        try:
            return await _exec(ctx, argv, failed="exited", timeout=timeout, **kw)
        except ctx.Error as e:
            if e.message == f"not found: {argv[0]}":
                missing.append(argv[0])
            else:
                failed = e
    raise failed or ctx.Error(f"not installed on this worker: {', '.join(missing)}")


async def _which(ctx, tools):
    for name in tools:
        if await ctx.call_on(ctx.worker["id"], "shell.which", name=name):
            return name
    return None


async def _tool(ctx, tools, what):
    name = await _which(ctx, tools)
    if name is None:
        raise ctx.Error(f"no {what} tool on this worker: install {' or '.join(tools)}")
    return name


async def _seq(ctx, steps, timeout=8.0):
    await _exec(ctx, [*PS, WIN_SEQ], env={"ROOK_SEQ": " ".join(steps)}, timeout=timeout)


async def type_(ctx, text, enter=False):
    # The text goes on stdin: never in argv, where `ps` shows it.
    stdin = text + ("\n" if enter else "")
    if _windows(ctx):
        return await _try(ctx, [[*PS, WIN_TYPE]], 25, stdin=stdin)
    return await _try(ctx, [["xdotool", "type", "--clearmodifiers", "--file", "-"], ["wtype", "-"],
                            ["ydotool", "type", "--file", "-"]], 25, stdin=stdin)


async def move(ctx, x, y):
    if _windows(ctx):
        return await _try(ctx, [[*PS, WIN_MOVE]], 4, env={"ROOK_X": str(x), "ROOK_Y": str(y)})
    return await _try(ctx, [["xdotool", "mousemove", str(x), str(y)],
                            ["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)]], 4)


async def key(ctx, key: str, mods=()):
    mods = list(dict.fromkeys(mods))
    name = key.lower() if len(key) > 1 else key
    if _windows(ctx):
        if name.lower() not in VK:
            raise ctx.Error(f"unknown key: {key}")
        vks, vk = [VK[m] for m in mods], VK[name.lower()]
        await _seq(ctx, [*(f"k:{v}:1" for v in vks), f"k:{vk}:1", f"k:{vk}:0", *(f"k:{v}:0" for v in reversed(vks))])
        return {"key": key, "mods": mods}
    tool = await _tool(ctx, KEYBOARD, "keyboard")
    sym = KEYSYM.get(name, key)
    if tool == "xdotool":
        await _exec(ctx, ["xdotool", "key", "--clearmodifiers", "+".join([*(XDO_MOD[m] for m in mods), sym])])
    elif tool == "wtype":
        argv = ["wtype"]
        for m in mods:
            argv += ["-M", WTYPE_MOD[m]]
        argv += ["-k", sym]
        for m in reversed(mods):
            argv += ["-m", WTYPE_MOD[m]]
        await _exec(ctx, argv)
    else:
        code = EVDEV.get(name.lower())
        if code is None:
            raise ctx.Error(f"ydotool: unknown key {key} (use a contract key name)")
        codes = [EVDEV[m] for m in mods]
        await _exec(ctx, ["ydotool", "key", *(f"{c}:1" for c in codes), f"{code}:1", f"{code}:0",
                          *(f"{c}:0" for c in reversed(codes))])
    return {"key": key, "mods": mods, "backend": tool}


async def click(ctx, button: str = "left", x: int | None = None, y: int | None = None):
    if (x is None) != (y is None):
        raise ctx.Error("give x and y together, or neither")
    if _windows(ctx):
        down, up = WIN_BUTTON[button]
        await _seq(ctx, [*([f"p:{x}:{y}"] if x is not None else []), f"m:{down}", f"m:{up}"])
        return {"button": button, "x": x, "y": y}
    tool = await _tool(ctx, POINTER, "pointer")
    if tool == "xdotool":
        move_to = ["mousemove", str(x), str(y)] if x is not None else []
        await _exec(ctx, ["xdotool", *move_to, "click", XDO_BUTTON[button]])
    else:
        if x is not None:
            await _exec(ctx, ["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)])
        await _exec(ctx, ["ydotool", "click", f"0x{YDO_BUTTON[button] | 0xC0:02X}"])
    return {"button": button, "x": x, "y": y, "backend": tool}


async def drag(ctx, x1: int, y1: int, x2: int, y2: int, duration="300ms", button: str = "left"):
    secs = parse_duration(duration)
    if _windows(ctx):
        down, up = WIN_BUTTON[button]
        await _seq(ctx, [f"p:{x1}:{y1}", f"m:{down}", f"s:{int(secs * 1000)}", f"p:{x2}:{y2}", f"m:{up}"],
                   timeout=secs + 8)
        return {"from": [x1, y1], "to": [x2, y2], "button": button}
    tool = await _tool(ctx, POINTER, "pointer")
    if tool == "xdotool":
        b = XDO_BUTTON[button]
        await _exec(ctx, ["xdotool", "mousemove", str(x1), str(y1), "mousedown", b, "sleep", f"{secs:g}",
                          "mousemove", str(x2), str(y2), "mouseup", b], timeout=secs + 8)
    else:
        b = YDO_BUTTON[button]
        await _exec(ctx, ["ydotool", "mousemove", "--absolute", "-x", str(x1), "-y", str(y1)])
        await _exec(ctx, ["ydotool", "click", f"0x{b | 0x40:02X}"])
        try:
            await asyncio.sleep(secs)
            await _exec(ctx, ["ydotool", "mousemove", "--absolute", "-x", str(x2), "-y", str(y2)])
        finally:   # never leave the button held
            await _exec(ctx, ["ydotool", "click", f"0x{b | 0x80:02X}"])
    return {"from": [x1, y1], "to": [x2, y2], "button": button, "backend": tool}


async def status(ctx):
    if _windows(ctx):
        return {"backend": "sendinput", "pointer": "sendinput", "platform": "win32"}
    return {"backend": await _which(ctx, KEYBOARD), "pointer": await _which(ctx, POINTER), "platform": "linux"}
