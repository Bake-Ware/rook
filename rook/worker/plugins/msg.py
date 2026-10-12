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
import time

from ...core.pluginkit import has_display, is_windows, opt_int, require_str, state_path, which
from ..plugin import Plugin, capability

_INBOX = state_path("messages.jsonl")
_MAX_KEEP = 500      # trim the inbox to the most recent N on write


async def _notify(title: str, body: str) -> bool:
    """Best-effort desktop notification; never raises."""
    if is_windows():
        return await _notify_windows(title, body)
    if not has_display():
        return False
    exe = which("notify-send")
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


async def _notify_windows(title: str, body: str) -> bool:
    """Balloon-tip notification via .NET NotifyIcon (no extra deps — ships
    with every Windows install). The icon has to stay alive for the balloon
    to render, so the script sleeps briefly before disposing it."""
    ps = which("powershell", "pwsh")
    if not ps:
        return False

    def esc(s: str) -> str:
        return s.replace("'", "''")

    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.BalloonTipTitle = '{esc(title)}'; "
        f"$n.BalloonTipText = '{esc(body)}'; "
        "$n.ShowBalloonTip(5000); "
        "Start-Sleep -Seconds 4; "
        "$n.Dispose()"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            ps, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 8)
        return proc.returncode == 0
    except Exception:
        return False


class MsgPlugin(Plugin):
    NAMESPACE = "msg"

    @capability("send")
    async def _send(self, text: str, sender: str = "operator") -> dict:
        """Deliver a text message to this worker: store it in the inbox and try a
        desktop notification. ``sender`` labels who it's from."""
        text = require_str(text, "text", strip=False)
        entry = {"ts": int(time.time()), "sender": str(sender or "band")[:64],
                 "text": text[:4000]}
        try:
            _INBOX.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            if _INBOX.exists():
                lines = _INBOX.read_text(encoding="utf-8", errors="replace").splitlines()[-(_MAX_KEEP - 1):]
            lines.append(json.dumps(entry, ensure_ascii=False))
            _INBOX.write_text("\n".join(lines) + "\n", encoding="utf-8")
            stored = True
        except Exception as e:
            stored = False
            entry["store_error"] = f"{type(e).__name__}: {e}"
        notified = await _notify(f"rook · {entry['sender']}", text[:200])
        return {"ok": True, "stored": stored, "notified": notified}

    @capability("read")
    def _read(self, limit: int = 20) -> dict:
        """Return the most recent messages from this worker's inbox."""
        limit = opt_int(limit, "limit", 20, min=1, max=_MAX_KEEP)
        if not _INBOX.exists():
            return {"ok": True, "messages": []}
        out = []
        for ln in _INBOX.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return {"ok": True, "messages": out}

    @capability("clear")
    def _clear(self) -> dict:
        """Empty this worker's inbox."""
        _INBOX.unlink(missing_ok=True)
        return {"ok": True, "cleared": True}


PLUGIN = MsgPlugin
