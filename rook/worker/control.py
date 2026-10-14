"""The worker's local control link: ``rook status``, ``rook ask``, ``rook
plugins`` and ``rook logs`` on this machine talk to the running worker here.

Loopback TCP, not a Unix socket, because it must work the same on Windows
(the worker's Python there has no AF_UNIX). The server binds 127.0.0.1 on an
ephemeral port and writes ``<state dir>/control.json`` (mode 0600 on POSIX;
on Windows the profile folder's ACL already keeps other users out) with the
port and a random token. A client proves it can read that file by sending the
token; anyone who can read it can read the band key in ``enrollment.json``
beside it, so the link grants nothing the file system does not already.

Wire: one JSON object per line each way. Every request carries ``token`` and
``op``:

* ``status``: the live state only the process knows (band contact, loaded
  plugins, uptime). The CLI reads the files (enrollment, update result) itself.
* ``call``: ``{cap, args, timeout, local}``. ``local`` runs one of this
  worker's own admin caps (:data:`LOCAL_CAPS`: the plugin switches, the same
  ones the dashboard uses, persisted the same way); otherwise the call goes
  out on the band under this worker's identity (``Worker.request``), so the
  hub applies its band rules (the band risk ceiling) exactly as for any
  worker.
* ``logs``: ``{since, limit}``: recent log lines from an in-memory ring, so
  ``rook logs`` works where the service has no log file (the Windows task
  runs ``pythonw`` with no console).

``ROOK_CONTROL=0`` turns the link off. Stdlib only: it ships in the bundle.
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import os
import secrets
import sys
import time
from typing import Any

from ..core.paths import worker_state_path

log = logging.getLogger("rook.worker.control")

#: Where the port and token live (read by rook.worker.localcli).
CONTROL_FILE = "control.json"
#: This worker's own caps the local link may run. Everything else is a band call.
LOCAL_CAPS = frozenset({"worker.plugin.list", "worker.plugin.enable",
                        "worker.plugin.disable", "worker.settings_report"})
#: Bounds: one request line, concurrent clients, log lines kept.
MAX_LINE = 1 << 20
MAX_CLIENTS = 8
LOG_LINES = 500
#: A band message heard within this many seconds counts as "connected".
CONNECTED_SECS = 90.0
#: Longest band call a client may ask for (home.ask runs a model).
MAX_CALL_TIMEOUT = 600.0


def control_path():
    return worker_state_path(CONTROL_FILE)


def enabled() -> bool:
    return os.environ.get("ROOK_CONTROL", "1").strip().lower() not in ("0", "false", "no", "off")


class LogRing(logging.Handler):
    """The last :data:`LOG_LINES` formatted log records, numbered so a
    follower can ask for what came after the last one it saw."""

    def __init__(self, size: int = LOG_LINES) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: collections.deque = collections.deque(maxlen=size)
        self.seq = 0
        self.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            return
        self.seq += 1
        self.lines.append((self.seq, record.levelname, text))

    def since(self, seq: int = 0, limit: int = 200) -> dict:
        rows = [{"seq": s, "level": lv, "text": t} for s, lv, t in self.lines if s > seq]
        return {"lines": rows[-max(1, min(int(limit), LOG_LINES)):], "last": self.seq}


class ControlServer:
    """Serves the local control link for one :class:`rook.worker.core.Worker`."""

    def __init__(self, worker: Any, ring: LogRing | None = None,
                 started: float | None = None) -> None:
        self.worker = worker
        self.ring = ring
        self.started = started or time.time()
        self.token = secrets.token_urlsafe(32)
        self.port: int | None = None
        self._server: asyncio.base_events.Server | None = None
        self._clients = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0,
                                                  limit=MAX_LINE)
        self.port = self._server.sockets[0].getsockname()[1]
        from ..core.db import atomic_write_text
        atomic_write_text(control_path(), json.dumps({
            "port": self.port, "token": self.token, "pid": os.getpid(),
            "name": getattr(self.worker, "name", ""), "started": self.started}) + "\n", 0o600)
        log.debug("local control link on 127.0.0.1:%s", self.port)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        # Only remove the file if it is still ours (a second worker on the
        # same state dir may have replaced it).
        try:
            data = json.loads(control_path().read_text(encoding="utf-8"))
            if data.get("token") == self.token:
                control_path().unlink()
        except Exception:
            pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._clients >= MAX_CLIENTS:
            writer.close()
            return
        self._clients += 1
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), 10)
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError
            except Exception:
                reply: dict = {"ok": False, "error": "bad request"}
            else:
                if not hmac.compare_digest(str(req.get("token", "")), self.token):
                    reply = {"ok": False, "error": "bad token"}
                else:
                    try:
                        reply = await self.handle(req)
                    except Exception as e:
                        log.exception("control request failed")
                        reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            writer.write((json.dumps(reply, default=str) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            self._clients -= 1
            try:
                writer.close()
            except Exception:
                pass

    async def handle(self, req: dict) -> dict:
        op = req.get("op")
        if op == "status":
            return {"ok": True, "status": self.status()}
        if op == "logs":
            if self.ring is None:
                return {"ok": False, "error": "this worker keeps no log ring"}
            return {"ok": True, **self.ring.since(int(req.get("since") or 0),
                                                   int(req.get("limit") or 200))}
        if op == "call":
            cap = str(req.get("cap") or "")
            args = req.get("args") or {}
            if not cap or not isinstance(args, dict):
                return {"ok": False, "error": "call needs cap and args"}
            try:
                timeout = min(max(float(req.get("timeout") or 15.0), 1.0), MAX_CALL_TIMEOUT)
            except (TypeError, ValueError):
                timeout = 15.0
            if req.get("local"):
                if cap not in LOCAL_CAPS:
                    return {"ok": False, "error": f"{cap} is not available on the local link"}
                result = await self.worker.registry.call(cap, **args)
                return {"ok": True, "result": result}
            target = req.get("target") or None
            try:
                return await self.worker.request(cap, args, target=target, timeout=timeout)
            except asyncio.TimeoutError:
                return {"ok": False, "error": f"no reply from the band within {int(timeout)} s"}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def status(self) -> dict:
        w = self.worker
        now = time.monotonic()
        rx = getattr(w, "band_rx_at", None)
        rtt = getattr(w, "band_rtt_at", None)
        transport = getattr(w, "transport", None)
        hub = getattr(transport, "hub", None)
        peers = []
        try:
            peers = w.band_peers()
        except Exception:
            pass
        hub_seen = any(v[0] for v in getattr(w, "_band_peers", {}).values())
        host = getattr(w, "host", None)
        plugins = []
        for p in getattr(w, "plugins", []):
            mod = getattr(p, "_module", "") or getattr(p, "NAMESPACE", "")
            if mod:
                plugins.append(mod)
        failed = []
        for mod, st in (getattr(host, "status", None) or {}).items():
            if st.get("state") in ("failed",) or st.get("start_error"):
                failed.append({"module": mod, "reason": st.get("reason") or st.get("start_error") or ""})
        from ._build_info import BUILD, VERSION
        return {
            "name": getattr(w, "name", ""), "worker_id": getattr(w, "worker_id", ""),
            "version": VERSION, "build": BUILD, "pid": os.getpid(),
            "python": sys.version.split()[0],
            "uptime_s": int(time.time() - self.started),
            "band": {
                "connected": rx is not None and now - rx < CONNECTED_SECS,
                "last_heard_s": None if rx is None else int(now - rx),
                "last_round_trip_s": None if rtt is None else int(now - rtt),
                "peers_seen": len(peers), "hub_seen": hub_seen,
                "hub": f"{hub[0]}:{hub[1]}" if isinstance(hub, tuple) else None,
                "ws": bool(getattr(transport, "_use_ws", False)),
                "ws_url": getattr(transport, "_ws_url", None)
                if getattr(transport, "_use_ws", False) else None,
            },
            "plugins": sorted(plugins), "plugins_failed": failed,
            "caps": len(w.registry.list()) if hasattr(w, "registry") else 0,
        }


# -- client side (used by rook.worker.localcli) ------------------------------------

class NotRunning(Exception):
    """No worker answers on the local control link."""


def read_control() -> dict | None:
    try:
        data = json.loads(control_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("port") and data.get("token") else None


def request(op: str, wait: float = 10.0, **body: Any) -> dict:
    """One request to the running worker, waiting up to ``wait`` seconds for
    the answer (``body`` may carry the call's own ``timeout``); raises
    :class:`NotRunning` when there is no control file or nothing listens."""
    import socket
    info = read_control()
    if info is None:
        raise NotRunning("the worker is not running (no control file)")
    try:
        sock = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2.0)
    except OSError:
        raise NotRunning("the worker is not running (its control port is closed)") from None
    with sock:
        sock.settimeout(wait)
        sock.sendall((json.dumps({"token": info["token"], "op": op, **body}) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 16 * MAX_LINE:
                raise ValueError("reply too large")
    if not buf:
        raise NotRunning("the worker closed the control link without answering")
    return json.loads(buf)
