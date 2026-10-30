"""Plugin hooks: start/stop/status, ``every`` and ``on``, run by one scheduler in core.

From a manifest's ``hooks:`` block (castling-manifest-2, "Hooks")::

    hooks:
      start: scripts/tv.py:start
      stop: scripts/tv.py:stop
      status: scripts/tv.py:status
      every: [ { interval: 5m, call: deluge.stats.custom } ]
      on: [ { event: worker.online, run: scripts/stats.py:warm } ]

Each target is a ``run:`` function (``def f(ctx, **args)``) or a ``call:`` to
a cap (with optional ``args:``). ``start``/``stop``/``status`` may be given as
a bare run target. Hooks run as the plugin, without a person: a ``call:`` is
held to the plugin's ``uses`` like ``ctx.call``. ``on`` run functions get
``event=`` and ``payload=``; ``on`` calls get only their declared ``args``.

The scheduler never lets a hook take down another: errors are logged and the
next tick runs. :meth:`Scheduler.stop` cancels a plugin's timers and in-flight
runs, unsubscribes its events and then runs its ``stop`` hook, so unloading a
plugin leaves nothing behind. Core's own periodic work (record pruning) uses
:meth:`Scheduler.every` under an owner name and stops the same way.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from ._types import parse_duration
from .ctx import Caller, Ctx, KitError, allowed, invoke, load_run

__all__ = ["Scheduler"]

log = logging.getLogger("rook.kit.hooks")


@dataclass
class _Target:
    label: str
    func: Callable[..., Any] | None = None
    cap: str | None = None
    args: dict = field(default_factory=dict)


@dataclass
class _Plugin:
    name: str
    manifest: Mapping | None = None
    make_ctx: Callable[[Caller], Ctx] | None = None
    band: str = ""
    stop: _Target | None = None
    status: _Target | None = None
    on: list[tuple[str, _Target]] = field(default_factory=list)
    tasks: set[asyncio.Task] = field(default_factory=set)


class Scheduler:
    """One per process. ``dispatch(cap, args, caller)`` runs ``call:`` targets;
    ``may_receive(plugin, event)`` filters which events reach ``on`` hooks."""

    def __init__(self, dispatch: Callable[[str, dict, Caller], Any] | None = None,
                 timeout: float = 30.0, may_receive: Callable[[str, str], bool] | None = None):
        self.dispatch = dispatch
        self.timeout = timeout
        # Event grants are hub state: core passes its check here so a plugin's
        # on: hooks only see events it was granted (default: all).
        self.may_receive = may_receive
        self._plugins: dict[str, _Plugin] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def plugins(self) -> list[str]:
        return list(self._plugins)

    # -- lifecycle ------------------------------------------------------------

    async def start(self, manifest: Mapping, *, plugin_dir: str | Path | None = None,
                    make_ctx: Callable[[Caller], Ctx] | None = None,
                    resolve: Callable[[str], Callable[..., Any]] | None = None,
                    band: str = "") -> None:
        """Load a plugin's hooks: run ``start``, then arm ``every`` and ``on``.
        ``resolve(ref)`` finds run targets (default: ``load_run`` in plugin_dir, one
        module cache per start; a host passes its own to share it with caps).
        A failing start hook leaves nothing armed and raises."""
        name = manifest["plugin"]
        if name in self._plugins:
            raise KitError(f"{name} hooks are already running; stop them first")
        self._loop = asyncio.get_running_loop()
        hooks = manifest.get("hooks") or {}
        if resolve is None:
            if plugin_dir is None and _has_run(hooks):
                raise KitError(f"{name}: run hooks need the plugin folder")
            modules: dict = {}
            resolve = lambda ref: load_run(plugin_dir, ref, modules)  # noqa: E731
        make_ctx = make_ctx or (lambda caller: Ctx(manifest, caller, dispatch=self.dispatch,
                                                   loop=self._loop))
        p = _Plugin(name, manifest, make_ctx, band)
        p.stop = _target(hooks.get("stop"), "stop", resolve)
        p.status = _target(hooks.get("status"), "status", resolve)
        every = [(parse_duration(h["interval"]), _target(h, f"every {h['interval']}", resolve))
                 for h in hooks.get("every") or []]
        if any(sec <= 0 for sec, _ in every):
            raise KitError(f"{name}: every interval must be positive")
        p.on = [(h["event"], _target(h, f"on {h['event']}", resolve)) for h in hooks.get("on") or []]
        start = _target(hooks.get("start"), "start", resolve)
        if start is not None:
            await asyncio.wait_for(self._run(p, start, {}), self.timeout)
        self._plugins[name] = p
        for sec, target in every:
            self._spawn(p, self._tick(p, sec, target))

    async def stop(self, name: str) -> None:
        """Cancel timers and in-flight runs, drop event subscriptions, run ``stop``."""
        p = self._plugins.pop(name, None)
        if p is None:
            return
        for t in list(p.tasks):
            t.cancel()
        await asyncio.gather(*p.tasks, return_exceptions=True)
        if p.stop is not None:
            try:
                await asyncio.wait_for(self._run(p, p.stop, {}), self.timeout)
            except Exception as e:   # unloading goes on regardless
                log.warning("%s stop hook failed: %s", name, e)

    async def close(self) -> None:
        for name in list(self._plugins):
            await self.stop(name)

    async def status(self, name: str) -> Any:
        """The plugin's ``status`` hook result (merged into the heartbeat), or
        ``{"error": ...}``; None when it has no status hook."""
        p = self._plugins.get(name)
        if p is None or p.status is None:
            return None
        try:
            return await asyncio.wait_for(self._run(p, p.status, {}), self.timeout)
        except Exception as e:
            return {"error": _message(e)}

    def every(self, owner: str, interval: str | float, fn: Callable[[], Any]) -> None:
        """Core's own periodic work, e.g. ``every("records", "1d", store.prune)``.
        ``fn`` may be sync (run in a thread) or async. Stopped by ``stop(owner)``."""
        self._loop = asyncio.get_running_loop()
        p = self._plugins.setdefault(owner, _Plugin(owner))
        self._spawn(p, self._tick(p, parse_duration(interval), _Target(f"every {interval}", func=fn)))

    # -- events ---------------------------------------------------------------

    def publish(self, event: str, payload: Mapping | None = None, source: str | None = None) -> None:
        """Run every ``on`` hook matching ``event`` (exact or glob). Safe to call
        from any thread; returns at once. Signature fits ``Ctx(emit=...)`` and
        ``RecordStore(emit=...)``."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._fan_out, event, dict(payload or {}))

    def _fan_out(self, event: str, payload: dict) -> None:
        for p in list(self._plugins.values()):
            if p.on and self.may_receive is not None and not self.may_receive(p.name, event):
                continue
            for pattern, target in p.on:
                if fnmatch.fnmatchcase(event, pattern):
                    args = {"event": event, "payload": payload} if target.func else {}
                    self._spawn(p, self._safe(p, target, args))

    # -- running --------------------------------------------------------------

    def _spawn(self, p: _Plugin, coro: Awaitable) -> None:
        task = asyncio.ensure_future(coro)
        p.tasks.add(task)
        task.add_done_callback(p.tasks.discard)

    async def _tick(self, p: _Plugin, seconds: float, target: _Target) -> None:
        while True:
            await asyncio.sleep(seconds)
            await self._safe(p, target, {})

    async def _safe(self, p: _Plugin, target: _Target, args: dict) -> None:
        try:
            await self._run(p, target, args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s hook %s failed: %s", p.name, target.label, _message(e))

    async def _run(self, p: _Plugin, target: _Target, args: dict) -> Any:
        caller = Caller(plugin=p.name, band=p.band, why=f"{p.name} hook: {target.label}")
        if target.cap is not None:
            if not allowed(p.name, (p.manifest or {}).get("uses") or [], target.cap):
                raise KitError(f"{p.name} may not call {target.cap}: not in its manifest's uses")
            if self.dispatch is None:
                raise KitError("no dispatcher for call hooks")
            res = self.dispatch(target.cap, {**target.args, **args}, caller)
            return await res if inspect.isawaitable(res) else res
        if p.make_ctx is None:   # core work
            res = target.func() if inspect.iscoroutinefunction(target.func) else \
                await asyncio.to_thread(target.func)
            return await res if inspect.isawaitable(res) else res
        return await invoke(target.func, p.make_ctx(caller), args)


def _has_run(hooks: Mapping) -> bool:
    singles = [hooks.get(k) for k in ("start", "stop", "status")]
    lists = [*(hooks.get("every") or []), *(hooks.get("on") or [])]
    return any(isinstance(h, str) or (isinstance(h, Mapping) and "run" in h) for h in singles + lists)


def _target(spec: Any, label: str, resolve: Callable[[str], Callable[..., Any]]) -> _Target | None:
    if spec is None:
        return None
    if isinstance(spec, str):
        return _Target(label, func=resolve(spec))
    if "call" in spec:
        return _Target(label, cap=spec["call"], args=dict(spec.get("args") or {}))
    if "run" in spec:
        return _Target(label, func=resolve(spec["run"]), args={})
    raise KitError(f"hook {label} needs run: or call:")


def _message(e: BaseException) -> str:
    return getattr(e, "message", None) or str(e) or type(e).__name__
