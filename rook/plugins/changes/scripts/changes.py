"""Change records (castling-changes, castling-caps C2): a record, not a runner.

``change.create`` checks the record against what the project's trust level
asks for (:func:`lacks`) and stores it. It runs nothing, waits for nothing
and holds nothing: the change itself is made with ordinary calls, which the
hub's one hold path covers like any other. The other caps (list, get,
search, update, history) are the kit's.
"""
from __future__ import annotations

from rook.kit.ctx import Caller, KitError

FULL = ("risk", "testing", "rollback", "steps")
TEXT_MIN = 8            # a real sentence, not "n/a"
LEVELS = ("sacred", "normal", "ship-it")


def lacks(rec: dict, level: str) -> list[str]:
    """What a record still needs at ``level`` (empty: enough). ship-it: what
    and why. normal and sacred: also risk, testing, rollback and steps."""
    if level == "ship-it":
        return []
    return [f for f in FULL if len(" ".join(str(rec.get(f) or "").split())) < TEXT_MIN]


async def level(ctx, project: str | None) -> str:
    """The project's trust level (project.trust); normal without a project,
    without a level, or without the workbench. A project that the workbench
    doesn't know is refused."""
    if not project:
        return "normal"
    me = Caller(band=ctx.caller.band, plugin="change",
                why=f"Read project {project}'s trust level for a change record")
    try:
        try:
            p = await ctx.call_as(me, "project.get", {"id": project})
        except KitError as e:
            if e.message.startswith(("unknown capability", "no single worker")):
                raise
            found = await ctx.call_as(me, "project.list", {"where": {"slug": project}, "limit": 1})
            if not found:
                raise KitError(f"bad args: no project {project}") from None
            p = found[0]
        trust = await ctx.call_as(me, "project.trust.list", {"where": {"project": p["id"]}, "limit": 1})
    except KitError as e:
        if e.message.startswith(("unknown capability", "no single worker")):
            return "normal"           # no workbench on this hub: every change is normal
        raise
    got = trust[0].get("level") if trust else None
    return got if got in LEVELS else "normal"


async def create(ctx, what: str, why: str, **fields):
    what, why = " ".join(str(what).split()), " ".join(str(why).split())
    if not what or not why:
        raise KitError("bad args: what and why say what changes and why")
    lvl = await level(ctx, fields.get("project"))
    missing = lacks(fields, lvl)
    if missing:
        raise KitError(f"bad args: the record needs {', '.join(missing)} (the project is {lvl}): "
                       "what could break, how it is tested, how to undo it and the steps")
    return ctx.records.change.create(what=what, why=why, level=lvl,
                                     **{k: v for k, v in fields.items() if v not in (None, "", [])})
