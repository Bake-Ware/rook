"""The band's Telegram bot: direct messages to linked people, posts to chats,
link codes, and inbound messages as tainted ``telegram.message`` events.

One long-poll loop (``getUpdates``) runs from the start hook until stop.
Links are ``link`` kit records (the person, their Telegram user, chat and
name), made only when someone sends a valid ``/link CODE`` to the bot in a
direct message. The poll offset is a cursor in the plugin's data dir. Error messages never carry the token (it is in every URL).
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import secrets
import time

import aiohttp

from rook.kit.ctx import Caller, KitError

LIMIT = 4096                   # Telegram's longest message
CODE_TTL = 600.0
LINK = re.compile(r"/link(?:@\w+)?\s+([0-9a-f]{8})", re.I)

# Shared by caps and hooks (one module per plugin load).
_state: dict = {"task": None, "bot": None, "connected": False, "error": None}
_codes: dict = {}              # code -> (person, expires)


# -- caps -----------------------------------------------------------------------

async def send(ctx, to, text, title=None):
    person = _person(to)
    link = _link(ctx, person)
    if not link:
        raise KitError(f"{_name(person)} hasn't linked Telegram (telegram.link)")
    msg = await _api(ctx, "sendMessage", {"chat_id": link["chat"], "text": _text(text, title),
                                          "disable_web_page_preview": True})
    return {"to": person, "message": str(msg.get("message_id", ""))}


async def post(ctx, chat, text, reply_to=None):
    body = {"chat_id": chat, "text": _text(text), "disable_web_page_preview": True}
    if reply_to:
        body["reply_parameters"] = {"message_id": int(reply_to) if reply_to.isdigit() else reply_to,
                                    "allow_sending_without_reply": True}
    msg = await _api(ctx, "sendMessage", body)
    return {"chat": chat, "message": str(msg.get("message_id", ""))}


async def link(ctx):
    if not ctx.person:
        raise KitError("link codes are for a person (call as or for one)")
    for code in [c for c, (p, _) in _codes.items() if p == ctx.person]:
        del _codes[code]                               # one live code per person
    code = secrets.token_hex(4)
    _codes[code] = (ctx.person, time.time() + CODE_TTL)
    bot = _state["bot"] or (await _api(ctx, "getMe")).get("username")
    return {"code": code, "expires_in": int(CODE_TTL),
            "how": f"Send /link {code} to @{bot} in a direct message." if bot else f"Send /link {code} to the bot."}


async def unlink(ctx, to=None):
    person = _person(to) if to else ctx.person
    if person != ctx.person and not ctx.caller.owner:
        raise KitError("denied: only an owner unlinks someone else")
    link = _link(ctx, person)
    if link:
        _book(ctx).delete(link["id"])
    return {"person": person, "unlinked": link is not None}


async def status(ctx, to=None):
    person = _person(to) if to else ctx.person
    out = {"connected": _state["connected"], "bot": _state["bot"], "error": _state["error"]}
    if person:
        out["linked"] = _link(ctx, person) is not None
    return out


# -- hooks ----------------------------------------------------------------------

async def start(ctx):
    _state.update(task=asyncio.get_running_loop().create_task(_poll(ctx)), connected=False, error=None)


async def stop(ctx):
    task = _state.get("task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _state.update(task=None, connected=False)


async def _poll(ctx):
    offset, backoff = _load_offset(ctx), 1.0
    wait = int(ctx.settings["poll"])
    while True:
        try:
            if not _state["bot"]:
                _state["bot"] = (await _api(ctx, "getMe")).get("username")
            updates = await _api(ctx, "getUpdates", {"offset": offset, "timeout": wait,
                                                     "allowed_updates": ["message"]}, timeout=wait + 15)
            _state.update(connected=True, error=None)
            backoff = 1.0
            for u in updates or []:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                if isinstance(u.get("message"), dict):
                    await _inbound(ctx, u["message"])
            if updates:
                _save_offset(ctx, offset)
        except asyncio.CancelledError:
            raise
        except Exception as e:      # the loop outlives a bad update or a dropped connection
            _state.update(connected=False, error=getattr(e, "message", None) or type(e).__name__)
            ctx.log.warning("telegram poll failed: %s", _state["error"])
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _inbound(ctx, msg: dict) -> None:
    frm, chat = msg.get("from") or {}, msg.get("chat") or {}
    if frm.get("is_bot"):
        return
    text = str(msg.get("text") or msg.get("caption") or "")
    private = chat.get("type") == "private"
    m = LINK.fullmatch(text.strip())
    if m and private:
        person = _redeem(m.group(1).lower())
        if person:
            _linked(ctx, person, str(frm.get("id")), str(chat.get("id")), _who(frm))
        reply = f"Linked: you are {_name(person)} on Rook." if person else "That code is wrong or has expired."
        await _api(ctx, "sendMessage", {"chat_id": chat.get("id"), "text": reply})
        return
    user = str(frm.get("id", ""))
    person = _person_of(ctx, user)
    ctx.emit("telegram.message", {"text": text, "chat": str(chat.get("id", "")), "private": private,
                                  "from": _who(frm), "from_id": user, "message": str(msg.get("message_id", "")),
                                  **({"person": person} if person else {})})


# -- helpers --------------------------------------------------------------------

async def _api(ctx, method: str, payload: dict | None = None, timeout: float = 30) -> dict:
    url = f"{str(ctx.settings['api']).rstrip('/')}/bot{ctx.settings['token']}/{method}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(url, json=payload or {}) as r:
                status = r.status
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:     # no str(e): it may hold the URL
        raise KitError(f"telegram {method}: {type(e).__name__}", retry=True) from None
    if isinstance(data, dict) and data.get("ok"):
        return data.get("result")
    data = data if isinstance(data, dict) else {}
    code = data.get("error_code") or status
    why = str(data.get("description") or f"HTTP {status}")[:200]
    raise KitError(f"telegram {method}: {why}", retry=code == 429 or code >= 500)


def _text(text: str, title: str | None = None) -> str:
    out = f"{title}\n{text}" if title else text
    return out if len(out) <= LIMIT else out[:LIMIT - 1] + "…"


def _redeem(code: str) -> str | None:
    now = time.time()
    for c in [c for c, (_, exp) in _codes.items() if exp < now]:
        del _codes[c]
    person, _ = _codes.pop(code, (None, 0))
    return person


def _book(ctx, person: str | None = None):
    """The ``link`` records (one per linked person), as the plugin: every row.
    With ``person``, as that person, so the record they make is theirs."""
    c = copy.copy(ctx)
    c.caller = Caller(person=person, band=ctx.band, owner=person is None, plugin=ctx.plugin)
    return c.records.link


def _link(ctx, person: str) -> dict | None:
    return next((r for r in _book(ctx).list(limit=500) if r["person"] == person), None)


def _linked(ctx, person: str, user: str, chat: str, name: str) -> None:
    """``person`` redeemed a code from ``user``: one link per person and per account."""
    book = _book(ctx)
    for r in book.list(limit=500):
        if r["person"] == person or r["user"] == user:
            book.delete(r["id"])
    _book(ctx, person).create(user=user, chat=chat, name=name)


def _person_of(ctx, user: str) -> str | None:
    rows = _book(ctx).list(where={"user": user}, limit=1)
    return rows[0]["person"] if rows else None


def _load_offset(ctx) -> int:
    try:
        return int(json.loads((ctx.data / "offset.json").read_text(encoding="utf-8"))["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _save_offset(ctx, offset: int) -> None:
    _write(ctx.data / "offset.json", json.dumps({"offset": offset}))


def _write(path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _who(frm: dict) -> str:
    return str(frm.get("username") or frm.get("first_name") or frm.get("id", ""))


def _person(name: str) -> str:
    name = name.strip()
    return name if name.startswith("person:") else f"person:{name}"


def _name(person: str | None) -> str:
    return (person or "").removeprefix("person:")
