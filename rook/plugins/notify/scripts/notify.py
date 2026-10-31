"""notify.send: reach a person on the first channel that works.

A channel is ``phone`` (``notification.post`` on the worker in the person's
``phone`` setting) or any plugin that provides the ``notify.channel.send``
contract (Telegram, Discord, ...), named by its plugin. Notify finds them in
the hub's contract registry (``ctx.providers``) and calls each through the
contract, so a new channel plugs in without changing notify. Every channel
call runs as the caller, so their rights still bound it.
"""
from __future__ import annotations

from dataclasses import replace

from rook.kit.ctx import KitError

SEND, STATUS = "notify.channel.send", "notify.channel.status"


async def send(ctx, text, to=None, title=None, channel=None):
    people = [_person(to)] if to else ctx.owners()
    have = _channels(ctx)
    sent, errors = [], {}
    for person in people:
        for ch in _order(ctx, person, channel, have):
            try:
                await _deliver(ctx, have, ch, person, text, title)
            except KitError as e:
                errors.setdefault(person, {})[ch] = e.message
                continue
            sent.append({"to": person, "channel": ch})
            if channel != "all":
                break
    if not sent:
        detail = "; ".join(f"{p} {ch}: {why}" for p, chs in errors.items() for ch, why in chs.items())
        raise KitError(f"couldn't reach {', '.join(people) or 'anyone'}: {detail or 'no channel to try'}")
    return {"sent": sent, "errors": errors}


async def channels(ctx, to=None):
    person = _person(to) if to else ctx.person
    have = _channels(ctx)
    out = []
    for ch in _order(ctx, None, None, have):
        row = {"channel": ch}
        if ch == "phone":
            phone = ctx.settings_for(person)["phone"] if person else None
            row.update(installed=True, linked=bool(phone), worker=phone)
        elif ch not in have:
            row.update(installed=False, error=f"no plugin provides the {ch} channel")
        else:
            try:
                row.update(installed=True, **await _call(ctx, STATUS, have[ch].get(STATUS),
                                                         {"to": person} if person else {}))
            except KitError as e:
                row.update(installed=True, error=e.message)
        out.append(row)
    return {"person": person, "order": _order(ctx, person, None, have) if person else [r["channel"] for r in out],
            "channels": out}


def _channels(ctx) -> dict:
    """{channel: {contract: provider cap}}: phone, then every plugin that
    provides notify.channel.send, first installed first."""
    have = {"phone": {}}
    for contract in (SEND, STATUS):
        for cap in ctx.providers(contract):
            have.setdefault(cap.partition(".")[0], {})[contract] = cap
    return {ch: caps for ch, caps in have.items() if ch == "phone" or SEND in caps}


def _order(ctx, person: str | None, channel: str | None, have: dict) -> list:
    if channel and channel != "all":
        return [channel]
    mine = ctx.settings_for(person)["reach"] if person else None
    return list(mine or ctx.settings["channels"] or have)


async def _deliver(ctx, have: dict, ch: str, person: str, text: str, title: str | None) -> None:
    heading = {"title": title} if title else {}
    if ch == "phone":
        worker = ctx.settings_for(person)["phone"]
        if not worker:
            raise KitError("no phone set (their notify phone setting)")
        await ctx.call_on(worker, "notification.post", text=text, **heading)
    elif ch not in have:
        raise KitError(f"no plugin provides the {ch} channel")
    else:
        await _call(ctx, SEND, have[ch][SEND], {"to": person, "text": text, **heading})


async def _call(ctx, contract: str, cap: str | None, args: dict):
    if not cap:
        raise KitError(f"the channel has no {contract}")
    return await ctx.call_as(replace(ctx.caller, plugin=ctx.plugin), contract, args, provider=cap)


def _person(name: str) -> str:
    name = name.strip()
    return name if name.startswith("person:") else f"person:{name}"
