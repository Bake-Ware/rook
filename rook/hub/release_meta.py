"""Release metadata: the ``release.json`` every Rook GitHub release carries.

``tools/release_prep.py --release-json`` writes it in the release workflow
from the stamped tree; the hub's update service (rook/hub/plugins/updates)
reads it back to decide what an update means for the band::

    {"schema": 1, "typ": "rook-release",
     "tag": "v0.3.0", "version": "0.3.0", "build": 530,
     "name": "530.lumpy.moose", "commit": "abc1234",
     "channel": "stable",                 # or "prerelease"
     "breaking": false,                   # raises a compat floor (or marked)
     "breaking_reasons": [],
     "min_worker_build": 142,             # rook/core/compat.py at that commit
     "min_android_app": "0.4.1",
     "min_hub_build": 230,
     "relay": "v0.3.0",                   # telesthete relay the release pins
     "android_app": {"version_name": "0.4.15", "version_code": 19},
     "firmware": {"esp32": "0.6.7", "rp2040": "0.1.0"}}

Stdlib only, no rook imports: the release tool runs it on a bare checkout.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Iterable

SCHEMA = 1
TYP = "rook-release"
FLOORS = ("min_worker_build", "min_android_app", "min_hub_build")
# compat.py names -> release.json keys
_COMPAT_NAMES = {"MIN_WORKER_BUILD": "min_worker_build", "MIN_ANDROID_APP": "min_android_app",
                 "MIN_HUB_BUILD": "min_hub_build"}


def version_tuple(v: Any) -> tuple:
    """``"0.4.15"`` -> ``(0, 4, 15)``; a pre-release sorts below its release
    (``0.3.0rc1`` < ``0.3.0``). Unparseable -> ``()`` (sorts lowest)."""
    if v is None:
        return ()
    s = str(v).strip().lstrip("vV")
    m = re.match(r"^(\d+(?:\.\d+)*)(?:[-.]?(a|b|rc|dev)\.?(\d+))?", s)
    if not m:
        return ()
    nums = tuple(int(x) for x in m.group(1).split("."))
    # Trailing zeros don't change a version: 0.4 == 0.4.0.
    while len(nums) > 1 and nums[-1] == 0:
        nums = nums[:-1]
    pre = m.group(2)
    rank = {"dev": 0, "a": 1, "b": 2, "rc": 3}.get(pre or "", 4)
    return nums + (-1, rank, int(m.group(3) or 0)) if pre else nums + (0,)


def version_lt(a: Any, b: Any) -> bool:
    ta, tb = version_tuple(a), version_tuple(b)
    if not ta or not tb:
        return False      # unknown versions are never called "too old"
    return ta < tb


# -- reading the tree (release tool side) ------------------------------------

def compat_floors(root: Path) -> dict:
    """The floors in ``rook/core/compat.py`` of a checkout (parsed, not imported)."""
    tree = ast.parse((root / "rook" / "core" / "compat.py").read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in _COMPAT_NAMES:
                    out[_COMPAT_NAMES[t.id]] = ast.literal_eval(node.value)
    missing = [k for k in FLOORS if k not in out]
    if missing:
        raise ValueError(f"rook/core/compat.py has no {', '.join(missing)}")
    return out


def firmware_versions(root: Path) -> dict:
    """Firmware versions in the tree: ESP32 ``ROOK_FW_VERSION`` and the
    RP2040 dongle's ``VERSION``. Missing sources are left out."""
    out = {}
    esp = root / "firmware" / "include" / "config.h"
    if esp.is_file():
        m = re.search(r'#define\s+ROOK_FW_VERSION\s+"([^"]+)"', esp.read_text(encoding="utf-8"))
        if m:
            out["esp32"] = m.group(1)
    rp = root / "firmware-rp2040" / "src" / "main.cpp"
    if rp.is_file():
        m = re.search(r'\bVERSION\[\]\s*=\s*"([^"]+)"', rp.read_text(encoding="utf-8"))
        if m:
            out["rp2040"] = m.group(1)
    return out


def android_app_version(root: Path) -> dict:
    """The APK version the tree builds (build.gradle defaults), or {}."""
    gradle = root / "android" / "app" / "build.gradle"
    if not gradle.is_file():
        return {}
    text = gradle.read_text(encoding="utf-8")
    code = re.search(r'ROOK_APK_VERSION_CODE"\)\s*\?:\s*"(\d+)"', text)
    name = re.search(r'ROOK_APK_VERSION_NAME"\)\s*\?:\s*"([^"]+)"', text)
    out = {}
    if name:
        out["version_name"] = name.group(1)
    if code:
        out["version_code"] = int(code.group(1))
    return out


def floors_raised(new: dict, previous: dict | None) -> list[str]:
    """Human reasons a release with floors ``new`` is breaking against the
    release ``previous`` (its release.json), [] when nothing was raised."""
    if not previous:
        return []
    out = []
    a, b = previous.get("min_worker_build"), new.get("min_worker_build")
    if isinstance(a, int) and isinstance(b, int) and b > a:
        out.append(f"workers older than build {b} are no longer supported (was {a})")
    a, b = previous.get("min_android_app"), new.get("min_android_app")
    if a and b and version_lt(a, b):
        out.append(f"the Android app older than {b} is no longer supported (was {a})")
    a, b = previous.get("min_hub_build"), new.get("min_hub_build")
    if isinstance(a, int) and isinstance(b, int) and b > a:
        out.append(f"workers and the rook CLI need a hub of build {b} or newer (was {a})")
    return out


def build_release_json(root: Path, *, tag: str, version: str, build: int, name: str,
                       commit: str, relay: str, previous: dict | None = None,
                       breaking: bool = False, breaking_note: str = "") -> dict:
    floors = compat_floors(root)
    pre = bool(re.search(r"(a|b|rc|dev)\d*$", version))
    reasons = floors_raised(floors, previous)
    if breaking and breaking_note:
        reasons.insert(0, breaking_note)
    elif breaking and not reasons:
        reasons.append("marked breaking by the release")
    return {
        "schema": SCHEMA, "typ": TYP, "tag": tag, "version": version, "build": int(build),
        "name": name, "commit": commit, "channel": "prerelease" if pre else "stable",
        "breaking": bool(breaking or reasons), "breaking_reasons": reasons,
        **floors, "relay": relay,
        "android_app": android_app_version(root),
        "firmware": firmware_versions(root),
    }


# -- reading it back (hub side) ----------------------------------------------

def parse_release_json(data: Any) -> dict:
    """Validate a downloaded release.json; returns a cleaned copy. Raises
    ValueError when it is not one (unknown keys are dropped, never trusted
    as anything but data)."""
    if not isinstance(data, dict) or data.get("typ") != TYP:
        raise ValueError("not a Rook release.json")
    if data.get("schema") != SCHEMA:
        raise ValueError(f"unsupported release.json schema {data.get('schema')!r}")
    out: dict = {}
    for k in ("tag", "version", "name", "commit", "relay"):
        v = data.get(k)
        out[k] = str(v)[:80] if isinstance(v, (str, int)) else ""
    b = data.get("build")
    out["build"] = b if isinstance(b, int) and b >= 0 else 0
    out["channel"] = "prerelease" if data.get("channel") == "prerelease" else "stable"
    out["breaking"] = bool(data.get("breaking"))
    reasons = data.get("breaking_reasons")
    out["breaking_reasons"] = ([str(r)[:300] for r in reasons[:10]]
                               if isinstance(reasons, list) else [])
    mw = data.get("min_worker_build")
    out["min_worker_build"] = mw if isinstance(mw, int) and mw >= 0 else 0
    out["min_android_app"] = str(data.get("min_android_app") or "")[:40]
    mh = data.get("min_hub_build")
    out["min_hub_build"] = mh if isinstance(mh, int) and mh >= 0 else 0
    app = data.get("android_app") if isinstance(data.get("android_app"), dict) else {}
    out["android_app"] = {k: app[k] for k in ("version_name", "version_code")
                          if isinstance(app.get(k), (str, int))}
    fw = data.get("firmware") if isinstance(data.get("firmware"), dict) else {}
    out["firmware"] = {str(k)[:20]: str(v)[:40] for k, v in list(fw.items())[:10]}
    return out


def floors_of(meta: dict) -> dict:
    return {k: meta.get(k) for k in FLOORS}


def raised_against(meta: dict, current: dict) -> list[str]:
    """What ``meta`` raises compared with the floors this hub runs with now."""
    return floors_raised(floors_of(meta), current)


def below_floor(target: dict, floors: dict) -> str:
    """Why a band member (``kind``, ``build``, ``app_version``) is below
    ``floors``, in plain words, or "" when it is not. Firmware is never
    covered by the floors (see compat.py)."""
    kind = target.get("kind")
    if kind == "app":
        need = floors.get("min_android_app")
        have = target.get("app_version")
        if target.get("platform") == "android" and need and have and version_lt(have, need):
            return f"Rook app {have} is older than {need}"
        b, need_b = target.get("build"), floors.get("min_worker_build")
        if isinstance(b, int) and b > 0 and isinstance(need_b, int) and b < need_b:
            return f"its built-in worker (build {b}) is older than build {need_b}"
        return ""
    if kind == "worker":
        b, need_b = target.get("build"), floors.get("min_worker_build")
        if isinstance(b, int) and b > 0 and isinstance(need_b, int) and b < need_b:
            return f"build {b} is older than build {need_b}"
    return ""


def cut_off(targets: Iterable[dict], floors: dict) -> list[dict]:
    """The members ``floors`` would leave behind, each with its ``why``."""
    out = []
    for t in targets:
        why = below_floor(t, floors)
        if why:
            out.append({**t, "why": why})
    return out
