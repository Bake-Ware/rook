"""The band's Discord bot: direct messages to linked people, posts to
channels, link codes, and inbound messages as tainted ``discord.message``
events.

One Gateway connection runs from the start hook until stop: hello, identify,
heartbeat on the server's interval, MESSAGE_CREATE in; any close or error
reconnects with backoff (a fresh identify; no resume). Links are ``link``
kit records (the person, their Discord user, DM channel and name), made only
when someone sends a valid ``!link CODE`` in a direct message.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import secrets
import time

import aiohttp

from rook.kit.ctx import Caller, KitError

LIMIT = 2000                   # Discord's longest message
CODE_TTL = 600.0
LINK = re.compile(r"!link\s+([0-9a-f]{8})", re.I)
INTENTS = (1 << 9) | (1 << 12) | (1 << 15)      # GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
FATAL = {4004, 4010, 4011, 4012, 4013, 4014}    # bad token, intents: retrying soon won't help
AGENT = "DiscordBot (https://github.com/Bake-Ware/rook, 0.5)"

_state: dict = {"task": None, "bot": None, "bot_id": None, "connected": False, "error": None}
_codes: dict = {}              # code -> (person, expires)


# -- caps -----------------------------------------------------------------------

async def send(ctx, to, text, title=None):
    person = _person(to)
    link = _link(ctx, person)
    if not link:
        raise KitError(f"{_name(person)} hasn't linked Discord (discord.link)")
    if not link.get("chat"):
        chat = str((await _api(ctx, "POST", "/users/@me/channels", {"recipient_id": link["user"]}))["id"])
        link = _book(ctx).update(link["id"], chat=chat)
    msg = await _api(ctx, "POST", f"/channels/{link['chat']}/messages", {"content": _text(text, title)})
    return {"to": person, "message": str(msg.get("id", ""))}


async def post(ctx, chat, text, reply_to=None):
    body = {"content": _text(text)}
    if reply_to:
        body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
    msg = await _api(ctx, "POST", f"/channels/{_snowflake(chat)}/messages", body)
    return {"chat": chat, "message": str(msg.get("id", ""))}


async def link(ctx):
    if not ctx.person:
        raise KitError("link codes are for a person (call as or for one)")
    for code in [c for c, (p, _) in _codes.items() if p == ctx.person]:
        del _codes[code]
    code = secrets.token_hex(4)
    _codes[code] = (ctx.person, time.time() + CODE_TTL)
    bot = _state["bot"]
    return {"code": code, "expires_in": int(CODE_TTL),
            "how": f"Send !link {code} to {bot or 'the bot'} in a direct message."}


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
    _state.update(task=asyncio.get_running_loop().create_task(_run(ctx)), connected=False, error=None)


async def stop(ctx):
    task = _state.get("task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _state.update(task=None, connected=False)


async def _run(ctx):
    backoff = 1.0
    while True:
        try:
            code = await _gateway(ctx)
            if _state["connected"]:
                backoff = 1.0
            if code in FATAL:
                _state["error"] = f"gateway closed {code} (bad token or intents not allowed)"
                backoff = 300.0
        except asyncio.CancelledError:
            raise
        except Exception as e:      # the loop outlives a dropped connection or a bad event
            _state["error"] = getattr(e, "message", None) or type(e).__name__
        _state["connected"] = False
        ctx.log.warning("discord gateway down: %s", _state["error"])
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300.0)


async def _gateway(ctx) -> int | None:
    """One Gateway connection, until it closes; returns the close code."""
    url = (await _api(ctx, "GET", "/gateway/bot"))["url"]
    seq: dict = {"s": None}
    async with aiohttp.ClientSession() as s, s.ws_connect(f"{url}/?v=10&encoding=json", max_msg_size=0) as ws:
        hello = await ws.receive_json(timeout=30)
        interval = float(hello["d"]["heartbeat_interval"]) / 1000
        await ws.send_json({"op": 2, "d": {"token": ctx.settings["token"], "intents": INTENTS,
                                           "properties": {"os": "linux", "browser": "rook", "device": "rook"}}})
        beat = asyncio.get_running_loop().create_task(_heartbeat(ws, interval, seq))
        try:
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                ev = json.loads(m.data)
                if ev.get("s") is not None:
                    seq["s"] = ev["s"]
                op = ev.get("op")
                if op == 0 and ev.get("t") == "READY":
                    me = ev["d"].get("user") or {}
                    _state.update(connected=True, error=None, bot=me.get("username"), bot_id=str(me.get("id")))
                elif op == 0 and ev.get("t") == "MESSAGE_CREATE":
                    await _inbound(ctx, ev.get("d") or {})
                elif op == 1:
                    await ws.send_json({"op": 1, "d": seq["s"]})
                elif op in (7, 9):          # reconnect / invalid session: start over
                    break
        finally:
            beat.cancel()
        return ws.close_code


async def _heartbeat(ws, interval: float, seq: dict) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send_json({"op": 1, "d": seq["s"]})


async def _inbound(ctx, d: dict) -> None:
    author = d.get("author") or {}
    if author.get("bot") or str(author.get("id")) == _state["bot_id"]:
        return
    text = str(d.get("content") or "")
    private = not d.get("guild_id")
    user, chat = str(author.get("id", "")), str(d.get("channel_id", ""))
    m = LINK.fullmatch(text.strip())
    if m and private:
        person = _redeem(m.group(1).lower())
        if person:
            _linked(ctx, person, user, chat, str(author.get("username") or user))
        reply = f"Linked: you are {_name(person)} on Rook." if person else "That code is wrong or has expired."
        await _api(ctx, "POST", f"/channels/{chat}/messages", {"content": reply})
        return
    person = _person_of(ctx, user)
    ctx.emit("discord.message", {"text": text, "chat": chat, "private": private,
                                 "from": str(author.get("username") or user), "from_id": user,
                                 "message": str(d.get("id", "")),
                                 **({"guild": str(d["guild_id"])} if d.get("guild_id") else {}),
                                 **({"person": person} if person else {})})


# -- helpers --------------------------------------------------------------------

async def _api(ctx, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    url = str(ctx.settings["api"]).rstrip("/") + path
    headers = {"Authorization": f"Bot {ctx.settings['token']}", "User-Agent": AGENT}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, url, json=body, headers=headers) as r:
                status = r.status
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise KitError(f"discord {method} {path}: {type(e).__name__}", retry=True) from None
    if 200 <= status < 300 and isinstance(data, dict):
        return data
    why = (str(data.get("message") or "") if isinstance(data, dict) else "")[:200] or f"HTTP {status}"
    raise KitError(f"discord {method} {path}: {why}", retry=status == 429 or status >= 500)


def _snowflake(chat: str) -> str:
    if not str(chat).isdigit():
        raise KitError("chat is a Discord channel id (digits)")
    return str(chat)


def _text(text: str, title: str | None = None) -> str:
    out = f"**{title}**\n{text}" if title else text
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


def _person(name: str) -> str:
    name = name.strip()
    return name if name.startswith("person:") else f"person:{name}"


def _name(person: str | None) -> str:
    return (person or "").removeprefix("person:")
