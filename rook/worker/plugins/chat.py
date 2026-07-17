"""chat.* — two-way chat between the operator (rook band) and a worker's human.

The transcript for a room lives on the worker as a JSONL file. The operator
drives it over the band (``chat.send`` / ``chat.poll``); ``chat.open`` pops a
terminal window ON THE WORKER running a tiny chat client that tails the same
file and lets the local human type back. Both ends read/write one transcript, so
it stays in sync.

    chat.open(room, me?, title?)   -> {ok, spawned, via}
    chat.send(room, text, sender)  -> {ok}
    chat.poll(room, since=0)       -> {ok, messages:[{ts,sender,text}], last}
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..plugin import Plugin, capability

_CHATDIR = Path(os.path.expanduser("~")) / ".rook-band-worker" / "chat"


def _room_file(room: str) -> Path:
    safe = "".join(c for c in str(room) if c.isalnum() or c in "-_") or "default"
    return _CHATDIR / f"{safe}.jsonl"


# The chat client that runs in the popped-up terminal on the worker. Same
# two-panel layout as the operator's `rook band` chat pane (sidebar of the local
# rooms + conversation + input) so both ends look identical. Pure stdlib; reads
# rooms straight from the JSONL transcripts in the chat dir.
_RECV_CLIENT = r'''#!/usr/bin/env python3
import sys, os, json, time, curses, textwrap, glob
chatdir, me = sys.argv[1], sys.argv[2]
room = sys.argv[3] if len(sys.argv) > 3 else None
os.makedirs(chatdir, exist_ok=True)

def load(r):
    out = []
    try:
        for ln in open(os.path.join(chatdir, r + ".jsonl")).read().splitlines():
            try: out.append(json.loads(ln))
            except Exception: pass
    except Exception: pass
    return out

def rooms():
    out = []
    for f in sorted(glob.glob(os.path.join(chatdir, "*.jsonl"))):
        r = os.path.basename(f)[:-6]
        ms = load(r); last = ms[-1] if ms else {}
        out.append({"room": r, "last_ts": last.get("ts", 0),
                    "last_text": last.get("text", ""), "last_sender": last.get("sender")})
    out.sort(key=lambda e: e["last_ts"], reverse=True)
    return out

def send(r, text):
    with open(os.path.join(chatdir, r + ".jsonl"), "a") as fh:
        fh.write(json.dumps({"ts": int(time.time()), "sender": me, "text": text}) + "\n")

def run(scr):
    global room
    curses.curs_set(1); scr.timeout(400)
    curses.start_color(); curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)    # me
    curses.init_pair(4, curses.COLOR_CYAN, -1)     # accents
    inp = ""
    while True:
        rs = rooms()
        if room is None and rs: room = rs[0]["room"]
        if room is None: room = "chat"
        sidesel = next((i for i, e in enumerate(rs) if e["room"] == room), 0)
        h, w = scr.getmaxyx(); scr.erase()
        sw = min(30, max(16, w // 4))
        def put(y, x, t, a=0):
            n = w - 1 - x
            if 0 <= y < h and n > 0:
                try: scr.addnstr(y, x, t[:n], n, a)
                except curses.error: pass
        put(0, 0, (" ROOK CHAT · %s/%s · ↑↓ switch · Ctrl-C close " % (me, room)).ljust(w),
            curses.A_REVERSE)
        put(1, 0, " chats".ljust(sw), curses.A_BOLD)
        for i, e in enumerate(rs[:h - 3]):
            put(2 + i, 0, (" " + e["room"]).ljust(sw), curses.A_REVERSE if i == sidesel else 0)
        for y in range(1, h - 1):
            try: scr.addch(y, sw, curses.ACS_VLINE)
            except curses.error: pass
        cx, cw = sw + 2, w - sw - 2
        rowlines = []
        for m in load(room):
            mine = m.get("sender") == me
            who = "you" if mine else str(m.get("sender"))
            for j, seg in enumerate(textwrap.wrap(str(m.get("text", "")), max(8, cw - 13)) or [""]):
                head = ("%-11s" % (who + ":")) if j == 0 else " " * 11
                rowlines.append((head + " " + seg, mine))
        for i, (line, mine) in enumerate(rowlines[-(h - 4):]):
            put(2 + i, cx, line, curses.color_pair(1) if mine else 0)
        put(h - 1, cx, ("> " + inp).ljust(cw), curses.A_BOLD)
        try: scr.move(h - 1, min(cx + 2 + len(inp), w - 2))
        except curses.error: pass
        scr.refresh()
        try: k = scr.getch()
        except KeyboardInterrupt: break
        if k == -1: continue
        if k in (10, 13):
            if inp.strip(): send(room, inp)
            inp = ""
        elif k == curses.KEY_UP and rs:
            room = rs[max(0, sidesel - 1)]["room"]
        elif k == curses.KEY_DOWN and rs:
            room = rs[min(len(rs) - 1, sidesel + 1)]["room"]
        elif k in (curses.KEY_BACKSPACE, 127, 8): inp = inp[:-1]
        elif 32 <= k <= 126: inp += chr(k)

try: curses.wrapper(run)
except Exception: pass
'''


def _spawn_terminal(script: str, chatdir: str, me: str, room: str, title: str) -> str | None:
    """Best-effort: open a terminal window running the chat client. Returns the
    terminal it used, or None if no display / no terminal emulator."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    py = sys.executable or "python3"
    cmd = [py, script, chatdir, me, room]
    joined = " ".join(shlex.quote(c) for c in cmd)
    candidates = [
        ["konsole", "-p", "tabtitle=" + title, "-e"] + cmd,
        ["gnome-terminal", "--title", title, "--"] + cmd,
        ["xfce4-terminal", "--title", title, "-e", joined],
        ["alacritty", "-t", title, "-e"] + cmd,
        ["kitty", "-T", title] + cmd,
        ["xterm", "-T", title, "-e"] + cmd,
        ["x-terminal-emulator", "-e"] + cmd,
    ]
    for c in candidates:
        if not shutil.which(c[0]):
            continue
        try:
            subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return c[0]
        except Exception:
            continue
    return None


class ChatPlugin(Plugin):
    NAMESPACE = "chat"

    @capability("open")
    def _open(self, room: str, me: str | None = None, title: str = "rook chat") -> dict:
        """Pop a chat window on this worker for ``room`` and return whether one
        was spawned. The local human types there; the operator uses chat.send/poll."""
        _CHATDIR.mkdir(parents=True, exist_ok=True)
        me = me or socket.gethostname()
        transcript = _room_file(room)
        transcript.touch(exist_ok=True)
        # Write the client script once (per boot) and spawn a terminal for it.
        client = _CHATDIR / "_recv_client.py"
        try:
            client.write_text(_RECV_CLIENT)
            os.chmod(client, 0o755)
        except Exception as e:
            return {"ok": False, "error": f"could not write client: {e}"}
        via = _spawn_terminal(str(client), str(_CHATDIR), me, room, title)
        return {"ok": True, "spawned": via is not None, "via": via,
                "room": room, "note": ("no window — no display/terminal here; "
                                        "messages still arrive via chat.poll/msg"
                                        if via is None else None)}

    @capability("send")
    def _send(self, room: str, text: str, sender: str = "operator") -> dict:
        """Append a message to the room transcript (the worker's window shows it)."""
        text = str(text or "")
        if not text.strip():
            return {"ok": False, "error": "empty message"}
        _CHATDIR.mkdir(parents=True, exist_ok=True)
        entry = {"ts": int(time.time()), "sender": str(sender)[:64], "text": text[:4000]}
        try:
            with open(_room_file(room), "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True}

    @capability("rooms")
    def _rooms(self) -> dict:
        """List chat rooms on this worker with a last-message preview — the band
        CLI aggregates these across workers into its chat sidebar."""
        out = []
        if _CHATDIR.exists():
            for f in sorted(_CHATDIR.glob("*.jsonl")):
                msgs = []
                try:
                    for ln in f.read_text().splitlines():
                        try:
                            msgs.append(json.loads(ln))
                        except Exception:
                            continue
                except Exception:
                    continue
                if not msgs:
                    continue
                last = msgs[-1]
                out.append({
                    "room": f.stem,
                    "count": len(msgs),
                    "last_ts": last.get("ts", 0),
                    "last_sender": last.get("sender"),
                    "last_text": str(last.get("text", ""))[:120],
                    "participants": sorted({m.get("sender") for m in msgs if m.get("sender")}),
                })
        out.sort(key=lambda r: r.get("last_ts", 0), reverse=True)
        return {"ok": True, "rooms": out}

    @capability("poll")
    def _poll(self, room: str, since: float = 0.0) -> dict:
        """Return messages in ``room`` newer than ``since`` (unix ts). The
        operator polls this to receive the local human's replies."""
        f = _room_file(room)
        if not f.exists():
            return {"ok": True, "messages": [], "last": since}
        out = []
        last = float(since)
        try:
            for ln in f.read_text().splitlines():
                try:
                    m = json.loads(ln)
                except Exception:
                    continue
                if float(m.get("ts", 0)) > float(since):
                    out.append(m)
                    last = max(last, float(m.get("ts", 0)))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "messages": out, "last": last}


PLUGIN = ChatPlugin
