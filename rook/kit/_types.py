"""Value checks for the manifest type language, and duration parsing.

TODO(castling:manifest): a local stand-in. rook.kit.manifest owns the type
language (and its JSON Schema compiler); once it lands, swap these for its
checker so records validate exactly like cap args do. Only what the kit needs
is here: type, required, enum, min, max, pattern.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)")

_PY = {
    "str": str, "markdown": str, "secret": str, "ref": str, "file": str, "duration": str,
    "int": int, "float": (int, float), "bool": bool,
    "list": list, "map": dict,
}


def parse_duration(value: Any) -> float:
    """Seconds from ``90d``, ``5m``, ``1h30m``, ``250ms`` or a plain number."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    pos, total = 0, 0.0
    for m in _DUR.finditer(text):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(text) or not text:
        raise ValueError(f"not a duration: {value!r}")
    return total


def check(name: str, spec: Mapping[str, Any], value: Any) -> None:
    """Raise ValueError if ``value`` doesn't fit field ``spec``. None passes
    (required-ness is checked by the caller, which knows create from update)."""
    if value is None:
        return
    kind = spec.get("type", "str")
    if kind == "time":
        ok = isinstance(value, (str, int, float)) and not isinstance(value, bool)
    elif kind == "json":
        ok = True
    elif kind in _PY:
        want = _PY[kind]
        ok = isinstance(value, want) and not (isinstance(value, bool) and kind in ("int", "float"))
    else:
        raise ValueError(f"{name}: unknown type {kind!r}")
    if not ok:
        raise ValueError(f"{name}: expected {kind}, got {type(value).__name__}")
    if kind == "duration":
        parse_duration(value)
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"{name}: must be one of {', '.join(map(str, spec['enum']))}")
    size = len(value) if isinstance(value, (str, list, dict)) else value
    if isinstance(size, (int, float)):
        if "min" in spec and size < spec["min"]:
            raise ValueError(f"{name}: below min {spec['min']}")
        if "max" in spec and size > spec["max"]:
            raise ValueError(f"{name}: above max {spec['max']}")
    if "pattern" in spec and isinstance(value, str) and not re.fullmatch(spec["pattern"], value):
        raise ValueError(f"{name}: doesn't match {spec['pattern']}")
