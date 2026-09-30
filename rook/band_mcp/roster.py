"""Filtered, compact views of the live worker roster for the MCP tools.

Unfiltered, the full roster is tens of kilobytes on a mid-sized band (every
worker lists every cap and plugin). These views default to what an agent needs
to pick a machine, and take filters and a ``fields`` projection for the rest.
``fields="all"`` (or ``ROOK_MCP_ENVELOPE=legacy``) returns the full pre-beta
row shape.
"""
from __future__ import annotations

import time

from . import envelope

ALL_FIELDS = ("worker_id", "name", "description", "band", "caps", "plugins", "version",
              "build", "app_release", "hb", "last_seen_age_secs", "online")
DEFAULT_FIELDS = ("name", "description", "build", "hb", "last_seen_age_secs")
# Workers announce every 30s (±20%); one missed announce still counts as online.
ONLINE_SECS = 65.0


def _row(w: dict, now: float) -> dict:
    age = round(now - w.get("last_seen", 0.0), 1)
    return {
        "worker_id": w["worker_id"],
        "name": w.get("name"),
        "description": w.get("description", ""),
        "band": w.get("band"),
        "caps": list(w.get("caps", [])),
        "plugins": w.get("plugins", []),
        "version": w.get("version"),
        "build": w.get("build"),
        "app_release": w.get("app_release") or {},
        "hb": w.get("hb") or {},
        "last_seen_age_secs": age,
        "online": age <= ONLINE_SECS,
    }


def workers_view(workers: dict, name: str | None = None, cap_prefix: str | None = None,
                 online: bool | None = None, fields=None, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    wanted = envelope.fields_arg(fields)
    full = envelope.legacy() or (wanted is not None and "all" in wanted)
    if wanted is not None and not full:
        unknown = [f for f in wanted if f not in ALL_FIELDS]
        if unknown:
            raise ValueError(f"unknown fields {unknown}; choose from {', '.join(ALL_FIELDS)} or 'all'")
    rows = [_row(w, now) for w in workers.values()]
    if name:
        needles = [n.strip().lower() for n in name.split(",") if n.strip()]
        rows = [r for r in rows if any(n in (r["name"] or "").lower() or n == r["worker_id"]
                                       for n in needles)]
    if cap_prefix:
        rows = [r for r in rows if any(c.startswith(cap_prefix) for c in r["caps"])]
        for r in rows:
            r["caps"] = [c for c in r["caps"] if c.startswith(cap_prefix)]
    if online is not None:
        rows = [r for r in rows if r["online"] == bool(online)]
    rows.sort(key=lambda r: ((r["name"] or "").lower(), r["worker_id"]))
    if full:
        return [{k: v for k, v in r.items() if k != "online"} for r in rows]
    keep = list(wanted) if wanted is not None else list(DEFAULT_FIELDS)
    if wanted is None and cap_prefix:
        keep.append("caps")  # the filter's point: which matching caps each has
    counts: dict[str, int] = {}
    for r in rows:
        counts[(r["name"] or "").lower()] = counts.get((r["name"] or "").lower(), 0) + 1
    out = []
    for r in rows:
        cols = list(keep)
        if wanted is None and counts[(r["name"] or "").lower()] > 1 and "worker_id" not in cols:
            cols.insert(0, "worker_id")  # a shared name needs the id to target
        item = {k: r[k] for k in cols}
        if wanted is None:  # compact default: omit empty values
            item = {k: v for k, v in item.items() if v not in ("", {}, [], None)}
        out.append(item)
    return out


def caps_view(workers: dict, prefix: str | None = None, worker: str | None = None):
    """Caps → holders. Compact: ``"*"`` (every live worker; the hub node
    ``rook`` is not counted), ``{"all_but": […]}``
    when most hold it, else the list of names. ``worker`` lists one worker's caps.
    Legacy: ``[{cap, workers}]``."""
    names = {wid: (w.get("name") or wid) for wid, w in workers.items()}
    if worker:
        spec = worker.strip()
        hits = [wid for wid in workers if wid == spec or names[wid].lower() == spec.lower()]
        if not hits:
            raise ValueError(f"unknown worker {spec!r}; live workers: "
                             f"{', '.join(sorted(set(names.values()), key=str.lower)) or 'none'}")
        return [{"worker": names[wid], "worker_id": wid,
                 "caps": sorted(c for c in workers[wid].get("caps", []) if c.startswith(prefix or ""))}
                for wid in hits]
    by_cap: dict[str, set[str]] = {}
    for wid, w in workers.items():
        for cap in w.get("caps", []):
            if cap.startswith(prefix or ""):
                by_cap.setdefault(cap, set()).add(names[wid])
    if envelope.legacy():
        return [{"cap": c, "workers": sorted(ws)} for c, ws in sorted(by_cap.items())]
    # The hub's own node ("rook", entry["local"]) is not a band worker: it
    # doesn't count toward "*"/all_but, so worker caps stay "*" and hub-only
    # caps read ["rook"].
    local = {names[wid] for wid, w in workers.items() if w.get("local")}
    everyone = set(names.values()) - local
    out: dict = {}
    for cap, holders in sorted(by_cap.items()):
        held = holders - local
        if held and held == everyone:
            out[cap] = "*"
        elif len(everyone) >= 4 and len(held) > len(everyone) / 2:
            out[cap] = {"all_but": sorted(everyone - held, key=str.lower)}
        else:
            out[cap] = sorted(holders, key=str.lower)
    return {"workers": len(everyone), "caps": out}
