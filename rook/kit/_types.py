"""Value checks against the manifest type language, and duration parsing.

``check`` compiles a field spec with rook.kit.manifest's type compiler and
validates the value with the same JSON Schema validator the manifest
validator uses, so record fields validate exactly like cap args do.
jsonschema is imported on first check: records live on the hub, which has
it; workers import this module only for ``parse_duration`` (hooks).
"""

from __future__ import annotations

import functools
import json
import re
from typing import Any, Mapping

from .manifest.types import TypeSpecError, compile_type

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)")


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


@functools.lru_cache(maxsize=512)
def _validator(spec_json: str):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    schema = compile_type(json.loads(spec_json))
    try:
        Draft202012Validator.check_schema(schema)   # a bad `schema:` passthrough
    except SchemaError as e:
        raise TypeSpecError((), f"bad schema: {e.message}") from None
    return Draft202012Validator(schema)


def check(name: str, spec: Mapping[str, Any], value: Any) -> None:
    """Raise ValueError if ``value`` doesn't fit field ``spec``. None passes
    (required-ness is checked by the caller, which knows create from update)."""
    if value is None:
        return
    try:
        validator = _validator(json.dumps(spec, sort_keys=True, default=str))
    except TypeSpecError as e:
        raise ValueError(f"{name}: {e}") from None
    from jsonschema.exceptions import best_match
    err = best_match(validator.iter_errors(value))
    if err is None:
        return
    where = ".".join([name, *map(str, err.absolute_path)])
    if err.validator == "type":
        want = err.schema.get("x-rook-type", err.validator_value)
        raise ValueError(f"{where}: expected {want}, got {type(err.instance).__name__}")
    raise ValueError(f"{where}: {err.message}")
