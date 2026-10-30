"""Generated caps for record types: ``<plugin>.<record>.<verb>``.

Each verb becomes a real hub cap with a manifest-shaped spec (doc, risk,
scope, effects, args in the type language, result) and a ``(ctx, **args)``
handler, so the trust map can tell ``homework.item.update`` from
``homework.item.delete`` and nobody writes an ``action=`` switch again.

Default risks: list/get/search read, create/update/archive write, delete write
with the ``destructive`` effect; the block's ``risk:`` overrides per verb.
Scope: ``self`` for private types, else ``multi-user`` (records are band-wide
hub state).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .store import RecordStore, RecordType

__all__ = ["GeneratedCap", "record_caps"]

_RISK = {"list": "read", "get": "read", "search": "read",
         "create": "write", "update": "write", "archive": "write", "delete": "write"}
_FIELD_KEYS = ("type", "doc", "enum", "min", "max", "pattern", "items", "fields", "to", "default")


@dataclass(frozen=True)
class GeneratedCap:
    name: str
    spec: dict
    handler: Callable[..., Any]


def _field_args(rtype: RecordType, creating: bool) -> dict:
    """Field args. Only create keeps required and default: an update arg that
    a validator filled with its default would overwrite the stored value."""
    out = {}
    for f, spec in rtype.rdef.fields.items():
        arg = {k: spec[k] for k in _FIELD_KEYS if k in spec and (creating or k != "default")}
        if creating and spec.get("required"):
            arg["required"] = True
        out[f] = arg
    return out


def _args(rtype: RecordType, verb: str) -> dict:
    rid = {"id": {"type": "str", "required": True, "doc": "Record id."}}
    if verb == "list":
        return {"where": {"type": "map", "doc": "Field = value filters."},
                "archived": {"type": "bool", "default": False, "doc": "List archived records instead."},
                "limit": {"type": "int", "default": 50, "min": 1, "max": 500},
                "offset": {"type": "int", "default": 0, "min": 0},
                "order": {"type": "str", "doc": "Field to sort by; prefix - for descending (default -updated)."}}
    if verb == "get":
        return {**rid, "history": {"type": "bool", "default": False},
                "backlinks": {"type": "bool", "default": False}}
    if verb == "search":
        return {"query": {"type": "str", "required": True, "doc": "Words to find (all must match)."},
                "limit": {"type": "int", "default": 20, "min": 1, "max": 200}}
    if verb == "create":
        return _field_args(rtype, creating=True)
    revision = {"revision": {"type": "int", "required": rtype.rdef.history,
                             "doc": "Current revision (refused if the record changed since)."}}
    if verb == "update":
        return {**rid, **revision, **_field_args(rtype, creating=False)}
    if verb == "archive":
        return {**rid, **revision, "restore": {"type": "bool", "default": False,
                                               "doc": "Bring an archived record back."}}
    return rid   # delete


_DOC = {"list": "List {n} records.", "get": "Get one {n} record.",
        "search": "Find {n} records by words.", "create": "Create a {n} record.",
        "update": "Change a {n} record.", "archive": "Archive (or restore) a {n} record.",
        "delete": "Delete a {n} record for good."}


def _handler(store: RecordStore, full: str, verb: str) -> Callable[..., Any]:
    def handler(ctx, **args):
        return getattr(store.type(full), verb)(ctx.caller, **args)
    handler.__name__ = f"{full}.{verb}"
    return handler


def record_caps(store: RecordStore, plugin: str) -> dict[str, GeneratedCap]:
    """Every generated cap for a plugin's registered record types."""
    caps: dict[str, GeneratedCap] = {}
    for rtype in store.types(plugin):
        rdef = rtype.rdef
        for verb in rdef.verbs:
            name = f"{rdef.full}.{verb}"
            spec = {"doc": _DOC[verb].format(n=rdef.full),
                    "risk": rdef.risk.get(verb, _RISK[verb]),
                    "scope": "self" if rdef.private else "multi-user",
                    "effects": ["destructive"] if verb == "delete" else [],
                    "args": _args(rtype, verb),
                    "result": {"type": "list" if verb in ("list", "search") else "map"},
                    "idempotent": verb in ("list", "get", "search"),
                    "on": "hub"}
            caps[name] = GeneratedCap(name, spec, _handler(store, rdef.full, verb))
    return caps
