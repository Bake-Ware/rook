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


# The chat client that runs in the popped-up terminal on the worker. Pure stdlib.
# Tails the shared transcript in a thread and appends the human's lines to it.
_RECV_CLIENT = r'''#!/usr/bin/env python3
import sys, os, json, time, threading
path, me = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path, "a").close()
def tail():
    seen = 0
    while True:
        try:
            lines = open(path).read().splitlines()
        except Exception:
            lines = []
        for ln in lines[seen:]:
            try: m = json.loads(ln)
            except Exception: continue
            if m.get("sender") != me:
                sys.stdout.write("\r\033[K\033[1m%s:\033[0m %s\n> " % (m.get("sender"), m.get("text")))
                sys.stdout.flush()
        seen = len(lines)
        time.sleep(0.5)
threading.Thread(target=tail, daemon=True).start()
print("\033[1m── rook chat ──\033[0m  you are %r · type and press enter · Ctrl-C to close\n" % me)
try:
    while True:
        t = input("> ")
        if not t.strip(): continue
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "sender": me, "text": t}) + "\n")
except (EOFError, KeyboardInterrupt):
    print("\nchat closed.")
'''


def _spawn_terminal(script: str, transcript: str, me: str, title: str) -> str | None:
    """Best-effort: open a terminal window running the chat client. Returns the
    terminal it used, or None if no display / no terminal emulator."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    py = sys.executable or "python3"
    cmd = [py, script, transcript, me]
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
        via = _spawn_terminal(str(client), str(transcript), me, title)
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
