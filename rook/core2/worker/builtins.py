"""The worker's own caps beside core.yaml's: ``worker.*`` (builtin/worker.yaml:
info, log and selfupdate moved into core) and ``stream.*`` (builtin/stream.yaml:
terminals collapse into streams, castling-streams).

- :class:`RanLog` is the worker's own record of what it ran: one JSON line per
  call a verified ticket let through (cap, who, why, ok or error, time taken),
  in ``<data>/ran.jsonl``, rotated to ``ran.jsonl.1`` past ``MAX_BYTES``.
- :func:`handlers` returns the ``{cap: handler}`` map for ``Host.add_builtin``.
  The handlers get the worker and its ``Streams``; a stream is only for its
  participants (the person, else the agent, that opened it).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from rook.kit.ctx import KitError
from rook.streams import StreamError, Streams

MAX_BYTES = 2 * 1024 * 1024         # one ran.jsonl before it rotates (two kept)
CLOSE_GRACE = 3.0                   # seconds a producer gets to end after HUP before the stream is closed
RESTART_DELAY = 0.5                 # the reply goes out first
# Not worth a line each: the hub's own manifest fetches and stream polling
# (the hub journals both).
UNLOGGED = frozenset({"core.manifests", "stream.read"})


class RanLog:
    """Append-only, bounded: ``ran.jsonl`` plus one rotated ``ran.jsonl.1``."""

    def __init__(self, path: Path, max_bytes: int = MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def add(self, entry: dict) -> None:
        line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass        # a full disk must not fail the call that already ran

    def read(self, limit: int = 50, cap: str | None = None, person: str | None = None,
             agent: str | None = None, since: float | None = None, failed: bool = False) -> list[dict]:
        out: list[dict] = []
        with self._lock:
            for path in (self.path.with_name(self.path.name + ".1"), self.path):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line in lines:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if (cap and not str(e.get("cap", "")).startswith(cap)) or \
                            (person and e.get("person") != person) or (agent and e.get("agent") != agent) or \
                            (since and float(e.get("at") or 0) < since) or (failed and e.get("ok")):
                        continue
                    out.append(e)
        return out[-limit:]


def entry(ctx: dict, cap: str, started: float, reply: dict) -> dict:
    """One RanLog line from a verified call's context and its reply."""
    out = {"at": round(started, 3), "cap": cap, "person": ctx.get("person"), "agent": ctx.get("agent"),
           "session": ctx.get("session"), "band": ctx.get("band"), "why": str(ctx.get("why") or "")[:300],
           "call": ctx.get("call"), "ok": bool(reply.get("ok")), "ms": int((time.time() - started) * 1000)}
    if not reply.get("ok"):
        out["error"] = str(reply.get("error") or "")[:300]
    if reply.get("elevated"):
        out["elevated"] = True
    return out


def _mine(meta, caller) -> bool:
    # The person decides when there is one; agents alone only for person-less streams.
    if caller.person:
        return caller.person in meta.participants
    return bool(caller.agent) and caller.agent in meta.participants and not any(
        p.startswith("person:") for p in meta.participants)


def handlers(worker: Any, streams: Streams, ran: RanLog, *, restart: Callable[[], None] | None = None,
             started: float | None = None) -> dict[str, Callable[..., Any]]:
    """``{cap: handler}`` for builtin/worker.yaml and builtin/stream.yaml.
    ``worker`` is the :class:`~rook.core2.worker.runtime.Worker`."""
    up = started or time.time()

    def _get(sid: str, caller):
        try:
            meta = streams.get(sid)
        except (KeyError, StreamError):
            meta = None
        if meta is None or not _mine(meta, caller):
            raise KitError(f"no stream {sid} of yours on this worker")
        return meta

    async def ping(ctx) -> dict:
        return {"pong": True, "at": time.time()}

    async def status(ctx) -> dict:
        from .runtime import _facts, _version
        from .. import CORE_VERSION
        t = worker.transport
        bands = []
        for b in (t.bands if t is not None else ()):
            try:
                bands.append({"band": b[:8], "connected": t.connected(b), "hub_contact": not t.lost(b)})
            except Exception:       # left the band meanwhile
                continue
        host = worker.host
        unavailable = {cap: why for p in host.plugins.values() for cap, why in sorted(p.unavailable.items())}
        return {"name": worker.cfg.name, "worker_id": worker.worker_id, "version": _version(),
                "core": CORE_VERSION, "facts": _facts(), "pid": os.getpid(),
                "started": round(up, 3), "uptime_s": int(time.time() - up), "bands": bands,
                "plugins": sorted(f"{p.name}@{p.version}" for p in host.plugins.values() if not p.builtin),
                "unavailable": unavailable, "calls_in_flight": len(worker.inflight),
                "streams_open": len(streams.list(open_only=True))}

    async def log(ctx, limit=50, cap=None, person=None, agent=None, since=None, failed=False) -> dict:
        return {"entries": await asyncio.to_thread(ran.read, limit, cap, person, agent, since, failed)}

    async def do_restart(ctx) -> dict:
        if restart is None:
            raise KitError("this worker can't restart itself")
        asyncio.get_running_loop().call_later(RESTART_DELAY, restart)
        return {"restarting": True}

    async def read(ctx, stream, after=0, limit=200, wait=0.0, types=None) -> dict:
        _get(stream, ctx.caller)
        try:
            out = await streams.read(stream, after, limit, types, wait)
        except StreamError as e:
            raise KitError(str(e)) from None
        return {**out, "events": [ev.wire() for ev in out["events"]]}

    async def write(ctx, stream, body, type="input") -> dict:
        _get(stream, ctx.caller)
        try:
            ev = streams.append(stream, type, body, by=ctx.caller.actor)
        except StreamError as e:
            raise KitError(str(e)) from None
        return {"seq": ev.seq}

    async def list_(ctx, kind=None, open=False) -> dict:
        return {"streams": [{k: v for k, v in m.wire().items() if k not in ("participants", "host")}
                            for m in streams.list(kind, open_only=open) if _mine(m, ctx.caller)]}

    async def close(ctx, stream) -> dict:
        meta = _get(stream, ctx.caller)
        if not meta.closed and meta.spec.input and streams.attached(stream):
            # Ask the producer to end its process; it closes the stream with the exit code.
            streams.append(stream, "input", {"signal": "HUP"}, by=ctx.caller.actor)
            deadline = time.monotonic() + CLOSE_GRACE
            while not streams.get(stream).closed and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        meta = streams.get(stream)
        if not meta.closed:
            meta = streams.close(stream, by=ctx.caller.actor)
        return {"stream": stream, "closed": meta.closed}

    return {"worker.ping": ping, "worker.status": status, "worker.log": log, "worker.restart": do_restart,
            "stream.read": read, "stream.write": write, "stream.list": list_, "stream.close": close}


def reexec() -> None:
    """Replace this process with a fresh one on the same command line (and
    PID, so a service manager sees no exit)."""
    sys.stdout.flush()
    sys.stderr.flush()
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    os.execv(sys.executable, [sys.executable, *argv[1:]])
