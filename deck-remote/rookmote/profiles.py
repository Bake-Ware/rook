"""Device profiles, as the firmware keeps them (firmware-remote/src/prof.h),
in one JSON file the app and the worker plugin share.

A profile has a name, an icon, an optional link to a device on the network
("roku@192.168.1.20", "webos@192.168.1.30") and keys, each an action, in the
firmware's profile_save JSON:
  {"rook": true}                         the remote.button event, nothing else (hub jobs act on it)
  {"cap": "x.y", "worker": ?, "args": ?} a cap call, carried to the hub in the event
  {"lan": true | "<key>"}                the key (or another) to the linked device
  {"protocol": "nec", "address", "command"} | {"raw": [...], "carrier"}
                                         IR: kept (a profile copied from the bridge keeps
                                         them) but the Deck has no IR LED: unavailable
A key with no action goes to the linked device (lan) when there is one.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path

from . import keys as K
from .paths import data_dir

ICONS = ["tv", "projector", "speaker", "streaming", "light", "phone", "car", "game", "generic"]
ICON_ALIASES = {"streaming_box": "streaming", "stream": "streaming", "box": "streaming", "game_controller": "game",
                "controller": "game", "console": "game", "television": "tv", "lamp": "light", "lights": "light"}
MAX_PROFILES = 32
MAX_NAME = 20
MAX_ARGS = 240

# The routes a profile's keys take (as prof::Route): the order the screen shows them in.
ROUTE_IR, ROUTE_ROOK, ROUTE_WIFI, ROUTE_BT = 1, 2, 4, 8


class ProfileError(ValueError):
    pass


def icon_name(s: str | None) -> str | None:
    """An icon by name (blank, none: None). Raises for an unknown one."""
    n = (s or "").strip().lower().replace(" ", "_").replace("-", "_")
    if n in ("", "none"):
        return None
    n = ICON_ALIASES.get(n, n)
    if n not in ICONS:
        raise ProfileError("bad args: icon is one of " + ", ".join(ICONS))
    return n


def kind(action: dict) -> str:
    """ir, lan, cap or rook."""
    if "rook" in action:
        return "rook"
    if "cap" in action:
        return "cap" if action.get("cap") else "rook"       # a nameless cap: rook (the firmware's storage)
    if "lan" in action:
        return "lan"
    return "ir"


def check_action(key: str, v) -> dict:
    """One key's action, checked as the firmware checks it (prof.cpp parse_code)."""
    if not isinstance(v, dict):
        raise ProfileError(f"bad args: keys.{key} is {{protocol: nec, address, command}}, {{raw: [...]}}, "
                           "{lan: true | key}, {cap, worker, args} or {rook: true}")
    if "rook" in v:
        if v["rook"] is not True:
            raise ProfileError(f"bad args: keys.{key}.rook is true (null takes the key out)")
        return {"rook": True}
    if "lan" in v:
        lan = v["lan"]
        if lan is True:
            return {"lan": True}
        if isinstance(lan, str) and 0 < len(lan) <= 24:
            return {"lan": lan}
        raise ProfileError(f"bad args: keys.{key}.lan is true or a key name of 1..24 characters")
    if "cap" in v:
        cap = v.get("cap")
        if not isinstance(cap, str) or "." not in cap.strip(".") or len(cap) > 48:
            raise ProfileError(f"bad args: keys.{key}.cap is a cap's name, plugin.verb (at most 48 characters)")
        out = {"cap": cap}
        if v.get("worker"):
            if not isinstance(v["worker"], str) or len(v["worker"]) > 40:
                raise ProfileError(f"bad args: keys.{key}.worker is a worker's name or id, at most 40 characters")
            out["worker"] = v["worker"]
        if v.get("args") is not None:
            if not isinstance(v["args"], dict):
                raise ProfileError(f"bad args: keys.{key}.args is a map")
            if len(json.dumps(v["args"])) > MAX_ARGS:
                raise ProfileError(f"bad args: keys.{key}.args is over {MAX_ARGS} bytes as JSON")
            if v["args"]:
                out["args"] = copy.deepcopy(v["args"])
        return out
    if "raw" in v:
        raw = v["raw"]
        if not isinstance(raw, list) or not raw or len(raw) > 256 or not all(isinstance(d, int) and 0 < d <= 1000000 for d in raw):
            raise ProfileError(f"bad args: keys.{key}.raw is 1..256 durations in microseconds")
        carrier = v.get("carrier", 38000)
        if not isinstance(carrier, int) or not 20000 <= carrier <= 100000:
            raise ProfileError(f"bad args: keys.{key}.carrier is 20000..100000 Hz")
        return {"raw": list(raw), "carrier": carrier}
    if (v.get("protocol") or "nec") != "nec":
        raise ProfileError(f"bad args: keys.{key}.protocol is nec (or give raw)")
    a, c = v.get("address"), v.get("command")
    if not isinstance(a, int) or not isinstance(c, int) or not 0 <= a <= 0xFFFF or not 0 <= c <= 0xFF:
        raise ProfileError(f"bad args: keys.{key} needs address (0..65535) and command (0..255)")
    return {"protocol": "nec", "address": a, "command": c}


def routes(p: dict) -> int:
    """The routes a profile's keys take (prof::Info::routes)."""
    r = 0
    for a in (p.get("keys") or {}).values():
        k = kind(a)
        r |= ROUTE_IR if k == "ir" else ROUTE_ROOK if k in ("cap", "rook") else ROUTE_WIFI
    if p.get("link"):
        r |= ROUTE_WIFI
    return r


def clean_name(name) -> str:
    n = (name or "").strip() if isinstance(name, str) else ""
    if not n or len(n) > MAX_NAME or any(ord(c) < 0x20 or ord(c) == 0x7F for c in n):
        raise ProfileError(f"bad args: name is 1..{MAX_NAME} characters")
    return n


class Store:
    """The profiles and which one is picked, in data_dir()/profiles.json.
    Every change is written at once (atomically); a change made by the other
    process (the app, or the plugin) is picked up by reload()."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "profiles.json"
        self.lock = threading.RLock()
        self.profiles: list[dict] = []
        self.selected: str | None = None
        self._mtime = None
        self.reload(force=True)

    # -- the file ---------------------------------------------------------------------

    def reload(self, force: bool = False) -> bool:
        """Read the file again when it changed. True: it did."""
        with self.lock:
            try:
                mt = self.path.stat().st_mtime_ns
            except FileNotFoundError:
                mt = None
            if not force and mt == self._mtime:
                return False
            self._mtime = mt
            data = {}
            if mt is not None:
                try:
                    data = json.loads(self.path.read_text())
                except (OSError, ValueError):
                    data = {}
            self.profiles = [p for p in data.get("profiles") or [] if isinstance(p, dict) and p.get("name")]
            self.selected = data.get("selected") if any(p["name"] == data.get("selected") for p in self.profiles) else None
            return True

    def _save(self) -> None:
        data = {"version": 1, "selected": self.selected, "profiles": self.profiles}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".profiles-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    # -- reading ----------------------------------------------------------------------

    def find(self, name: str) -> dict | None:
        with self.lock:
            n = (name or "").lower()
            return next((p for p in self.profiles if p["name"].lower() == n), None)

    def current(self) -> dict | None:
        with self.lock:
            return self.find(self.selected) if self.selected else None

    def action(self, p: dict | None, key: str) -> dict | None:
        """The key's action in p (None: none; a key with no action and a link goes to the device)."""
        if p is None:
            return None
        k = K.canonical(key)
        a = (p.get("keys") or {}).get(k)
        if a is None and p.get("link"):
            return {"lan": True}
        return a

    def listing(self, p: dict) -> dict:
        """As remote.ir profiles lists one (name, link, icon, keys, lan_keys, cap_keys, rook_keys, ir_keys)."""
        ks = p.get("keys") or {}
        out = {"name": p["name"], "icon": p.get("icon") or "generic", "keys": sorted(ks)}
        if p.get("link"):
            out["link"] = p["link"]
        for k in ("lan", "cap", "rook", "ir"):
            sel = sorted(n for n, a in ks.items() if kind(a) == k)
            if sel:
                out[k + "_keys"] = sel
        if out.get("ir_keys"):
            out["ir_unavailable"] = True          # no IR LED on the Deck
        return out

    # -- changes ----------------------------------------------------------------------

    def save(self, name, keys: dict | None = None, *, link=None, set_link: bool = False, merge: bool = True,
             icon=None, set_icon: bool = False) -> tuple[dict, bool]:
        """profile_save: make or change name. keys: key -> action (None takes it out).
        Answers the profile and whether it was made."""
        name = clean_name(name)
        given, drop = {}, []
        for k, v in (keys or {}).items():
            if not K.valid_key(k):
                raise ProfileError(f"bad args: {k} is not a key name: a named key ({', '.join(K.KEYS)}) or any "
                                   f"name the remote reports, [a-z0-9_], at most {K.MAX_KEY} characters")
            ck = K.canonical(k)
            if v is None:
                drop.append(ck)
            else:
                given[ck] = check_action(k, v)
        if set_link and link and (not isinstance(link, str) or len(link) > 60):
            raise ProfileError("bad args: link is a device (roku@address, webos@address), at most 60 characters")
        ic = icon_name(icon) if set_icon else None
        with self.lock:
            self.reload()
            p = self.find(name)
            created = p is None
            if created:
                if len(self.profiles) >= MAX_PROFILES:
                    raise ProfileError(f"full: the Deck keeps {MAX_PROFILES} profiles; profile_delete one first")
                p = {"name": name, "icon": None, "link": None, "keys": {}}
                self.profiles.append(p)
            ks = dict(p.get("keys") or {}) if merge else {}
            for k in drop:
                ks.pop(k, None)
            ks.update(given)
            p["keys"] = ks
            if set_link:
                p["link"] = link or None
            if set_icon:
                p["icon"] = ic
            self._save()
            return copy.deepcopy(p), created

    def delete(self, name: str) -> str:
        with self.lock:
            self.reload()
            p = self.find(name)
            if p is None:
                raise ProfileError(f"no profile called {name}")
            self.profiles.remove(p)
            if self.selected and self.selected.lower() == p["name"].lower():
                self.selected = None
            self._save()
            return p["name"]

    def rename(self, name: str, to: str) -> dict:
        to = clean_name(to)
        with self.lock:
            self.reload()
            p = self.find(name)
            if p is None:
                raise ProfileError(f"no profile called {name}")
            other = self.find(to)
            if other is not None and other is not p:
                raise ProfileError(f"there is already a profile called {to}")
            if self.selected == p["name"]:
                self.selected = to
            p["name"] = to
            self._save()
            return copy.deepcopy(p)

    def select(self, name: str | None) -> dict | None:
        with self.lock:
            self.reload()
            if name is None:
                self.selected = None
                self._save()
                return None
            p = self.find(name)
            if p is None:
                raise ProfileError(f"no profile called {name}")
            self.selected = p["name"]
            self._save()
            return copy.deepcopy(p)
