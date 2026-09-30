"""Moved to :mod:`rook.hub.plugins.knowledge.search` (the knowledge hub plugin).
This alias keeps ``rook.knowledge.search`` imports working: it is the same module."""
import sys

from ..hub.plugins.knowledge import search as _moved

sys.modules[__name__] = _moved
