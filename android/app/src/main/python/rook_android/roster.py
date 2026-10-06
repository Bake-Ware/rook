"""The band roster as this phone sees it, for the app's Workers tab.

Every band member re-announces about every 30 s and the hub relays those
announces to everyone on the band (the MCP bridge builds ``rook_workers`` from
the same packets). ``rook.worker.core.Worker`` ignores announces, so this module
listens alongside it: :func:`attach` registers a non-consuming binary handler
that records each announce, and :func:`snapshot` hands the Kotlin UI a JSON
copy. No new band traffic, no hub call, and no credentials beyond the band key
the worker already holds.

Limits: only workers heard since this phone's worker connected are listed, and
nothing is known while the worker is stopped. A worker that goes quiet stays
listed (with its age) until :data:`FORGET_SECS`.

Other bands: when the phone runs on its enrolled device identity, the worker's
30 s device-config refresh also asks the hub for the rosters of every band the
enrolling account can see (:func:`hub_result`). That copy is shown alongside
the local one while it is fresh (:data:`HUB_FRESH_SECS`); a PSK-only phone, an
old hub or a failed refresh simply leaves the local, same-band roster. A hub
that answers the config refresh without ``workers`` :data:`UNSUPPORTED_AFTER`
times in a row (the hub throttles to one roster per 20 s, so one miss can be a
throttle) is reported as not providing other bands, rather than as failing.
"""
from __future__ import annotations

import json
import threading
import time

#: Drop workers not heard from for this long (a band move starts afresh anyway).
FORGET_SECS = 24 * 3600
#: Upper bound on remembered workers; a roster this large is malformed traffic.
MAX_WORKERS = 500
_MARK = b'"announce"'
#: The hub roster rides on the 30 s config refresh; older than this means the
#: refresh is failing (or the hub predates it) and only the local roster shows.
HUB_FRESH_SECS = 120
MAX_BANDS = 64
#: Consecutive roster-less config responses before the hub counts as lacking the feature.
UNSUPPORTED_AFTER = 2


def _text(value, limit: int = 280) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


class Roster:
    def __init__(self, self_id: str = "", clock=time.time) -> None:
        self.self_id = self_id
        self._clock = clock
        self._lock = threading.Lock()
        self._workers: dict[str, dict] = {}

    def observe(self, payload, _peer=None) -> bool:
        """Binary-handler hook: record an announce, never consume the payload."""
        try:
            if _MARK not in payload:
                return False
            msg = json.loads(payload)
        except Exception:
            return False
        if isinstance(msg, dict) and msg.get("kind") == "announce" and not msg.get("cap"):
            self.record(msg)
        return False

    def record(self, msg: dict) -> None:
        wid = msg.get("worker_id")
        if not isinstance(wid, str) or not wid:
            return
        caps = msg.get("caps")
        entry = {
            "worker_id": wid[:64],
            "name": _text(msg.get("name"), 80) or wid[:12],
            "description": _text(msg.get("description")),
            "caps": len(caps) if isinstance(caps, list) else 0,
            "version": _text(msg.get("version"), 40),
            "build": msg.get("build") if isinstance(msg.get("build"), int) else None,
            "app_release": {k: v for k, v in _dict(msg.get("app_release")).items()
                            if k in ("platform", "version", "code")},
            "hb": {k: v for k, v in _dict(msg.get("hb")).items() if k == "battery"},
            "last_seen": self._clock(),
        }
        with self._lock:
            if wid not in self._workers and len(self._workers) >= MAX_WORKERS:
                return
            self._workers[wid] = entry

    def rows(self, own: dict | None = None) -> list[dict]:
        now = self._clock()
        with self._lock:
            for wid in [w for w, e in self._workers.items() if now - e["last_seen"] > FORGET_SECS]:
                del self._workers[wid]
            entries = [dict(e) for e in self._workers.values()]
        if own and not any(e["worker_id"] == own.get("worker_id") for e in entries):
            entries.append({**own, "last_seen": now})
        for e in entries:
            e["last_seen_age_secs"] = round(max(0.0, now - e.pop("last_seen")), 1)
            e["self"] = e["worker_id"] == self.self_id
        return entries


_lock = threading.Lock()
_roster: Roster | None = None
_worker = None
#: How this phone joined: {"identity": bool, "band_id": str, "band_name": str}.
_identity: dict = {"identity": False, "band_id": "", "band_name": ""}
#: Last hub roster (``workers`` from /auth/devices/config), tagged with the
#: band whose config carried it, and when it arrived.
_hub: dict | None = None
_hub_at = 0.0
#: (band id, config responses in a row for that band that carried no roster).
_hub_misses: tuple[str, int] = ("", 0)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _clean_worker(w) -> dict | None:
    """One hub roster row, rebuilt field by field with the same caps as
    :meth:`Roster.record` (the hub already trims; this phone doesn't rely on it)."""
    if not isinstance(w, dict) or not isinstance(w.get("worker_id"), str) or not w["worker_id"]:
        return None
    app = _dict(w.get("app_release"))
    battery = _dict(_dict(w.get("hb")).get("battery"))
    build = w.get("build")
    return {
        "worker_id": w["worker_id"][:64],
        "name": _text(w.get("name"), 80),
        "description": _text(w.get("description")),
        "caps": w["caps"] if isinstance(w.get("caps"), int) and not isinstance(w["caps"], bool) else 0,
        "version": _text(w.get("version"), 40),
        "build": build if isinstance(build, int) and not isinstance(build, bool) else None,
        "app_release": {k: (app[k][:40] if isinstance(app[k], str) else app[k])
                        for k in ("platform", "version", "code")
                        if isinstance(app.get(k), (str, int)) and not isinstance(app.get(k), bool)},
        "hb": {"battery": {k: battery[k] for k in ("percent", "charging")
                           if isinstance(battery.get(k), (int, float, bool))}} if battery else {},
        "last_seen_age_secs": _number(w.get("last_seen_age_secs")),
    }


def _clean_band(band) -> dict | None:
    if not isinstance(band, dict) or not isinstance(band.get("id"), str):
        return None
    raw = band.get("workers") if isinstance(band.get("workers"), list) else []
    workers = [w for w in (_clean_worker(x) for x in raw[:MAX_WORKERS]) if w]
    return {"id": band["id"][:64], "name": _text(band.get("name"), 100) or band["id"][:8],
            "role": _text(band.get("role"), 20), "current": band.get("current") is True,
            "workers": workers}


def note_identity(identity: bool, band: dict | None = None) -> None:
    """Record whether the worker runs on its enrolled identity (and on which
    band). A PSK-only phone, or one that moved to another band, forgets any
    hub roster it had: that copy was fetched for another band's device.

    (The refresh hands :func:`hub_result` the new band's roster before this
    is called, so what is dropped is a copy for a *different* band, not
    simply anything older than the change.)"""
    global _identity, _hub, _hub_misses
    band = band if isinstance(band, dict) else {}
    with _lock:
        _identity = {"identity": bool(identity), "band_id": _text(band.get("id"), 64),
                     "band_name": _text(band.get("name"), 100)}
        if not identity or (_hub is not None and _hub["band_id"] != _identity["band_id"]):
            _hub = None
        if not identity or _hub_misses[0] != _identity["band_id"]:
            _hub_misses = ("", 0)


def hub_result(result) -> None:
    """``enroll.refresh(on_result=...)`` hook: keep the hub's roster, if it sent one."""
    global _hub, _hub_at, _hub_misses
    if not isinstance(result, dict):
        return
    band_id = _text(_dict(result.get("band")).get("id"), 64)
    workers = result.get("workers")
    if not isinstance(workers, dict) or not isinstance(workers.get("bands"), list):
        # Throttled, or a hub without the roster: keep the last copy until stale.
        with _lock:
            seen, count = _hub_misses
            _hub_misses = (band_id, count + 1 if seen == band_id else 1)
        return
    bands = [b for b in (_clean_band(x) for x in workers["bands"][:MAX_BANDS]) if b]
    with _lock:
        _hub = {"band_id": band_id, "scope": "account" if workers.get("scope") == "account" else "band",
                "bands": bands}
        _hub_at = time.time()
        _hub_misses = ("", 0)


def _hub_unsupported() -> bool:
    with _lock:
        band_id, count = _hub_misses
        return (_identity["identity"] and band_id == _identity["band_id"]
                and count >= UNSUPPORTED_AFTER)


def _hub_view() -> dict | None:
    with _lock:
        hub, at, identity = _hub, _hub_at, dict(_identity)
    if hub is None or not identity["identity"] or hub["band_id"] != identity["band_id"]:
        return None
    age = time.time() - at
    if not 0 <= age <= HUB_FRESH_SECS:
        return None
    bands = []
    for band in hub["bands"]:
        rows = []
        for w in band["workers"]:
            row = dict(w)
            seen = w.get("last_seen_age_secs")
            row["last_seen_age_secs"] = None if seen is None else round(max(0.0, float(seen)) + age, 1)
            rows.append(row)
        bands.append({**band, "workers": rows})
    return {"scope": hub["scope"], "age_secs": round(age, 1), "bands": bands}


def attach(worker) -> Roster:
    """Start recording announces for this worker's band (a fresh roster per connection)."""
    global _roster, _worker
    roster = Roster(str(getattr(worker, "worker_id", "") or ""))
    register = getattr(worker, "register_binary_handler", None)
    if callable(register):
        register(roster.observe)
    with _lock:
        _roster, _worker = roster, worker
    return roster


def detach(worker) -> None:
    global _roster, _worker
    with _lock:
        if _worker is worker:
            _roster, _worker = None, None


def _own(worker) -> dict | None:
    """This phone's own row, from what its worker would announce (the hub may not echo it)."""
    try:
        from rook.worker._build_info import BUILD, VERSION
        return {"worker_id": worker.worker_id, "name": worker.name,
                "description": worker.metadata.description or "",
                "caps": len(worker.registry.list()), "version": VERSION, "build": BUILD,
                "app_release": dict(getattr(worker, "app_release", {}) or {}), "hb": {}}
    except Exception:
        return None


def snapshot() -> str:
    """JSON for the app (UI groups, sorts and labels)::

        {"running": bool, "self_id": str, "workers": [...this band, heard locally],
         "identity": bool, "band_id": str, "band_name": str,
         "hub": null | {"scope": "account"|"band", "age_secs": float,
                        "bands": [{"id", "name", "role", "current", "workers": [...]}]},
         "hub_unsupported": bool}   # config refreshes arrive, but never with a roster
    """
    with _lock:
        roster, worker, identity = _roster, _worker, dict(_identity)
    if roster is None:
        return json.dumps({"running": False, "workers": [], **identity, "hub": None,
                           "hub_unsupported": False})
    hub = _hub_view()
    return json.dumps({"running": True, "self_id": roster.self_id,
                       "workers": roster.rows(_own(worker) if worker is not None else None),
                       **identity, "hub": hub, "hub_unsupported": hub is None and _hub_unsupported()})
