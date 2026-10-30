"""The manifest type language, compiled to JSON Schema.

One language for cap args and results, settings, record fields, event
payloads, contracts and component props::

    state: { type: str, enum: [Downloading, Seeding], required: true }
    tags:  { type: list, items: { type: str }, max: 10 }
    raw:   { schema: { type: object } }        # JSON Schema passthrough

``type`` defaults to ``str``. Compiled schemas carry ``x-rook-type`` (and
``x-rook-ref`` for refs) so the source type survives the round trip.
"""

from __future__ import annotations

import json

TYPES = ("str", "int", "float", "bool", "time", "duration", "markdown", "json",
         "file", "secret", "ref", "list", "map")

DURATION_PATTERN = r"^[0-9]+(\.[0-9]+)?(ms|s|m|h|d|w)$"

_BASE = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    # ISO 8601 text or epoch seconds: Rook stores epoch floats, people write dates.
    "time": {"type": ["string", "number"], "format": "date-time"},
    # Seconds, or a number with a unit.
    "duration": {"type": ["string", "number"], "pattern": DURATION_PATTERN, "minimum": 0},
    "markdown": {"type": "string", "contentMediaType": "text/markdown"},
    "json": {},
    # A file value; its shape (path, or name/mime/data) belongs to the kit's
    # ctx.file. TODO(castling:kit): narrow once ctx.file is settled.
    "file": {"type": ["object", "string"]},
    # The value is a vault name, never the secret itself.
    "secret": {"type": "string", "writeOnly": True},
    "ref": {"type": "string"},
    "list": {"type": "array"},
    "map": {"type": "object"},
}

# Which modifiers apply to which types (the rest apply to every type).
_ONLY = {
    "min": {"str", "markdown", "int", "float", "list", "duration"},
    "max": {"str", "markdown", "int", "float", "list", "duration"},
    "pattern": {"str", "markdown", "secret", "ref"},
    "items": {"list", "map"},
    "fields": {"map"},
    "to": {"ref"},
}

_BOUNDS = {
    "str": ("minLength", "maxLength"),
    "markdown": ("minLength", "maxLength"),
    "list": ("minItems", "maxItems"),
}


class TypeSpecError(ValueError):
    def __init__(self, path: tuple, message: str):
        super().__init__(message)
        self.path = path


def compile_type(spec: dict, path: tuple = ()) -> dict:
    """One field spec -> JSON Schema."""
    if "schema" in spec:
        out = dict(spec["schema"])
        if "doc" in spec:
            out.setdefault("description", spec["doc"])
        return out
    t = spec.get("type", "str")
    if t not in TYPES:
        raise TypeSpecError(path + ("type",), f"unknown type {t!r}")
    for mod in spec:
        if mod in _ONLY and t not in _ONLY[mod]:
            raise TypeSpecError(path + (mod,), f"{mod} does not apply to type {t}")
    if t == "ref" and "to" not in spec:
        raise TypeSpecError(path, "a ref needs to: <plugin>.<record>")
    out = dict(_BASE[t])
    out["x-rook-type"] = t
    if t == "list" and "items" in spec:
        out["items"] = compile_type(spec["items"], path + ("items",))
    if t == "map":
        if "fields" in spec:
            out.update(compile_fields(spec["fields"], path + ("fields",)))
        elif "items" in spec:
            out["additionalProperties"] = compile_type(spec["items"], path + ("items",))
    if t == "ref":
        out["x-rook-ref"] = spec["to"]
    lo, hi = _BOUNDS.get(t, ("minimum", "maximum"))
    if "min" in spec:
        out[lo] = spec["min"]
    if "max" in spec:
        out[hi] = spec["max"]
    for src, dst in (("enum", "enum"), ("pattern", "pattern"), ("default", "default"),
                     ("doc", "description")):
        if src in spec:
            out[dst] = spec[src]
    return out


def compile_fields(fields: dict, path: tuple = ()) -> dict:
    """A name -> field spec map (args, payload, props, map fields) -> an object
    schema that refuses unknown names."""
    return {
        "type": "object",
        "properties": {n: compile_type(s, path + (n,)) for n, s in fields.items()},
        "required": [n for n, s in fields.items() if s.get("required")],
        "additionalProperties": False,
    }


def setting_spec(spec: dict) -> dict:
    """A setting's field spec: ``secret: true`` means a vault name."""
    if spec.get("secret") and "type" not in spec:
        return {**spec, "type": "secret"}
    return spec


def type_key(spec: dict):
    """What diff and contract checks compare: the type, its ref target, its
    item type (or the raw schema)."""
    if "schema" in spec:
        return ("schema", json.dumps(spec["schema"], sort_keys=True))
    items = spec.get("items")
    return (spec.get("type", "str"), spec.get("to"), type_key(items) if items else None)
