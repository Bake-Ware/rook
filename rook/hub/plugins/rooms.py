"""``chat.*`` on worker ``rook``: the hub's persistent chat rooms over the band.

The rooms themselves live in the MCP bridge's :class:`ChatStore`
(``chat.db`` beside the journal); the ``rook_chat_*`` MCP tools use it
directly. This plugin puts the same rooms on the band, so a worker or an app
written in any language can take part without an MCP client
(``docs/spec/core-v1.md``, "Chat rooms"):

* ``chat.read`` (risk ``read``): ``action=rooms`` (your rooms, unread counts)
  or ``action=read`` (messages after ``since_seq``).
* ``chat.write`` (risk ``write``): ``action=start`` (new room) or
  ``action=send`` (post; mentions route and auto-invite).
* ``chat.delete`` (risk ``write``, destructive): participants only.
* ``chat.presence`` (risk ``read``): who has been seen recently.

Names follow the permissions tier table (Appendix A.2), which already reserves
them for the hub. Worker chat caps (``chat.open``/``send``/``rooms``/``poll``,
the local-human transcript plugin) share the namespace but no names, so a
targeted call is never ambiguous.

Attribution. A band caller is unauthenticated (any PSK holder can stamp any
``identity``), so its identity is recorded as ``band:<identity>``: a band
peer can never post as ``human:*`` or as a token-attributed agent. In-process
callers with a verified principal (the MCP bridge) keep their identity. Band
calls are also subject to the hub's band risk ceiling
(``ROOK_HUB_BAND_MAX_RISK``, default ``read``): posting over the band needs
the operator to raise it to ``write``.
"""

from __future__ import annotations

import logging
import os

from ...core.context import current_identity
from ...core.plugin import Plugin, capability, place

log = logging.getLogger("rook.hub.plugins.rooms")

BAND_PREFIX = "band:"


def _band_caller() -> bool:
    """Whether the call being handled came in over the band (unauthenticated)."""
    from ..authz import current_principal
    p = current_principal.get()
    return p is not None and p.kind == "band"


def caller() -> str:
    """The identity chat records for the current caller."""
    ident = (current_identity() or "").strip()
    if _band_caller():
        ident = ident or "anonymous"
        return ident if ident.startswith(BAND_PREFIX) else BAND_PREFIX + ident
    return ident or "system:rook-hub"


def _names(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        raise TypeError("expected a list of identities or a comma-separated string")
    return [str(v).strip() for v in value if str(v).strip()]


class ChatRooms(Plugin):
    NAMESPACE = "chat"
    NAME = "chat-rooms"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    GUIDANCE = {"cap:chat.": ("chat.read/chat.write on worker 'rook' are the band side of the "
                              "rook_chat_* tools; band callers post as band:<identity>.")}
    SKILL = ("### chat rooms\n"
             "Persistent rooms shared by agents, people and workers. MCP: `rook_chat_*`. Over the "
             "band, on worker `rook`: `chat.read` (action rooms|read), `chat.write` (action "
             "start|send), `chat.delete`, `chat.presence`. Band callers are recorded as "
             "`band:<identity>`; posting over the band needs `ROOK_HUB_BAND_MAX_RISK=write`.\n")

    def __init__(self) -> None:
        super().__init__()
        self._store = None
        self._owned = False

    def bind_host(self, node) -> None:
        """Use the bridge's chat store if the node carries one; otherwise open
        ``chat.db`` in the hub state dir (the same file the bridge uses)."""
        store = getattr(node, "chat", None)
        if store is None:
            state = getattr(node, "_state_dir", None)
            if state:
                from ...band_mcp.chat_rooms import ChatStore
                store = ChatStore(os.path.join(state, "chat.db"))
                self._owned = True
        self._store = store

    async def stop(self) -> None:
        if self._owned and self._store is not None:
            self._store.close()
        self._store = None

    def _chat(self):
        if self._store is None or not getattr(self._store, "enabled", False):
            raise ValueError("the chat store is not open on this hub")
        return self._store

    @staticmethod
    def _checked(reply: dict) -> dict:
        if not reply.get("ok", True):
            raise ValueError(reply.get("error") or "chat operation failed")
        return {k: v for k, v in reply.items() if k != "ok"}

    @capability("read", risk="read")
    def read(self, action: str = "rooms", room: str | None = None, since_seq: int = 0,
             limit: int = 200, mark: bool = True) -> dict:
        """Read chat rooms: action=rooms (yours, newest first, unread counts) or read (room, since_seq).

        read returns messages with seq > since_seq (up to limit) and marks
        them read for you unless mark=false; pass the reply's last_seq next time."""
        store, me = self._chat(), caller()
        if action == "rooms":
            store.touch(me)
            return self._checked(store.rooms_for(me, limit=max(1, min(int(limit), 200))))
        if action == "read":
            if not room:
                raise ValueError("read needs room")
            store.touch(me)
            return self._checked(store.read(room, me, since_seq=int(since_seq),
                                            mark=bool(mark), limit=int(limit)))
        raise ValueError("action must be rooms or read")

    @capability("write", risk="write")
    def write(self, action: str, room: str | None = None, text: str | None = None,
              title: str | None = None, invite=None, mentions=None,
              expects_reply: bool = False) -> dict:
        """Write chat rooms: action=start (title, invite) or send (room, text, mentions).

        Mentioning a non-participant invites them; in a two-party room the other
        party is addressed implicitly. The reply lists who is offline."""
        store, me = self._chat(), caller()
        store.touch(me)
        if action == "start":
            return self._checked(store.start(title or "chat", me, _names(invite)))
        if action == "send":
            if not room:
                raise ValueError("send needs room")
            return self._checked(store.send(room, me, text or "", _names(mentions),
                                            bool(expects_reply)))
        raise ValueError("action must be start or send")

    @capability("delete", risk="write", tags=("destructive",))
    def delete(self, room: str) -> dict:
        """Delete a room and all its messages (participants only; final)."""
        store, me = self._chat(), caller()
        return self._checked(store.delete(room, me))

    @capability("presence", risk="read")
    def presence(self) -> dict:
        """Identities seen recently, newest first, with online flags."""
        store = self._chat()
        store.touch(caller())
        return {"agents": store.online()}


PLUGIN = ChatRooms
