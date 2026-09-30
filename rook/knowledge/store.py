"""Moved to :mod:`rook.hub.plugins.knowledge.store` (the knowledge hub plugin).
This alias keeps ``rook.knowledge.store`` imports working: it is the same module."""
import sys

from ..hub.plugins.knowledge import store as _moved

sys.modules[__name__] = _moved
