"""Compatibility shim: the per-dispatch call context now lives in :mod:`rook.core`.

Re-exports the same ``ContextVar`` object, so a value set through either
module is visible through both.
"""

from ..core.context import caller_identity, current_identity  # noqa: F401

__all__ = ["caller_identity", "current_identity"]
