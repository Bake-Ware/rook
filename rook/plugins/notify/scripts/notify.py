"""notify.send: reach a person on the first channel that works.

A channel is ``phone`` (``notification.post`` on the worker in the person's
``phone`` setting) or a chat plugin's ``<channel>.send`` with ``to`` (the
plugin knows the person's linked account). Every channel call runs as the
caller, so their rights still bound it.
"""
from __future__ import annotations

from rook.kit.ctx import KitError


async def send(ctx, text, to=None, title=None, channel=None):
    people = [_person(to)] if to else ctx.owners()
    sent, errors = [], {}
    for person in people:
        order = _order(ctx, person, channel)
        for ch in order:
            try:
                await _deliver(ctx, ch, person, text, title)
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
    out = []
    for ch in ctx.settings["channels"] or []:
        row = {"channel": ch}
        if ch == "phone":
            phone = ctx.settings_for(person)["phone"] if person else None
            row.update(installed=True, linked=bool(phone), worker=phone)
        else:
            try:
                row.update(installed=True, **await ctx.call(f"{ch}.status", **({"to": person} if person else {})))
            except KitError as e:
                row.update(installed=False, error=e.message)
        out.append(row)
    return {"person": person, "order": _order(ctx, person, None) if person else ctx.settings["channels"],
            "channels": out}


def _order(ctx, person: str, channel: str | None) -> list:
    if channel and channel != "all":
        return [channel]
    return list(ctx.settings_for(person)["reach"] or ctx.settings["channels"] or [])


async def _deliver(ctx, ch: str, person: str, text: str, title: str | None) -> None:
    heading = {"title": title} if title else {}
    if ch == "phone":
        worker = ctx.settings_for(person)["phone"]
        if not worker:
            raise KitError("no phone set (their notify phone setting)")
        await ctx.call_on(worker, "notification.post", text=text, **heading)
    else:
        await ctx.call(f"{ch}.send", to=person, text=text, **heading)


def _person(name: str) -> str:
    name = name.strip()
    return name if name.startswith("person:") else f"person:{name}"
