"""Opt-in allocation diagnostics: no object values, credentials, or heap dumps."""
import asyncio
import logging
import signal
import tracemalloc

log = logging.getLogger(__name__)


def install():
    loop = asyncio.get_running_loop()

    def toggle():
        if tracemalloc.is_tracing():
            tracemalloc.stop()
            log.warning('allocation tracing disabled')
        else:
            tracemalloc.start(1)
            log.warning('allocation tracing enabled (new allocations only)')

    def snapshot():
        if not tracemalloc.is_tracing():
            log.warning('allocation tracing is off; SIGUSR2 enables it')
            return
        current, peak = tracemalloc.get_traced_memory()
        log.warning('allocation bytes current=%d peak=%d', current, peak)
        for stat in tracemalloc.take_snapshot().statistics('lineno')[:20]:
            frame = stat.traceback[0]
            log.warning('allocation site=%s:%d bytes=%d count=%d',
                        frame.filename, frame.lineno, stat.size, stat.count)

    loop.add_signal_handler(signal.SIGUSR1, snapshot)
    loop.add_signal_handler(signal.SIGUSR2, toggle)

    def uninstall():
        loop.remove_signal_handler(signal.SIGUSR1)
        loop.remove_signal_handler(signal.SIGUSR2)
    return uninstall
