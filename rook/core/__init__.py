"""Rook core: the pieces shared by the hub and every worker.

``rook.core`` is stdlib-only so it can ship inside the worker bundle
(``band-worker.pyz``, the Android app) as well as run on the hub:

* :mod:`rook.core.registry` - dot-namespaced capability registry with the
  declared ``limit``/``fields`` output contract enforced at dispatch.
* :mod:`rook.core.plugin` - the plugin contract (``Plugin``, ``@capability``,
  ``place``, ``setting``, ``resource``, versioning, discovery).
* :mod:`rook.core.host` - the plugin host (load, placement, lifecycle,
  failure isolation) used by both hub and worker.
* :mod:`rook.core.facts` - node facts (signed roles + self-reported hardware)
  and placement expressions.
* :mod:`rook.core.context` - per-dispatch caller identity.

See ``docs/design/plugins.md``.
"""

from .facts import NodeFacts, detect_facts, evaluate_placement
from .host import PluginHost
from .plugin import (CORE_API_VERSION, CapMeta, Placement, Plugin, Resource,
                     Setting, capability, place, resource, setting)
from .registry import CapabilityRegistry

__all__ = [
    "CORE_API_VERSION", "CapMeta", "CapabilityRegistry", "NodeFacts", "Placement",
    "Plugin", "PluginHost", "Resource", "Setting", "capability", "detect_facts",
    "evaluate_placement", "place", "resource", "setting",
]
