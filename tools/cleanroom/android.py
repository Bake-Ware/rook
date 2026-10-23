"""Drive the Rook APK in an emulator the way a person would: through its UI.

uiautomator dumps the screen; taps and typing go through `adb shell input`.
Every interaction a person would make is counted as a human touch by the
scenario that calls these helpers.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .providers import EmulatorProvider

PACKAGE = "systems.bake.rook"


@dataclass
class Node:
    rid: str
    text: str
    desc: str
    cls: str
    clickable: bool
    bounds: tuple[int, int, int, int]

    @property
    def center(self):
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


def dump(emu: EmulatorProvider) -> list[Node]:
    for _ in range(3):
        emu.adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", check=False, timeout=60)
        xml = emu.adb("shell", "cat", "/sdcard/ui.xml", check=False).stdout
        if "<node" in xml:
            break
        time.sleep(1)
    nodes = []
    for m in re.finditer(r"<node [^>]*?/?>", xml):
        n = m.group(0)

        def g(k):
            mm = re.search(k + r'="([^"]*)"', n)
            return mm.group(1) if mm else ""
        b = [int(v) for v in re.findall(r"\d+", g("bounds"))] or [0, 0, 0, 0]
        nodes.append(Node(g("resource-id"), _unescape(g("text")), _unescape(g("content-desc")),
                          g("class"), g("clickable") == "true", tuple(b[:4])))
    return nodes


def _unescape(s: str) -> str:
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#10;", "\n")


def dismiss_system_dialogs(emu, nodes=None) -> bool:
    """Close "X isn't responding" dialogs a slow emulator throws up."""
    nodes = nodes if nodes is not None else dump(emu)
    wait = find(nodes, rid="aerr_wait")
    if wait:
        tap(emu, wait)
        return True
    return False


def find(nodes, *, rid=None, text=None, contains=None):
    for n in nodes:
        if rid and not n.rid.endswith(":id/" + rid):
            continue
        if text is not None and n.text.strip().lower() != text.lower():
            continue
        if contains and contains.lower() not in (n.text + " " + n.desc).lower():
            continue
        return n
    return None


def wait_for(emu, *, timeout=30, **kw) -> Node:
    deadline = time.time() + timeout
    while time.time() < deadline:
        nodes = dump(emu)
        n = find(nodes, **kw)
        if n:
            return n
        dismiss_system_dialogs(emu, nodes)
        time.sleep(1.5)
    raise TimeoutError(f"UI element {kw} never appeared")


def tap(emu, node: Node):
    x, y = node.center
    emu.adb("shell", "input", "tap", str(x), str(y))
    time.sleep(0.8)


def scroll_to(emu, *, timeout=20, **kw) -> Node:
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = find(dump(emu), **kw)
        if n:
            return n
        emu.adb("shell", "input", "swipe", "540", "1700", "540", "700", "300")
        time.sleep(0.8)
    raise TimeoutError(f"UI element {kw} not found while scrolling")


def type_into(emu, node: Node, text: str):
    tap(emu, node)
    # Clear what is there, then type (input text needs spaces as %s).
    emu.adb("shell", "input", "keyevent", "KEYCODE_MOVE_END")
    for _ in range(len(node.text) + 2):
        emu.adb("shell", "input", "keyevent", "KEYCODE_DEL", check=False)
    emu.adb("shell", "input", "text", text.replace(" ", "%s"))
    emu.adb("shell", "input", "keyevent", "KEYCODE_BACK", check=False)  # hide the keyboard
    time.sleep(0.5)


def launch(emu):
    emu.adb("shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity")
    time.sleep(3)


def grant_runtime_permissions(emu):
    """What a person taps through on first start: notifications and the mic."""
    for perm in ("android.permission.POST_NOTIFICATIONS", "android.permission.RECORD_AUDIO"):
        emu.adb("shell", "pm", "grant", PACKAGE, perm, check=False)
