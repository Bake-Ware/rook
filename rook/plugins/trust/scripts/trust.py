"""trust.map: everything that decides what agents may do to what, in one read.
Machines, security and agents come from core's hub caps; projects and agents'
grants from their plugins (a plugin that isn't installed leaves its part
empty and is named in ``missing``)."""

from rook.core2.rights import HARD_STOP_EFFECTS

LEVELS = [
    {"level": "sacred", "text": "Changes only on your say-so: every call that isn't a read waits on you (with security on). "
                                "A sacred project's changes get the full record and your sign-off."},
    {"level": "normal", "text": "Agents go ahead; hard stops still wait on you (with security on). "
                                "A normal project's changes get the full record."},
    {"level": "ship-it", "text": "Agents just go. A ship-it project's changes get a one-line note."},
]


def _rows(ctx, cap):
    """A plugin's record list; None when it isn't installed."""
    try:
        return ctx.call(cap, limit=500)
    except ctx.Error as e:
        if e.message.startswith(("unknown capability", "no single worker")):
            return None
        raise


def map(ctx):
    status = ctx.call("hub.status")
    machines = [{"id": w["id"], "name": w["name"], "online": bool(w["online"]), "last_seen": w["last_seen"],
                 "description": w.get("description") or "", "level": "sacred" if w.get("sacred") else "normal"}
                for w in ctx.call("hub.workers")["workers"] if not w.get("hub")]
    out = {"security": status["security"], "levels": LEVELS, "everything_else": "normal",
           "hard_stops": [*HARD_STOP_EFFECTS, "reveals a secret", "sacred machine"],
           "machines": machines, "projects": [], "agents": [], "missing": []}
    projects = _rows(ctx, "project.list")
    if projects is None:
        out["missing"].append("projects")
    else:
        by = {t["project"]: t for t in ctx.records.level.list(limit=500)}
        for pr in projects:
            t = by.get(pr["id"]) or {}
            out["projects"].append({"id": pr["id"], "title": pr.get("title") or pr.get("slug") or pr["id"],
                                    "slug": pr.get("slug"), "state": pr.get("state"),
                                    "level": t.get("level") or "normal", "set": bool(t), "note": t.get("note") or "",
                                    "trust_id": t.get("id"), "revision": t.get("revision")})
    agents = _rows(ctx, "agent.list")
    if agents is None:
        out["missing"].append("agents")
    for a in agents or ():
        out["agents"].append({"name": a.get("name"), "title": a.get("title") or "", "id": a.get("id"),
                              "revision": a.get("revision"), "persona": a.get("persona") or "",
                              "grants": list(a.get("grants") or []), "source": "agents"})
    listed = {a["name"] for a in out["agents"]}
    out["agents"] += [{"name": a["name"], "title": "", "grants": a["grants"], "source": "hub.yaml"}
                      for a in status.get("agents") or () if a["name"] not in listed]
    out["waiting"] = len(ctx.call("approval.list")["holds"])
    out["history"] = _history(ctx)
    out["summary"] = _summary(out)
    return out


def _history(ctx):
    """Recent trust changes, as the journal shows them to the caller."""
    rows = []
    for cap in ("hub.sacred", "trust.level.*", "setting.set", "agent.update"):
        for e in ctx.call("journal.search", cap=cap, limit=30)["entries"]:
            args = e.get("args") or {}
            if cap == "setting.set" and (args.get("plugin"), args.get("key")) != ("hub", "security"):
                continue
            if cap == "agent.update" and "grants" not in args:
                continue
            if e.get("risk") == "read" or e.get("ok") is False or e.get("decision") in ("deny", "held", "refused"):
                continue          # reads and what didn't happen
            rows.append({"at": e.get("ts"), "cap": e.get("cap"), "args": args, "why": e.get("why") or "",
                         "person": e.get("person"), "agent": e.get("agent")})
    rows.sort(key=lambda r: r["at"] or 0, reverse=True)
    return rows[:40]


def _summary(m):
    """What agents are told, in plain words, from the map."""
    sacred = [x["name"] for x in m["machines"] if x["level"] == "sacred"]
    lines = []
    if m["security"] == "off":
        lines.append("Security is off: nothing waits on a person; hard stops are only journaled (would_hold).")
    else:
        lines.append("Security is on: hard stops (" + ", ".join(m["hard_stops"]) + ") wait on a person.")
    lines.append("Sacred machines: " + (", ".join(sacred) if sacred else "none") + ". Anything not listed is normal.")
    for level in ("sacred", "ship-it"):
        ps = [p["title"] for p in m["projects"] if p["level"] == level]
        if ps:
            lines.append(f"{level} projects: " + ", ".join(ps) + ".")
    lines.append("Every other project is normal.")
    return " ".join(lines)
