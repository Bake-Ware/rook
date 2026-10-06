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
    """JSON for the app: ``{"running": bool, "workers": [...]}`` (UI sorts and labels)."""
    with _lock:
        roster, worker = _roster, _worker
    if roster is None:
        return json.dumps({"running": False, "workers": []})
    return json.dumps({"running": True, "self_id": roster.self_id,
                       "workers": roster.rows(_own(worker) if worker is not None else None)})
