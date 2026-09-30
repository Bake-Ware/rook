"""Moved to :mod:`rook.hub.plugins.knowledge.maintenance` (the knowledge hub plugin).
This alias keeps ``rook.knowledge.maintenance`` imports working: it is the same module."""
import sys

from ..hub.plugins.knowledge import maintenance as _moved

sys.modules[__name__] = _moved
