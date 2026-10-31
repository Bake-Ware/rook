"""Huddles: a record and a durable ``huddle`` stream each (plugin.yaml says
what they are). Posts are ``text`` events by whoever posted; a post naming
one of the huddle's roles (``@qa``, or ``@all``) also goes to those agents
through ``agent.chat.send``, as the poster, so the agent acts with no more
than the poster may.

The streams live in the hub's one stream store (``ctx.streams``), so
``stream.list`` finds them and ``stream.read`` reads them.
"""
from __future__ import annotations

import copy
import re

from rook.kit.ctx import Caller, KitError
from rook.streams import StreamError

_MENTION = re.compile(r"@([a-z][a-z0-9_-]{0,39})")
_RT: "Runtime | None" = None


class Runtime:
    def __init__(self, ctx):
        sys = copy.copy(ctx)
        sys.caller = Caller(band=ctx.band, owner=True, plugin="huddle")
        self.rec = sys.records          # every row, as the plugin
        self.streams = ctx.streams               # the hub's one store (castling-streams)

    def huddle(self, hid: str, open_: bool = False) -> dict:
        try:
            h = self.rec.huddle.get(str(hid or ""))
        except KitError:
            raise KitError(f"no huddle {hid}") from None
        if open_ and h["state"] != "open":
            raise KitError(f"huddle {hid} is closed")
        return h


def _rt() -> Runtime:
    if _RT is None:
        raise KitError("huddles are not running", retry=True)
    return _RT


async def start_hook(ctx):
    global _RT
    _RT = Runtime(ctx)


async def stop_hook(ctx):
    global _RT
    _RT = None


def _actor(ctx) -> str:
    return ctx.caller.actor or "someone"


async def start(ctx, title, roles=None, project=None):
    rt = _rt()
    title = " ".join(str(title).split())[:200]
    if not title:
        raise KitError("bad args: title says what the huddle is for")
    roles = list(dict.fromkeys(roles or []))
    meta = rt.streams.open("huddle", title=title, producer="huddle", participants=[_actor(ctx)],
                           attrs={"plugin": "huddle", "roles": roles, **({"project": project} if project else {})})
    h = ctx.records.huddle.create(title=title, roles=roles, stream=meta.id, state="open",
                                  **({"project": project} if project else {}))
    rt.streams.append(meta.id, "status", {"text": f"{_actor(ctx)} opened the huddle"
                                          + (f" with {', '.join(roles)}" if roles else "") + "."}, by="huddle")
    return {"huddle": h["id"], "stream": meta.id, "title": title, "roles": roles, "project": project}


async def post(ctx, huddle, text):
    rt = _rt()
    h = rt.huddle(huddle, open_=True)
    named = _MENTION.findall(text)
    roles = h["roles"] or []
    to = roles if "all" in named else [r for r in dict.fromkeys(named) if r in roles]
    to = [r for r in to if ctx.caller.agent != f"agent:{r}"]          # not back to the poster
    ev = rt.streams.append(h["stream"], "text", {"text": text, "mentions": to}, by=_actor(ctx))
    out = {"huddle": h["id"], "seq": ev.seq, "told": [], "not_told": {}}
    note = f"[huddle {h['id']}: {h['title']}] {_actor(ctx)}: {text}\n(Answer with huddle.post huddle={h['id']}.)"
    for role in to:
        try:
            await ctx.call("agent.chat.send", agent=role, text=note, wait=False)
            out["told"].append(role)
        except KitError as e:
            out["not_told"][role] = e.message[:200]
    return out


async def read(ctx, huddle, after=0, limit=100, wait=0):
    rt = _rt()
    h = rt.huddle(huddle)
    out = await rt.streams.read(h["stream"], int(after), int(limit), wait=float(wait))
    return {"huddle": h["id"], "title": h["title"], "roles": h["roles"] or [], "state": h["state"],
            "events": [e.wire() for e in out["events"]], "next": out["next"], "closed": out["closed"]}


async def close(ctx, huddle):
    rt = _rt()
    h = rt.huddle(huddle)
    c = ctx.caller
    mine = c.person == h.get("person") if c.person else c.actor == h.get("creator")
    if not (c.owner or mine):
        raise KitError("only the huddle's creator or an owner closes it")
    if h["state"] != "closed":
        h = rt.rec.huddle.update(h["id"], revision=h["revision"], state="closed")
        try:
            rt.streams.close(h["stream"], by=_actor(ctx))
        except StreamError:
            pass
    return {"huddle": h["id"], "state": h["state"]}
