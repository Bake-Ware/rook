"""Shared Chaquopy → Android helpers for the native rook plugins.

Every native plugin needs the app Context and a couple of reflection helpers.
The Context is fetched via ``ActivityThread.currentApplication()`` so no Kotlin
has to hand it over. All helpers degrade to None/False off a Chaquopy host so
the modules still import (and their plugins just report unavailable).
"""

from __future__ import annotations

try:
    from java import jclass, cast  # provided by Chaquopy at runtime
    _ActivityThread = jclass("android.app.ActivityThread")
    _PackageManager = jclass("android.content.pm.PackageManager")
except Exception:  # pragma: no cover - only importable on a Chaquopy host
    jclass = None
    cast = None
    _ActivityThread = None
    _PackageManager = None


def app_context():
    """The process-wide Application context, or None off-device."""
    if _ActivityThread is None:
        return None
    try:
        return _ActivityThread.currentApplication()
    except Exception:
        return None


def has_permission(perm: str) -> bool:
    """True if a dangerous runtime permission is granted (checkSelfPermission)."""
    ctx = app_context()
    if ctx is None or _PackageManager is None:
        return False
    try:
        return ctx.checkSelfPermission(perm) == _PackageManager.PERMISSION_GRANTED
    except Exception:
        return False


def on_device() -> bool:
    return app_context() is not None
