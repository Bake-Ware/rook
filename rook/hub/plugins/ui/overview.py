"""The knowledge half of the Overview page: work by project, what needs a
person, recent page changes and the links that tie a task to its rooms and
sessions.

The browser joins this with what only the dashboard process holds (live
sessions, chat rooms, workers; rook/web/overview.js), so this stays a read
of the knowledge store and nothing else.
"""

from __future__ import annotations

import json
import time

OPEN = ("in_progress", "blocked", "todo", "paused")
#: Link kinds that point at something the dashboard can open.
LINK_KINDS = ("chat", "session", "console", "handoff")
#: Open hygiene findings that ask a person to decide something.
DECISIONS = {"release_proposed": "An idle claim could be released",
             "project_complete": "All tasks are finished: close the project?"}


def _brief(item: dict) -> dict:
    out = {k: item.get(k) for k in ("id", "slug", "title", "state", "updated", "band")}
    if item.get("claimants"):
        out["claimants"] = [c["actor"] for c in item["claimants"]]
    if item.get("blocked_reason"):
        out["blocked_reason"] = item["blocked_reason"]
    if item.get("dependencies") and item.get("unblocked") is False:
        out["waiting_on"] = [d.get("slug") or d.get("id") for d in item["dependencies"]
                             if d.get("state") not in ("done", "closed", "cancelled")]
    return out


def projects(store, bands, per_list: int = 5, done_days: float = 3) -> list[dict]:
    """Projects that have open work, busiest first, each with its tasks by
    state (a few of each) and the full counts."""
    out = []
    for entry in store.deck(bands, done_days=done_days, outcome_chars=160):
        project = entry["project"]
        counts = {k: len(entry.get(k) or []) for k in ("in_progress", "blocked", "todo", "paused",
                                                         "recently_done")}
        if not any(counts[k] for k in OPEN) and project.get("state") != "active":
            continue
        if not any(counts.values()):
            continue
        row = {"project": {k: project.get(k) for k in ("id", "slug", "title", "state", "band")},
               "counts": counts}
        for key in ("in_progress", "blocked", "todo", "recently_done"):
            row[key] = [_brief(i) for i in (entry.get(key) or [])[:per_list]]
        latest = [i.get("updated") or 0 for k in OPEN + ("recently_done",) for i in entry.get(k) or []]
        row["active"] = max(latest, default=project.get("updated") or 0)
        out.append(row)
    out.sort(key=lambda r: (-(r["counts"]["in_progress"] + r["counts"]["blocked"]), -r["active"]))
    return out


def task_links(store, task_ids) -> dict[str, list[dict]]:
    """``{task id: [{kind, ref, relation}]}`` for the chat rooms, sessions,
    consoles and handoffs linked to each task (retracted links left out)."""
    task_ids = list(task_ids)
    if not task_ids:
        return {}
    out: dict[str, list[dict]] = {}
    marks = ",".join("?" * len(task_ids))
    kinds = ",".join("?" * len(LINK_KINDS))
    with store.db(False) as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT id, record, kind, ref, relation, retracts FROM links "
            f"WHERE record IN ({marks}) AND (kind IN ({kinds}) OR retracts IS NOT NULL) ORDER BY ts",
            (*task_ids, *LINK_KINDS))]
    gone = {r["retracts"] for r in rows if r["retracts"]}
    for r in rows:
        if r["retracts"] or r["id"] in gone or r["kind"] not in LINK_KINDS:
            continue
        links = out.setdefault(r["record"], [])
        if not any(l["kind"] == r["kind"] and l["ref"] == r["ref"] for l in links):
            links.append({"kind": r["kind"], "ref": r["ref"], "relation": r["relation"]})
    return out


def recent_pages(store, bands, limit: int = 10, scan: int = 400) -> list[dict]:
    """The wiki pages changed most recently, one row per page, with who
    changed it and how."""
    if not bands:
        return []
    marks = ",".join("?" * len(bands))
    with store.db(False) as db:
        events = db.execute(
            f"SELECT e.record, e.actor, e.action, e.ts, r.slug, r.title, r.state, r.band, r.attrs "
            f"FROM events e JOIN records r ON r.id=e.record "
            f"WHERE r.kind='knowledge' AND e.band IN ({marks}) AND e.action<>'hygiene' "
            f"ORDER BY e.seq DESC LIMIT ?", (*bands, scan)).fetchall()
    out, seen = [], set()
    for e in events:
        if e["record"] in seen:
            continue
        seen.add(e["record"])
        try:
            attrs = json.loads(e["attrs"] or "{}")
        except ValueError:
            attrs = {}
        out.append({"id": e["record"], "slug": e["slug"], "title": e["title"], "state": e["state"],
                    "band": e["band"], "actor": e["actor"], "action": e["action"], "ts": e["ts"],
                    "verification": attrs.get("verification", "unverified")})
        if len(out) >= limit:
            break
    return out


def decisions(service, bands, limit: int = 20) -> list[dict]:
    """Open hygiene findings that wait on a person (release a stale claim,
    close a finished project)."""
    engine = getattr(service, "hygiene", None)
    if engine is None or not getattr(engine, "enabled", False):
        return []
    out = []
    for f in engine.open(bands=bands, limit=200):
        if f["kind"] in DECISIONS:
            out.append({"kind": f["kind"], "what": DECISIONS[f["kind"]], "record": f["record"],
                        "slug": f.get("slug"), "band": f.get("band"), "actor": f.get("actor"),
                        "text": (f.get("text") or "").replace("[[", "").replace("]]", "")[:240],
                        "created": f.get("created")})
            if len(out) >= limit:
                break
    return out


def build(service) -> dict:
    """Everything the Overview needs from knowledge, for every band the
    operator has."""
    bands = [b["id"] for b in service.bands()]
    store = service.store
    rows = projects(store, bands)
    open_ids = {t["id"] for r in rows for k in ("in_progress", "blocked", "todo") for t in r[k]}
    return {"generated": time.time(), "bands": service.bands(), "projects": rows,
            "links": task_links(store, open_ids), "recent": recent_pages(store, bands),
            "decisions": decisions(service, bands)}
