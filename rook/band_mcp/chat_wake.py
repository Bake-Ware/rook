"""Make an agent answer in a chat room now: the wake path ``rook_chat_wake``
uses, as a function other hub code (the huddle plugin) can call.

Two routes, picked by the target worker's caps:

* ``hermes.chat``: hand the room's transcript to hermes and post its reply
  into the room as ``agent:hermes_<worker>``;
* ``agent.wake``: ask the worker to start a fresh agent session with the
  transcript as its brief (it posts in the room itself).

A worker with neither cannot be woken; the agent still gets the unread
notice on its next MCP call. Mirrors the MCP tool and the dashboard's
``/api/chat/wake`` (rook/band_mcp/server.py, rook/remote/bootstrap.py),
which keep their own copies for now.
"""
from __future__ import annotations

import re
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BOX = "─│╱╲╳┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬▀▄█▌▐░▒▓⚕☤┊⚡•·"
_FOOTER = ("resume this session", "session:", "duration:", "messages:", "hermes --resume")


def clean_hermes_stdout(text: str) -> str:
    """Hermes's reply from its TUI stdout: no ANSI or box drawing, the lines
    between the ``Hermes`` banner and the session footer."""
    lines = []
    for raw in _ANSI.sub("", str(text or "")).replace("\r", "\n").split("\n"):
        ln = "".join(c for c in raw if c not in _BOX).strip()
        if ln:
            lines.append(ln)
    banner = next((i for i, ln in enumerate(lines) if ln.lower() == "hermes"), -1)
    if banner >= 0:
        body = []
        for ln in lines[banner + 1:]:
            if ln.lower().startswith(_FOOTER):
                break
            body.append(ln)
        return " ".join(body).strip()
    kept = [ln for ln in lines if not ln.lower().startswith(("query:", "initializing agent") + _FOOTER)]
    return " ".join(kept).strip()


def wake_prompt(title: str, convo: str, note: str) -> str:
    """The brief for hermes: its reply text IS the message, so it must not
    go off and use rook chat tools itself."""
    note_line = f"\nNote from the person waking you: {note}" if note else ""
    return (f"You are a participant in the rook chat room '{title}'. Someone in the room is waiting "
            f"for your reply.{note_line}\n\nRespond to the conversation below as yourself, in plain prose "
            f"addressed to the room. Whatever you write back is posted into this room for you "
            f"automatically: do NOT call any rook chat tools (rook_chat_start/send/read/wake, chat.*), "
            f"do not create rooms, and do not describe what you did; just answer.\n\n"
            f"--- conversation ---\n{convo}\n--- end ---")


def find_worker(roster: dict, identity: str, hint: str | None = None) -> str | None:
    """The live worker an agent identity runs on: ``hint`` (a worker name or
    id the agent gave), else the identity's own name parts. Chat identities
    are ``agent:<token>_<host>`` (``X-Rook-Host``), ``agent:hermes_<worker>``
    for a hermes box and ``agent:<worker>`` for an ``agent.wake`` worker, so
    any ``_``-separated tail that names a worker counts."""
    names = {(w.get("name") or "").lower(): wid for wid, w in roster.items() if w.get("name")}
    if hint:
        if hint in roster:
            return hint
        if hint.lower() in names:
            return names[hint.lower()]
    name = str(identity or "").split(":", 1)[-1].lower()
    parts = name.split("_")
    for i in range(len(parts)):
        tail = "_".join(parts[i:])
        if tail in names:
            return names[tail]
    return None


async def wake(client: Any, chat: Any, room: str, worker_id: str, identity: str,
               note: str = "", timeout: float = 20.0) -> dict:
    """Wake the agent on ``worker_id`` into ``room``. Returns ``{ok, via,
    worker, error?}``; never raises for a worker that refuses or times out."""
    w = client.workers.get(worker_id) or {}
    caps = w.get("caps", []) or []
    wname = w.get("name") or worker_id
    tail = chat.read(room, None, since_seq=0, mark=False, limit=30)
    if not tail.get("ok"):
        return {"ok": False, "worker": wname, "error": tail.get("error") or "no such room"}
    transcript = tail.get("messages", [])
    try:
        if "hermes.chat" in caps:
            convo = "\n".join(f"[{m.get('sender')}] {m.get('text')}" for m in transcript)
            reply = await client.call(cap="hermes.chat",
                                      args={"message": wake_prompt(tail.get("title") or room, convo, note)},
                                      target=worker_id, timeout=max(timeout, 120.0), identity=identity)
            res = reply.get("result") or {}
            answer = clean_hermes_stdout(res.get("stdout", "")) if isinstance(res, dict) else ""
            if not answer:
                return {"ok": False, "via": "hermes.chat", "worker": wname,
                        "error": "hermes returned no reply"}
            chat.send(room, f"agent:hermes_{wname}", answer, [], False)
            return {"ok": True, "via": "hermes.chat", "worker": wname}
        if "agent.wake" in caps:
            reply = await client.call(cap="agent.wake", target=worker_id, timeout=timeout, identity=identity,
                                      args={"room": room, "thread_id": room, "title": tail.get("title"),
                                            "transcript": transcript, "woken_by": identity, "note": note})
            ok = bool(reply.get("ok")) and (reply.get("result") or {}).get("ok", True) is not False
            return {"ok": ok, "via": "agent.wake", "worker": wname,
                    **({} if ok else {"error": reply.get("error") or "agent.wake refused"})}
    except Exception as e:  # noqa: BLE001 - a timeout or a dead worker is an outcome, not a crash
        return {"ok": False, "worker": wname, "error": f"{type(e).__name__}: {e}"}
    return {"ok": False, "worker": wname, "error": f"{wname} exposes neither hermes.chat nor agent.wake"}
