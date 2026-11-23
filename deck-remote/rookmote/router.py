"""A key, through the picked profile, as the firmware's local_key/on_button do it."""

from __future__ import annotations

import threading
from typing import Callable

from . import keys as K
from . import lan
from .profiles import Store, kind


def route(store: Store, key: str) -> tuple[str, dict | None, dict | None]:
    """(via, action, profile). via: ir (unavailable on the Deck), lan, cap, rook,
    or none (nothing assigned); "hub" when no profile is picked (the press is
    the hub's, as on the bridge with no profile)."""
    p = store.current()
    if p is None:
        return "hub", None, None
    a = store.action(p, key)
    if a is None:
        return "none", None, p
    return kind(a), a, p


def payload(key: str, via: str, action: dict | None, profile: dict | None, held_ms: int = 0) -> dict:
    """remote.button's payload, field for field as the firmware sends it (source: deck)."""
    out = {"button": K.canonical(key), "action": "press", "index": -1, "held_ms": held_ms, "source": "deck"}
    if profile is not None:
        out.update({"target": profile["name"], "local": True, "profile": profile["name"]})
        if via in ("ir", "lan", "cap", "rook"):
            out["via"] = via
        if via == "cap" and action:
            call = {"cap": action["cap"], "args": dict(action.get("args") or {})}
            if action.get("worker"):
                call["worker"] = action["worker"]
            out["call"] = call
    return out


def lan_send(profile: dict, action: dict, key: str, done: Callable[[str | None], None] | None = None) -> None:
    """The key to the profile's linked device, on a thread (a TV can take seconds). done(err or None)."""
    link = profile.get("link")
    send = action.get("lan") if isinstance(action.get("lan"), str) else K.canonical(key)

    def run():
        err = None
        try:
            if not link:
                raise lan.LanError(f"{profile['name']} is linked to no device")
            lan.send_key(link, send)
        except (lan.LanError, OSError) as e:
            err = str(e)
        if done:
            done(err)

    threading.Thread(target=run, name="rookmote-lan", daemon=True).start()
