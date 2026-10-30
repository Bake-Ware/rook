"""``parse:`` turns an action's raw output into data; ``returns:`` picks or
renames fields of it; ``coerce`` fits the result to the cap's result type."""

from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import re
from collections.abc import Mapping
from typing import Any

from .errors import ActionError

_KV = re.compile(r"\s*([^:=]+?)\s*[:=]\s*(.*?)\s*")


def dig(value: Any, path: str, default: Any = None) -> Any:
    """``a.b.0.c``: map keys and list indexes."""
    for key in path.split("."):
        if isinstance(value, Mapping) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.lstrip("-").isdigit() and -len(value) <= int(key) < len(value):
            value = value[int(key)]
        else:
            return default
    return value


def file_result(data: bytes, name: str | None = None, mime: str | None = None) -> dict:
    """A returned file: same keys today's caps use (``data`` is base64)."""
    if not mime and name:
        mime = mimetypes.guess_type(name)[0]
    return {"name": name or "output", "mime": mime or "application/octet-stream",
            "bytes": len(data), "data": base64.b64encode(data).decode("ascii")}


def parse(data: bytes | str, how: Any, *, name: str | None = None, mime: str | None = None) -> Any:
    raw = data.encode() if isinstance(data, str) else data
    if how == "bytes":
        return file_result(raw, name, mime)
    s = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
    if isinstance(how, Mapping) and "regex" in how:
        try:
            rx = re.compile(how["regex"], re.MULTILINE)
        except re.error as e:
            raise ActionError(f"parse regex is invalid: {e}") from None
        if how.get("each"):   # one item per matching line
            return [m.groupdict() for m in map(rx.search, s.splitlines()) if m]
        m = rx.search(s)
        return m.groupdict() if m else None
    if how == "text":
        return s.rstrip("\r\n")
    if how == "lines":
        return [line.rstrip() for line in s.splitlines() if line.strip()]
    if how == "json":
        if not s.strip():
            return None
        try:
            return json.loads(s)
        except ValueError:
            raise ActionError(f"output is not JSON: {s[:200]!r}") from None
    if how == "kv":
        return {m[1]: m[2] for m in map(_KV.fullmatch, s.splitlines()) if m}
    if how == "csv":
        return list(csv.DictReader(io.StringIO(s)))
    raise ActionError(f"unknown parse {how!r}")


def pick(value: Any, returns: Any) -> Any:
    """``[a, b.c]`` keeps those fields (named by their last part); ``{new:
    old.path}`` renames. Applied to each item of a list."""
    if returns is None:
        return value
    if isinstance(value, list):
        return [pick(v, returns) for v in value]
    if not isinstance(value, Mapping):
        raise ActionError(f"returns needs an object, the action gave {type(value).__name__}")
    if isinstance(returns, Mapping):
        return {new: dig(value, str(path)) for new, path in returns.items()}
    return {str(path).rsplit(".", 1)[-1]: dig(value, str(path)) for path in returns}


_BOOLS = {"true": True, "yes": True, "on": True, "1": True,
          "false": False, "no": False, "off": False, "0": False}


def coerce(value: Any, spec: Mapping | None, where: str = "result") -> Any:
    """Text from parse into the cap's result types: "87" -> 87 for an int
    field, "yes" -> True for a bool, 87 -> "87" for a str. Blank text for a
    number or bool is None. Raw ``schema:`` results are left alone."""
    if not spec or value is None or "schema" in spec:
        return value
    t = spec.get("type", "str")
    if t == "list" and isinstance(value, list) and spec.get("items"):
        return [coerce(v, spec["items"], f"{where}.{i}") for i, v in enumerate(value)]
    if t == "map" and isinstance(value, Mapping):
        fields, items = spec.get("fields"), spec.get("items")
        if fields:
            return {k: coerce(v, fields.get(k), f"{where}.{k}") for k, v in value.items()}
        return {k: coerce(v, items, f"{where}.{k}") for k, v in value.items()} if items else value
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return value
    if t in ("str", "markdown", "url", "path") and not isinstance(value, str):
        return str(value)
    if t not in ("int", "float", "bool"):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        if t == "bool":
            return _BOOLS[str(value).lower()]
        if t == "float":
            return float(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                value = float(value)
        if value != int(value):
            raise ValueError
        return int(value)
    except (KeyError, ValueError, OverflowError):
        raise ActionError(f"{where}: {value!r} is not {'an' if t == 'int' else 'a'} {t}") from None
