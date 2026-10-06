"""Chaquopy entrypoint — boots rook.worker inside the Android app.

WorkerService (Kotlin) calls:
    worker_entry.start(hub, psk, name)   # blocks on its own asyncio loop
    worker_entry.stop()                  # from another thread, to shut down

We reuse the unmodified ``rook.worker`` package (staged in by stage_worker.py)
but swap two plugins for native Android backends:
  - screenshot.*  -> rook_android.plugins.screen      (MediaProjection)
  - hid.*         -> rook_android.plugins.hid_a11y     (AccessibilityService)

The stock ``screenshot``/``hid`` modules target Termux/X11/Win32 and would only
error on a Chaquopy host, so we exclude them from the loader and register the
native ones in their place.
"""

from __future__ import annotations

import asyncio
import logging
import pkgutil
import threading

log = logging.getLogger("rook.android.entry")

_stop_event: "asyncio.Event | None" = None
_loop: "asyncio.AbstractEventLoop | None" = None

# Builtin plugin module stems we replace with native bridges.
_NATIVE_OVERRIDES = {"screenshot", "hid", "battery", "selfupdate"}


def _builtin_enabled() -> list[str]:
    """All stock plugin modules except the ones we override natively."""
    import rook.worker.plugins as pkg
    names = [m.name for m in pkgutil.iter_modules(pkg.__path__)
             if not m.name.startswith("_")]
    return [n for n in names if n not in _NATIVE_OVERRIDES]


def _attach_native_plugins(worker) -> None:
    from rook_android.plugins.screen import AndroidScreenPlugin
    from rook_android.plugins.hid_a11y import AndroidHidPlugin
    from rook_android.plugins.battery_android import AndroidBatteryPlugin
    from rook_android.plugins.notify_android import AndroidNotifyPlugin
    from rook_android.plugins.ui_android import AndroidUiPlugin
    from rook_android.plugins.sms_android import AndroidSmsPlugin
    from rook_android.plugins.contacts_android import AndroidContactsPlugin
    from rook_android.plugins.calllog_android import AndroidCallLogPlugin
    from rook_android.plugins.location_android import AndroidLocationPlugin
    from rook_android.plugins.device_android import AndroidDevicePlugin
    from rook_android.plugins.speak_android import AndroidSpeakPlugin
    natives = (AndroidScreenPlugin(), AndroidHidPlugin(), AndroidBatteryPlugin(),
               AndroidNotifyPlugin(), AndroidUiPlugin(), AndroidSmsPlugin(),
               AndroidContactsPlugin(), AndroidCallLogPlugin(),
               AndroidLocationPlugin(), AndroidDevicePlugin(), AndroidSpeakPlugin())
    # Native plugins skip the plugin host, so give them the node's plugin state
    # directory ourselves; the default is relative to the cwd, which on Android
    # is the read-only root.
    data_root = getattr(getattr(worker, "host", None), "_data_root", None)
    for plugin in natives:
        if data_root:
            plugin.__dict__.setdefault("_data_root", data_root)
        # battery gates on a readable battery; screen/hid are always available()
        # (they report readiness per-call). Never announce a cap we can't back.
        try:
            if not plugin.available():
                log.info("native plugin ns=%s not available here, skipping", plugin.NAMESPACE)
                continue
        except Exception:
            log.warning("native plugin ns=%s available() raised, skipping", plugin.NAMESPACE)
            continue
        for dotpath, fn in plugin.caps().items():
            worker.registry.register(dotpath, fn)
        worker.plugins.append(plugin)
        log.info("attached native plugin ns=%s caps=%d",
                 plugin.NAMESPACE, len(plugin.caps()))


def _setup_logging() -> None:
    """Send worker logs to logcat (via stderr) and to a small file in the app's
    data, so a worker that stops can be diagnosed over adb (run-as) later."""
    import sys
    from logging.handlers import RotatingFileHandler
    from pathlib import Path
    root = logging.getLogger()
    if getattr(root, "_rook_android", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        path = Path.home() / ".rook-band-worker" / "android-worker.log"
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
