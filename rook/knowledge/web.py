"""Moved to :mod:`rook.hub.plugins.knowledge.web` (the knowledge hub plugin).
This alias keeps ``rook.knowledge.web`` imports working: it is the same module."""
import sys

from ..hub.plugins.knowledge import web as _moved

sys.modules[__name__] = _moved
