"""Chaquopy entrypoint — boots rook.worker inside the Android app.

WorkerService (Kotlin) calls:
    worker_entry.start(hub, psk, name)   # blocks on its own asyncio loop
    worker_entry.stop()                  # from another thread, to shut down

We reuse the unmodified ``rook.worker`` package (staged in by stage_worker.py)
and add the native Android plugins (``rook_android.plugins``) to the same
plugin host. Placement picks one of each pair: the worker reports the fact
``app="android"``, the native plugins are placed there (``ANDROID_APP``) and
the stock ``screenshot``/``hid``/``battery``/``selfupdate`` modules, which
target Termux/X11/Win32, are placed everywhere else. So every plugin gets the
host's status, settings, data directory and cap-collision safety.
"""

from __future__ import annotations

import logging

log = logging.getLogger("rook.android.entry")

#: Plugin packages the embedded worker's host discovers, in order.
PLUGIN_PACKAGES = ["rook.worker.plugins", "rook_android.plugins"]
#: Extra node facts: what the native plugins' placement matches.
FACTS = {"app": "android"}


def worker_kwargs() -> dict:
    """Keyword arguments for :class:`rook.worker.core.Worker` on the phone."""
    # The native plugins' declared tiers join this node's tier floor, as the
    # stock plugins' do.
    from rook.core import authz
    authz.add_floor_package("rook_android.plugins")
    return {"plugins_pkg": list(PLUGIN_PACKAGES), "facts": dict(FACTS)}


def _setup_logging() -> None:
    """Send worker logs to logcat (via stderr) and to a small file in the app's
    data, so a worker that stops can be diagnosed over adb (run-as) later."""
    import sys
    from logging.handlers import RotatingFileHandler
    root = logging.getLogger()
    if getattr(root, "_rook_android", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        from rook.core.paths import worker_state_path
        path = worker_state_path("android-worker.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=1))
    except Exception:  # logging must never stop the worker
        pass
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.INFO)
    root._rook_android = True


def start(hub: str, psk: str, name: str) -> None:
    """Run/reconnect the embedded worker without re-executing app_process."""
    _setup_logging()
    from rook_android.worker_runtime import start as run
    log.info("worker runtime starting as %s -> %s", name, hub)
    try:
        run(hub,psk,name)
    except BaseException:
        log.exception("worker runtime crashed")
        raise
    log.warning("worker runtime exited")


def stop() -> None:
    from rook_android.worker_runtime import stop as stop_runtime
    stop_runtime()
