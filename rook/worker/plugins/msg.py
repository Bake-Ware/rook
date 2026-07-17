"""msg.* — lightweight text messaging to a worker.

Send a short text to any worker on the band (operator→worker, or worker→worker):
it's appended to a small on-disk inbox and, best-effort, surfaced as a desktop
notification (notify-send) when a graphical session is present. Works on every
worker — no agent/chat backend required — so it complements the richer
``hermes.chat`` conversational path.

    msg.send(text, sender?)  -> {ok, stored, notified}
    msg.read(limit=20)       -> {ok, messages:[{ts, sender, text}]}
    msg.clear()              -> {ok, cleared}
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from ..plugin import Plugin, capability

_INBOX = Path(os.path.expanduser("~")) / ".rook-band-worker" / "messages.jsonl"
_MAX_KEEP = 500      # trim the inbox to the most recent N on write


async def _notify(title: str, body: str) -> bool:
    """Best-effort desktop notification; never raises."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    exe = shutil.which("notify-send")
    if not exe:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "-a", "rook", title, body,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 5)
        return proc.returncode == 0
    except Exception:
        return False


class MsgPlugin(Plugin):
    NAMESPACE = "msg"

    @capability("send")
    async def _send(self, text: str, sender: str = "band") -> dict:
        """Deliver a text message to this worker: store it in the inbox and try a
        desktop notification. ``sender`` labels who it's from."""
        text = str(text or "")
        if not text.strip():
            return {"ok": False, "error": "empty message"}
        entry = {"ts": int(time.time()), "sender": str(sender or "band")[:64],
                 "text": text[:4000]}
        try:
            _INBOX.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            if _INBOX.exists():
                lines = _INBOX.read_text().splitlines()[-(_MAX_KEEP - 1):]
            lines.append(json.dumps(entry))
            _INBOX.write_text("\n".join(lines) + "\n")
            stored = True
        except Exception as e:
            stored = False
            entry["store_error"] = f"{type(e).__name__}: {e}"
        notified = await _notify(f"rook · {entry['sender']}", text[:200])
        return {"ok": True, "stored": stored, "notified": notified}

    @capability("read")
    def _read(self, limit: int = 20) -> dict:
        """Return the most recent messages from this worker's inbox."""
        if not _INBOX.exists():
            return {"ok": True, "messages": []}
        out = []
        try:
            for ln in _INBOX.read_text().splitlines()[-int(limit):]:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "messages": out}

    @capability("clear")
    def _clear(self) -> dict:
        """Empty this worker's inbox."""
        try:
            _INBOX.unlink(missing_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "cleared": True}


PLUGIN = MsgPlugin
