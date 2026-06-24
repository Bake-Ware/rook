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
_NATIVE_OVERRIDES = {"screenshot", "hid"}


def _builtin_enabled() -> list[str]:
    """All stock plugin modules except the ones we override natively."""
    import rook.worker.plugins as pkg
    names = [m.name for m in pkgutil.iter_modules(pkg.__path__)
             if not m.name.startswith("_")]
    return [n for n in names if n not in _NATIVE_OVERRIDES]


def _attach_native_plugins(worker) -> None:
    from rook_android.plugins.screen import AndroidScreenPlugin
    from rook_android.plugins.hid_a11y import AndroidHidPlugin
    for plugin in (AndroidScreenPlugin(), AndroidHidPlugin()):
        for dotpath, fn in plugin.caps().items():
            worker.registry.register(dotpath, fn)
        worker.plugins.append(plugin)
        log.info("attached native plugin ns=%s caps=%d",
                 plugin.NAMESPACE, len(plugin.caps()))


def start(hub: str, psk: str, name: str) -> None:
    """Blocking. Runs the worker until stop() is called. Called on a JVM thread."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    host, _, port = hub.partition(":")
    if not port:
        raise ValueError(f"hub must be host:port, got {hub!r}")
    use_ws = port in ("443", "8443")  # TLS ports -> wss, matches the worker default

    from rook.worker.core import Worker
    from rook.worker.transports.telesthete_hub import TelestheteHubTransport

    transport = TelestheteHubTransport(
        psk=psk, hub_host=host, hub_port=int(port), use_ws=use_ws,
    )
    worker = Worker(transport=transport, enabled=_builtin_enabled(), name=name)
    _attach_native_plugins(worker)

    async def runner() -> None:
        global _stop_event, _loop
        _loop = asyncio.get_running_loop()
        _stop_event = asyncio.Event()
        task = asyncio.create_task(worker.run())
        await _stop_event.wait()
        await worker.shutdown()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            pass

    asyncio.run(runner())


def stop() -> None:
    """Thread-safe shutdown signal. Called from the JVM, off the worker thread."""
    loop, ev = _loop, _stop_event
    if loop is not None and ev is not None:
        loop.call_soon_threadsafe(ev.set)
