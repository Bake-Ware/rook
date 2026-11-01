"""Task verbs the records kit doesn't generate. Claims and links are lists on
the task, changed with the record's revision so two agents writing at once
both land (a conflict rereads and tries again). Comments and sign-offs are
records of their own that only these caps write."""

import time

OPEN = ("todo", "in_progress", "blocked", "paused")
TRIES = 8


def _change(ctx, id, change):
    """Apply ``change(task) -> fields`` to the current revision; retry on a race."""
    for attempt in range(TRIES):
        t = ctx.records.task.get(id)
        if t["archived"]:
            raise ctx.Error(f"task {id} is archived")
        fields = change(t)
        try:
            return ctx.records.task.update(id, revision=t["revision"], **fields)
        except ctx.Error as e:
            if not e.retry or attempt == TRIES - 1:
                raise


def _brief(t, *keys):
    return {"id": t["id"], "revision": t["revision"], "state": t["state"], **{k: t[k] for k in keys}}


def claim(ctx, id):
    me, now = ctx.caller.actor, time.time()

    def change(t):
        if t["state"] not in OPEN:
            raise ctx.Error(f"task {id} is {t['state']}; set its state to todo to reopen it")
        mine = next((c for c in t["claims"] or () if c["actor"] == me), None)
        claims = [c for c in t["claims"] or () if c["actor"] != me]
        claims.append({"actor": me, "person": ctx.person, "session": ctx.session,
                       "since": mine["since"] if mine else now})
        return {"claims": claims, **({"state": "in_progress"} if t["state"] in ("todo", "paused") else {})}

    t = _change(ctx, id, change)
    return {"task": t, "also_claimed_by": [c["actor"] for c in t["claims"] if c["actor"] != me]}


def _without(t, actor, state=None):
    claims = [c for c in t["claims"] or () if c["actor"] != actor]
    fields = {"claims": claims}
    if state:
        fields["state"] = state
    elif not claims and t["state"] == "in_progress":
        fields["state"] = "todo"
    return fields


def release(ctx, id, actor=None, state=None):
    who = actor or ctx.caller.actor

    def change(t):
        if not any(c["actor"] == who for c in t["claims"] or ()):
            raise ctx.Error(f"{who} holds no claim on task {id}")
        return _without(t, who, state)

    return _brief(_change(ctx, id, change), "claims")


def link(ctx, id, kind, ref, relation="evidence", note=None, remove=False):
    def change(t):
        links = [x for x in t["links"] or () if (x["kind"], x["ref"]) != (kind, ref)]
        if remove:
            if len(links) == len(t["links"] or ()):
                raise ctx.Error(f"task {id} has no {kind} link {ref}")
        else:
            links.append({"kind": kind, "ref": ref, "relation": relation, "note": note or "",
                          "by": ctx.caller.actor, "when": time.time()})
        return {"links": links}

    return _brief(_change(ctx, id, change), "links")


def comment(ctx, id, text):
    if not text.strip():
        raise ctx.Error("bad args: text is empty")
    t = ctx.records.task.get(id)
    if t["archived"]:
        raise ctx.Error(f"task {id} is archived")
    c = ctx.records.comment.create(task=id, text=text)
    return {"id": c["id"], "task": id, "by": c["creator"]}


def handoff(ctx, id, to, note):
    me = ctx.caller.actor

    def change(t):
        if t["state"] not in OPEN:
            raise ctx.Error(f"task {id} is {t['state']}; nothing to hand off")
        return {**_without(t, me), "assignee": to}

    t = _change(ctx, id, change)
    ctx.records.comment.create(task=id, text=f"Handed to {to}: {note}")
    ctx.emit("task.handed_off", {"id": t["id"], "title": t["title"], "to": to, "by": me, "note": note})
    return _brief(t, "assignee", "claims")


def _signoffs(ctx, id):
    return ctx.records.signoff.list(where={"task": id}, limit=500, order="created")


def _set_signoff(ctx, id, reviewer, **fields):
    """One row per (task, reviewer): asking again or answering rewrites it."""
    key = f"{id} {reviewer}"
    for attempt in range(2):
        row = next(iter(ctx.records.signoff.list(where={"key": key}, limit=1)), None)
        if row is not None:
            return ctx.records.signoff.update(row["id"], **fields)
        try:
            return ctx.records.signoff.create(task=id, reviewer=reviewer, key=key, **fields)
        except ctx.Error:
            if attempt:          # not a race with another writer: a real failure
                raise


def _view(s):
    return {k: s[k] for k in ("reviewer", "state", "note", "by", "updated")}


def _project(ctx, t):
    """(trust level, expert pool) of the task's project; normal and no pool without one."""
    if not t.get("project"):
        return "normal", {}
    p = ctx.call("project.get", id=t["project"])
    trust = ctx.call("project.trust.list", where={"project": p["id"]}, limit=1)
    pool = ctx.call("concept.pool", concepts=p["concepts"]) if p.get("concepts") else {}
    return (trust[0]["level"] if trust else "normal"), pool


def request_signoff(ctx, id, reviewers=None, note=None):
    me, t = ctx.caller.actor, ctx.records.task.get(id)
    if t["archived"] or t["state"] not in OPEN:
        raise ctx.Error(f"task {id} is {'archived' if t['archived'] else t['state']}; "
                        "reopen it before asking for sign-off")
    level, pool = _project(ctx, t)
    experts = [] if reviewers or pool.get("signoff") == "none" else [f"role:{r}" for r in pool.get("roles") or []]
    reviewers = list(dict.fromkeys(reviewers or experts))
    # The project's trust level decides whether the person signs; not while
    # the hub is moving in (nothing waits on a person: castling-switchover-decisions).
    if level == "sacred" and not ctx.move_in:
        reviewers += [o for o in ctx.owners() if o not in reviewers]
    if not reviewers:
        if pool.get("signoff") == "none" or level == "ship-it":
            ctx.records.comment.create(task=id, text="No sign-off needed: "
                                       + ("the project is ship-it." if level == "ship-it" else
                                          "its concepts say sign-off: none."))
            return {"id": id, "signoff": [], "needed": False}
        raise ctx.Error("name reviewers: the task's project has no concepts with expert roles")
    for r in reviewers:
        _set_signoff(ctx, id, r, state="requested", note=note or "", by=me)
    # The agents package wakes the agents asked (its on: hook), as the one asking.
    ctx.emit("task.signoff_requested", {"id": id, "title": t["title"], "reviewers": reviewers,
                                        "by": me, "person": ctx.person or "", "note": note or ""})
    return {"id": id, "signoff": [_view(s) for s in _signoffs(ctx, id)]}


def signoff(ctx, id, approve, note=None, reviewer=None):
    """Answer the request made to ``reviewer`` (a role you were asked as), or
    to you. Nobody answers for a person or another agent: a person's
    sign-off comes from the person, not through an agent acting for them."""
    me = ctx.caller.actor
    target = reviewer or me
    if target.startswith("person:") and target == ctx.person and ctx.agent:
        raise ctx.Error(f"{target} signs off from their own login, not through an agent")
    if target != me and target.startswith(("person:", "agent:")):
        raise ctx.Error(f"only {target} signs off as {target}")
    ctx.records.task.get(id)
    _set_signoff(ctx, id, target, state="approved" if approve else "declined", note=note or "", by=me)
    return {"id": id, "signoff": [_view(s) for s in _signoffs(ctx, id)]}
