"""Moved to :mod:`rook.hub.plugins.knowledge.service` (the knowledge hub plugin).
This alias keeps ``rook.knowledge.service`` imports working: it is the same module."""
import sys

from ..hub.plugins.knowledge import service as _moved

sys.modules[__name__] = _moved
