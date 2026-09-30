"""``notify.send``: one notification cap over every chat integration.

Plugins, agents and the watchdog call ``notify.send`` on worker ``rook``
instead of talking to Telegram or Discord themselves; it posts to each
running integration (``telegram``, ``discord``) or the one named by
``channel``. With no integration configured it returns ``ok: false`` so a
caller can fall back to its own channel. See docs/integrations.md.
"""

from __future__ import annotations

from typing import Any

from ...core.plugin import Plugin, capability, place

CHANNELS = ("telegram", "discord")


class Notify(Plugin):
    NAMESPACE = "notify"
    NAME = "notify"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### notify\n"
             "`rook_call(worker=\"rook\", cap=\"notify.send\", args={\"text\": \"...\"})` "
             "posts a notification to every configured chat integration (Telegram, "
             "Discord); `channel=\"telegram\"` picks one.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None

    def bind_host(self, node: Any) -> None:
        self._node = node

    def _channels(self) -> dict[str, Any]:
        node = self._node
        if node is None:
            return {}
        return {c: p for c in CHANNELS if (p := node.plugin(c)) is not None}

    @capability("send", risk="write")
    async def send(self, text: str, channel: str = "all") -> dict:
        """Send a notification to the chat integrations.

        ``channel`` is ``all`` (default: every running integration),
        ``telegram`` or ``discord``. ``ok`` is true if at least one channel
        took it; ``sent`` and ``errors`` say which."""
        if channel != "all" and channel not in CHANNELS:
            return {"ok": False, "error": f"channel must be all or one of {list(CHANNELS)}"}
        live = self._channels()
        targets = live if channel == "all" else {k: v for k, v in live.items() if k == channel}
        if not targets:
            return {"ok": False, "error": ("no chat integration is running"
                                           + ("" if channel == "all" else f" for {channel}"))}
        sent, errors = [], {}
        for name, plugin in targets.items():
            res = await plugin.notify(text)
            if res.get("ok"):
                sent.append(name)
            else:
                errors[name] = res.get("error")
        out: dict = {"ok": bool(sent), "sent": sent}
        if errors:
            out["errors"] = errors
        return out

    @capability("channels", risk="read")
    def channels(self) -> dict:
        """Which notification channels are running on this hub."""
        return {"channels": sorted(self._channels())}


PLUGIN = Notify
