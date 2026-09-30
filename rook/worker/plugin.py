"""Worker plugin API: a compatibility layer over :mod:`rook.core.plugin`.

Worker plugins import ``Plugin`` and ``capability`` from here, as they always
have; both are the core classes, so the same plugin runs on the hub or a
worker depending only on its placement. See ``docs/design/plugins.md``.
"""

from __future__ import annotations

from ..core.plugin import (  # noqa: F401
    CORE_API_VERSION, DEFAULT_PLACEMENT, CapMeta, Placement, Plugin, Resource, Setting,
    _UNSET, capability, load_plugins, place, resource, setting,
)
from ..core.registry import CapabilityRegistry  # noqa: F401

__all__ = [
    "CORE_API_VERSION", "CapMeta", "CapabilityRegistry", "Placement", "Plugin",
    "Resource", "Setting", "capability", "load_plugins", "place", "resource", "setting",
]
