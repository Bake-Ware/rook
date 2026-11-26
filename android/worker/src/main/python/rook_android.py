"""The phone's worker: the repo's own Python worker (core/python/rook_worker.py, staged into the APK
at build time), run by the app's foreground service under Chaquopy (brief AP2).

WorkerService.kt calls, on a thread of its own:

    rook_android.run(state_dir, hub_url, name, join_code, told)   # blocks until stop() or a failure
    rook_android.stop()

`told.state(kind, text)` hears where the worker is, in the worker's own words: "joining" (knocking,
waiting for approval, linking, or linking again after the link was lost, with what it said),
"linked" (text: the moniker the hub gave it), "stopped", or "failed" (text: why it stopped). The
worker keeps its keys and identity in `state_dir`, the app's private files, as any worker does in
its state directory; `join_code` is used only while it has no identity. It relinks by itself after a
hub restart or a network change, as every Python worker does (docs/link-design.md 3.2). What it says
also goes to logcat (Chaquopy puts stderr there)."""

import asyncio
import http.client
import logging
import sys
import urllib.error

from nacl.exceptions import BadSignatureError

import rook_worker
from rook_protocol import ProtocolError

_running = {}           # the worker and its loop, while run() runs


class _Told(logging.Handler):
    """The worker's log lines, as states: "Linked to … as <moniker>." is linked, any other is joining."""

    def __init__(self, told):
        super().__init__(logging.INFO)
        self.told = told

    def emit(self, record):
        if record.msg == "Linked to %s as %s.":
            self.told.state("linked", str(record.args[1]))
        else:
            self.told.state("joining", record.getMessage())


def why(e, hub_url):
    """Why the worker stopped, in a few plain words."""
    if isinstance(e, rook_worker.Refused):
        return f"{e.message['message']} {e.message['next']}"
    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return f"cannot reach the hub at {hub_url} ({rook_worker.unreachable(e)})"
    if isinstance(e, (ProtocolError, http.client.HTTPException)):
        return f"{hub_url} did not answer as a Rook hub"
    if isinstance(e, BadSignatureError):
        return f"the approval from {hub_url} does not verify under the hub's own key, so it was not kept"
    return f"{type(e).__name__}: {e}"


def run(state_dir, hub_url, name, join_code, told):
    out = logging.StreamHandler(sys.stderr)
    out.setFormatter(logging.Formatter("%(message)s"))
    handlers = [out, _Told(told)]
    for handler in handlers:
        rook_worker.log.addHandler(handler)
    rook_worker.log.setLevel(logging.INFO)
    rook_worker.log.propagate = False
    logging.getLogger("telesthete").setLevel(logging.ERROR)    # its warnings repeat what the worker says
    loop = asyncio.new_event_loop()
    try:
        worker = rook_worker.Worker(state_dir, hub_url, name=name, token=join_code or None)
        _running.update(worker=worker, loop=loop)
        told.state("joining", f"knocking at {worker.hub_url}" if worker.identity is None
                   else f"linking to {worker.hub_url} as {worker.identity['moniker']}")
        loop.run_until_complete(worker.run())
        told.state("stopped", "")
    except Exception as e:
        told.state("failed", why(e, hub_url))
    finally:
        _running.clear()
        loop.close()
        for handler in handlers:
            rook_worker.log.removeHandler(handler)


def stop():
    """End run(): the worker stops, as Ctrl-C stops `python -m rook_worker`."""
    worker, loop = _running.get("worker"), _running.get("loop")
    if worker is not None:
        loop.call_soon_threadsafe(worker.stop)
