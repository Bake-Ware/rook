"""The 0.5 worker inside the Rook Android app (Chaquopy).

The app's foreground service (WorkerService.kt) runs Python on a thread of
its own; this is the module it calls:

    from rook.core2.worker import android
    android.start(files_dir + "/rook/worker.yaml")   # blocks until stop()
    android.stop()                                    # from another thread
    android.kick()                                    # the default network changed

The config is the ordinary worker config (``python -m rook.core2.worker``);
enrollment writes it and the anchor into the app's files. What differs:

- **No processes.** The phone plugins (``platforms: [android]``) reach Android
  through Java (rook.kit.android), never a shell, and the worker launches
  nothing. Chaquopy builds its Python packages into the APK, so a plugin
  that needs more (``requires.python``) is refused at install.
- **os is android.** Android sets ANDROID_ROOT in every app process, so only
  ``platforms: [android]`` plugins load (``termux`` is the Termux app).
- **No signals.** The service calls ``stop()``; ``kick()`` redials every
  band after a network change instead of waiting for the link to notice.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from .runtime import Config, Worker, load_config, setup_logging

log = logging.getLogger("rook.worker.android")

_lock = threading.Lock()
_running: tuple[asyncio.AbstractEventLoop, asyncio.Event, "AndroidWorker"] | None = None
_stop_asked = False


def _no_pip(pins: list[str]) -> None:
    raise RuntimeError("the Android app can't install Python packages "
                       f"({', '.join(pins)}): they are built into the APK")


class AndroidWorker(Worker):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.reexec = None      # worker.restart would exec over the app's own process: the app restarts it

    async def _load_plugins(self) -> None:
        self.installer.install_python = _no_pip
        await super()._load_plugins()

    async def redial(self) -> None:
        """Drop and rebuild every band's link (the old socket may sit on a
        network that is gone). Hub-loss clocks start over with the new link."""
        for spec in [*self.cfg.bands, *self._joined().values()]:
            try:
                link = self.make_link(spec)
            except (ValueError, KeyError, TypeError) as e:
                log.error("band not redialled: %s", e)
                continue
            if link.band in self.transport.bands:
                await self.transport.remove(link.band)
                await self.transport.add(link)
        log.info("bands redialled")


def start(config: str | Config) -> None:
    """Run the worker until :func:`stop`. A ``stop()`` that arrives while it
    is still starting ends it as soon as it is up."""
    global _stop_asked
    with _lock:
        _stop_asked = False
    cfg = config if isinstance(config, Config) else load_config(config)
    setup_logging(cfg.log)          # stderr: logcat
    asyncio.run(_serve(AndroidWorker(cfg)))


async def _serve(worker: AndroidWorker) -> None:
    global _running
    stop_event = asyncio.Event()
    with _lock:
        _running = (asyncio.get_running_loop(), stop_event, worker)
        if _stop_asked:
            stop_event.set()
    try:
        await worker.start()
        await stop_event.wait()
    finally:
        with _lock:
            if _running is not None and _running[1] is stop_event:
                _running = None
        await worker.stop()


def stop() -> None:
    global _stop_asked
    with _lock:
        _stop_asked = True
        if _running is not None:
            loop, stop_event, _ = _running
            loop.call_soon_threadsafe(stop_event.set)


def kick() -> bool:
    """Redial every band now; False when no worker is running."""
    with _lock:
        running = _running
    if running is None or not running[2].started:
        return False
    done = asyncio.run_coroutine_threadsafe(running[2].redial(), running[0])
    done.add_done_callback(lambda f: f.cancelled() or f.exception() is None
                           or log.error("redial failed: %s", f.exception()))
    return True
