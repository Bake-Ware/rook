"""Compatibility shim: the capability registry now lives in :mod:`rook.core`."""

from ..core.registry import CapabilityRegistry, Handler  # noqa: F401

__all__ = ["CapabilityRegistry", "Handler"]
