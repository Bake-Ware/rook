"""agent.* — wake a local agent to attend a chat room (design §3, the wake cap).

A worker set up to spawn an agent session exposes ``agent.wake``. When the site
calls it (via rook_chat_wake), this host launches the configured agent command,
handing it the room transcript so it joins the conversation as a normal session
and posts back through rook chat. That's the asymmetric half of the design: the
caller stays in tool-land; the woken agent gets a real session where the room's
messages are its prompt.

Gated on ``ROOK_WAKE_CMD`` so only machines actually wired to spawn an agent
announce the cap (like the memory vault's env gate). The command is a template:

    ROOK_WAKE_CMD='claude -p {prompt_file}'          # {prompt_file} = path to the brief
    ROOK_WAKE_CMD='hermes run --stdin'               # brief arrives on stdin
    ROOK_WAKE_CMD='/usr/local/bin/wake-hermes.sh'    # your own launcher; brief on stdin + $ROOK_WAKE_PROMPT

Placeholders substituted in the command: ``{prompt_file}`` (a temp file holding
the brief), ``{room}``, ``{thread_id}``. If none appear, the brief is also piped
on stdin and exported as ``$ROOK_WAKE_PROMPT``. The agent is spawned detached —
we don't wait for it to finish (it may run for minutes); ``agent.wake`` returns
as soon as it's launched. Set ``ROOK_WAKE_AGENT`` to the identity this host wakes
(e.g. ``agent:hermes_sojourn``) so an already-attending agent can be reported.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..plugin import Plugin, capability

_STATE = Path(os.path.expanduser("~")) / ".rook-band-worker" / "wake-last.txt"
# Don't respawn the same agent within this window — a cheap guard against a
# wake storm (the design's "black-hole a wake for an already-attending agent").
_REWAKE_SECS = 45.0


def _brief(room: str, thread_id: str, title: str, transcript: list,
           woken_by: str, note: str) -> str:
    """The prompt handed to the spawned agent. Plain, explicit instructions —
    it wakes into this cold, so it must learn from the text alone what it is,
    where it is, and how to reply."""
    lines = [
        f"You have been woken into rook chat room '{title or room}' "
        f"(room id: {room}) by {woken_by or 'someone'}.",
        "",
        "You are an agent on the rook band. Read the conversation below and "
        "respond in the room. To reply, call the rook chat tool/capability "
        f"`chat.send` (or MCP tool `rook_chat_send`) with room=\"{room}\". "
        "Only respond when you have something useful to add; if nothing is "
        "needed, you may simply note that and stop.",
    ]
    if note:
        lines += ["", f"Note from {woken_by or 'the sender'}: {note}"]
    lines += ["", "--- conversation so far ---"]
    for m in transcript or []:
        who = m.get("sender", "?")
        lines.append(f"[{who}] {m.get('text','')}")
    lines += ["--- end conversation ---", ""]
    return "\n".join(lines)


class WakePlugin(Plugin):
    NAMESPACE = "agent"

    def available(self) -> bool:
        return bool(os.environ.get("ROOK_WAKE_CMD"))

    @capability("wake")
    def _wake(self, room: str, thread_id: str = "", title: str = "",
              transcript: list | None = None, woken_by: str = "",
              note: str = "") -> dict:
        """Spawn the configured local agent to attend ``room``. Returns once the
        agent is launched (it runs detached). Declines if the same agent was
        woken moments ago (already attending)."""
        cmd_tmpl = os.environ.get("ROOK_WAKE_CMD", "").strip()
        if not cmd_tmpl:
            return {"ok": False, "error": "ROOK_WAKE_CMD not set on this host"}

        # Black-hole a re-wake of an already-attending agent.
        now = time.time()
        try:
            last = float(_STATE.read_text().split(":", 1)[0]) if _STATE.exists() else 0.0
        except Exception:
            last = 0.0
        last_room = ""
        try:
            last_room = _STATE.read_text().split(":", 1)[1].strip() if _STATE.exists() else ""
        except Exception:
            pass
        if now - last < _REWAKE_SECS and last_room == room:
            return {"ok": True, "action": "already-attending",
                    "note": "agent was woken for this room moments ago; not respawning"}

        brief = _brief(room, thread_id or room, title, transcript or [],
                       woken_by, note)
        try:
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            fd, pf = tempfile.mkstemp(prefix="rook-wake-", suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(brief)
        except Exception as e:
            return {"ok": False, "error": f"could not write brief: {e}"}

        substituted = ("{prompt_file}" in cmd_tmpl or "{room}" in cmd_tmpl
                       or "{thread_id}" in cmd_tmpl)
        cmd_str = (cmd_tmpl.replace("{prompt_file}", pf)
                          .replace("{room}", room)
                          .replace("{thread_id}", thread_id or room))
        try:
            argv = shlex.split(cmd_str, posix=(sys.platform != "win32"))
        except Exception as e:
            return {"ok": False, "error": f"bad ROOK_WAKE_CMD: {e}"}

        env = dict(os.environ)
        env["ROOK_WAKE_PROMPT"] = brief
        env["ROOK_WAKE_ROOM"] = room
        env["ROOK_WAKE_PROMPT_FILE"] = pf
        stdin_src = None if substituted else subprocess.PIPE
        try:
            # Detached: the agent may run for minutes; we return on launch.
            kwargs = dict(env=env, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, stdin=stdin_src)
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **kwargs)
            if stdin_src is not None and proc.stdin:
                try:
                    proc.stdin.write(brief.encode("utf-8"))
                    proc.stdin.close()
                except Exception:
                    pass
            _STATE.write_text(f"{now}:{room}")
        except FileNotFoundError:
            return {"ok": False, "error": f"wake command not found: {argv[0]!r}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "action": "woken", "pid": proc.pid,
                "agent": os.environ.get("ROOK_WAKE_AGENT") or None,
                "room": room, "prompt_file": pf}

    @capability("wake_info")
    def _wake_info(self) -> dict:
        """Report whether this host can wake an agent and which one."""
        return {"ok": True, "can_wake": bool(os.environ.get("ROOK_WAKE_CMD")),
                "agent": os.environ.get("ROOK_WAKE_AGENT") or None,
                "cmd_set": bool(os.environ.get("ROOK_WAKE_CMD"))}


PLUGIN = WakePlugin
