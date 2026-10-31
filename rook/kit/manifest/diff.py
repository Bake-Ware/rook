"""Breaking changes between two versions of a plugin, and the semver check:
the hub refuses a breaking change without a major bump."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .load import Manifest, normalize
from .types import type_key
from .validate import Issue, pointer

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$")


@dataclass(frozen=True)
class Change:
    kind: str  # cap-removed | arg-removed | arg-required | type-changed | result-narrowed
    path: str
    message: str


def _data(m) -> dict:
    """A Manifest, or raw parsed (valid, not yet normalized) manifest data."""
    return m.data if isinstance(m, Manifest) else normalize(m)


def diff(old, new) -> list[Change]:
    """What in ``new`` breaks callers of ``old``. A cap renamed with its old
    name kept in ``aliases`` is not removed."""
    old_caps, new_caps = _data(old)["caps"], _data(new)["caps"]
    alias_of = {a: name for name, c in new_caps.items() for a in c.get("aliases", ())}
    out = []
    for name, oc in old_caps.items():
        current = name if name in new_caps else alias_of.get(name)
        if current is None:
            out.append(Change("cap-removed", pointer(("caps", name)), f"{name} was removed"))
        else:
            out += _cap_changes(name, oc, new_caps[current])
    return out


def _cap_changes(name, oc, nc) -> list[Change]:
    out = []
    oargs, nargs = oc.get("args", {}), nc.get("args", {})
    for arg, os_ in oargs.items():
        path = pointer(("caps", name, "args", arg))
        ns = nargs.get(arg)
        if ns is None:
            out.append(Change("arg-removed", path, f"{name}: arg {arg} was removed"))
            continue
        if type_key(os_) != type_key(ns):
            out.append(Change("type-changed", path, f"{name}: arg {arg} changed type"))
        elif "enum" in ns and ("enum" not in os_ or any(v not in ns["enum"] for v in os_["enum"])):
            out.append(Change("type-changed", path, f"{name}: arg {arg} accepts fewer values"))
        if ns.get("required") and not os_.get("required"):
            out.append(Change("arg-required", path, f"{name}: arg {arg} became required"))
    for arg, ns in nargs.items():
        if arg not in oargs and ns.get("required"):
            out.append(Change("arg-required", pointer(("caps", name, "args", arg)),
                              f"{name}: new required arg {arg}"))
    if "result" in oc:
        path = pointer(("caps", name, "result"))
        nr = nc.get("result")
        if nr is None:
            out.append(Change("result-narrowed", path, f"{name}: no longer declares a result"))
        elif type_key(oc["result"]) != type_key(nr):
            out.append(Change("type-changed", path, f"{name}: result changed type"))
        else:
            gone = set(oc["result"].get("fields", {})) - set(nr.get("fields", {}))
            if gone:
                out.append(Change("result-narrowed", path,
                                  f"{name}: result lost {', '.join(sorted(gone))}"))
    return out


def semver_key(version: str):
    m = _SEMVER.match(version)
    if not m:
        raise ValueError(f"not a semver version: {version!r}")
    pre = m.group(4)
    # A release sorts after its prereleases; numeric ids before alphanumeric.
    ids = (1,) if pre is None else (0,) + tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), ids)


def check_bump(old, new) -> list[Issue]:
    """Errors when ``new`` doesn't raise the version, or breaks callers without
    a major bump (for 0.x, a minor bump may break: 0.1 -> 0.2)."""
    od, nd = _data(old), _data(new)
    ov, nv = semver_key(od["version"]), semver_key(nd["version"])
    if nv <= ov:
        return [Issue(pointer(("version",)), "version-not-increased", "error",
                      f"{nd['version']} is not newer than {od['version']}")]
    if nv[0] > ov[0] or (ov[0] == 0 and nv[1] > ov[1]):
        return []
    return [Issue(c.path, "breaking-change", "error",
                  f"{c.message}: needs a major version bump ({od['version']} -> {nd['version']})")
            for c in diff(Manifest(od), Manifest(nd))]


_COMPARATOR = re.compile(r"^(>=|<=|==|>|<|=)?\s*(\d+(?:\.\d+){0,2}(?:-[0-9A-Za-z.-]+)?)$")


def _padded(v: str) -> str:
    core, dash, pre = v.partition("-")
    parts = core.split(".")
    return ".".join(parts + ["0"] * (3 - len(parts))) + dash + pre


def satisfies(version: str, spec: str) -> bool:
    """Whether ``version`` is in the range ``spec`` (``requires.plugins`` and
    ``requires.rook``): comparators joined by commas, each ``>=``, ``>``,
    ``<=``, ``<``, ``==`` or a bare version (exact); a version may leave out
    minor and patch (``>=0.5,<0.6``). Blank or ``*``: any. Raises ValueError
    for a range it can't read."""
    spec = spec.strip()
    if spec in ("", "*"):
        return True
    have = semver_key(version)
    for part in spec.split(","):
        m = _COMPARATOR.match(part.strip())
        if not m:
            raise ValueError(f"not a version range: {spec!r}")
        op, want = m.group(1) or "==", semver_key(_padded(m.group(2)))
        ok = {">=": have >= want, ">": have > want, "<=": have <= want, "<": have < want,
              "==": have == want, "=": have == want}[op]
        if not ok:
            return False
    return True
