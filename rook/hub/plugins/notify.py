"""``notify.send``: one notification cap over every chat integration.

Plugins, agents and the watchdog call ``notify.send`` on worker ``rook``
instead of talking to Telegram or Discord themselves; it posts to each
running chat integration (every loaded :class:`ChatIntegration` plugin, by
namespace: ``telegram``, ``discord``, or a third-party one) or the one named
by ``channel``. With ``user`` it goes to that person's linked accounts
instead (direct messages, from their own bot where they run one). With no
integration configured it returns ``ok: false`` so a caller can fall back to
its own channel. See docs/integrations.md.
"""

from __future__ import annotations

from typing import Any

from ...core.plugin import Plugin, capability, place
from ..integrations import ChatIntegration, person_refusal


class Notify(Plugin):
    NAMESPACE = "notify"
    NAME = "notify"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### notify\n"
             "`rook_call(worker=\"rook\", cap=\"notify.send\", args={\"text\": \"...\"})` "
             "posts a notification to every configured chat integration (Telegram, "
             "Discord); `channel=\"telegram\"` picks one; `user=\"<account id>\"` messages "
             "that person's linked accounts directly (yourself; owners: anyone).\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None

    def bind_host(self, node: Any) -> None:
        self._node = node

    def _channels(self) -> dict[str, Any]:
        """Running chat integrations by namespace, discovered from the host
        (so one added by an entry point is a channel too)."""
        node = self._node
        if node is None:
            return {}
        return {p.NAMESPACE: p for p in node.host.plugins if isinstance(p, ChatIntegration)}

    @capability("send", risk="write", scope="external")
    async def send(self, text: str, channel: str = "all", user: str | None = None) -> dict:
        """Send a notification to the chat integrations.

        ``channel`` is ``all`` (default: every running integration) or one
        integration's name (``telegram``, ``discord``; ``notify.channels``
        lists them). ``user``: a person's account id; the message goes to
        their linked accounts as direct messages (yourself; owners may
        message anyone). ``ok`` is true if at least one channel took it;
        ``sent`` and ``errors`` say which."""
        if user:
            refusal = person_refusal(user)
            if refusal:
                return {"ok": False, "error": refusal}
        live = self._channels()
        if channel != "all" and channel not in live:
            return {"ok": False, "error": (f"no chat integration is running for {channel}"
                                           if live else "no chat integration is running")}
        targets = live if channel == "all" else {k: v for k, v in live.items() if k == channel}
        if not targets:
            return {"ok": False, "error": ("no chat integration is running"
                                           + ("" if channel == "all" else f" for {channel}"))}
        sent, errors = [], {}
        for name, plugin in targets.items():
            res = await (plugin.notify_person(user, text) if user else plugin.notify(text))
            if res.get("ok"):
                sent.append(name)
            else:
                errors[name] = res.get("error")
        out: dict = {"ok": bool(sent), "sent": sent}
        if errors:
            out["errors"] = errors
        return out

    @capability("channels", risk="read", scope="multi-user")
    def channels(self) -> dict:
        """Which notification channels are running on this hub."""
        return {"channels": sorted(self._channels())}


PLUGIN = Notify
