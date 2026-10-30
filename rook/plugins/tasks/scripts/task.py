"""Task verbs the records kit doesn't generate. Each one changes a list on
the task (claims, links, comments, signoff) with the record's revision, so
two agents writing at once both land: a conflict rereads and tries again."""

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


def _comment(ctx, t, text):
    return [*(t["comments"] or ()), {"by": ctx.caller.actor, "when": time.time(), "text": text}]


def comment(ctx, id, text):
    if not text.strip():
        raise ctx.Error("bad args: text is empty")
    t = _change(ctx, id, lambda t: {"comments": _comment(ctx, t, text)})
    return {"id": t["id"], "revision": t["revision"], "comments": len(t["comments"])}


def handoff(ctx, id, to, note):
    me = ctx.caller.actor

    def change(t):
        if t["state"] not in OPEN:
            raise ctx.Error(f"task {id} is {t['state']}; nothing to hand off")
        return {**_without(t, me), "assignee": to,
                "comments": _comment(ctx, t, f"Handed to {to}: {note}")}

    t = _change(ctx, id, change)
    ctx.emit("task.handed_off", {"id": t["id"], "title": t["title"], "to": to, "by": me, "note": note})
    return _brief(t, "assignee", "claims")


def request_signoff(ctx, id, reviewers, note=None):
    me, now = ctx.caller.actor, time.time()

    def change(t):
        if t["state"] not in OPEN:
            raise ctx.Error(f"task {id} is {t['state']}; reopen it before asking for sign-off")
        asked = set(reviewers)
        keep = [s for s in t["signoff"] or () if s["reviewer"] not in asked]
        return {"signoff": keep + [{"reviewer": r, "state": "requested", "note": note or "",
                                    "by": me, "when": now} for r in dict.fromkeys(reviewers)]}

    t = _change(ctx, id, change)
    ctx.emit("task.signoff_requested", {"id": t["id"], "title": t["title"], "reviewers": list(reviewers),
                                        "by": me, "note": note or ""})
    return _brief(t, "signoff")


def signoff(ctx, id, approve, note=None, reviewer=None):
    """Answers the request for ``reviewer`` (a role, say), else the one naming
    the caller; with neither, the caller's answer is added beside the rest."""
    me = ctx.caller.actor
    entry = {"reviewer": reviewer or me, "state": "approved" if approve else "declined",
             "note": note or "", "by": me, "when": time.time()}

    def change(t):
        rows = list(t["signoff"] or ())
        names = (reviewer,) if reviewer else (me, ctx.person)
        for i, s in enumerate(rows):
            if s["reviewer"] in names:
                rows[i] = {**entry, "reviewer": s["reviewer"]}
                break
        else:
            rows.append(entry)
        return {"signoff": rows}

    return _brief(_change(ctx, id, change), "signoff")
