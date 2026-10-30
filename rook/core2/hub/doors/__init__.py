"""Doors: the ways into the hub (spec 10.1). Each one authenticates its
caller and calls the hub's async door API (``Hub.call``, ``Hub.workers_view``
...); every call ends at the one dispatch step.

A door is ``Door(hub, options)`` with ``name``, ``async start()`` and
``async stop()``; the config's ``doors:`` block names which ones run and
their options. MCP and the dashboard are later doors.
"""
from __future__ import annotations

import importlib

DOORS = {"http": "rook.core2.hub.doors.http:HttpDoor"}


def make_door(name: str, hub, options: dict):
    ref = DOORS.get(name)
    if ref is None:
        raise ValueError(f"unknown door {name!r} (known: {', '.join(sorted(DOORS))})")
    module, _, cls = ref.partition(":")
    return getattr(importlib.import_module(module), cls)(hub, options)
