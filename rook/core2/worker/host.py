"""The worker's plugin host: loaded plugins, their hooks, and the cap call path.

    host = Host(dispatch=hub_call, resolve_secret=vault_get, data_root=state / "plugins")
    host.add_builtin(core_manifest, {"core.manifests": core_manifests})
    await host.load("plugins/deluge/1.2.0")        # hot-load or replace
    result = await host.call("deluge.torrents.list", {}, caller)
    await host.rollback("deluge")                   # back to the kept version
    announce = {**base, **await host.announce(ceiling)}

A request from a band goes through :meth:`Host.handle`: the worker gate
(security.py) checks its ticket (spec 6.3) and runs it, elevating when the
cap asks; the host checks args against the manifest, merges ``bind``, takes
the cap's ``exclusive`` lock, runs the action or ``run:`` function under the
cap's timeout, and turns every failure into an error reply: a plugin never
takes the worker down. The gate's cap table follows every load and unload.

Replacing a plugin stops the old version's hooks, binds the new one from its
folder and starts its hooks; if that fails the old one comes back. The
previous version is kept (by folder and hash) so :meth:`Host.rollback` can
return to it without a restart.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import re
import tempfile
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

from rook.core2 import rights, wire
from rook.kit.actions import ActionError, Secret, run_action
from rook.kit.actions.parse import file_result
from rook.kit.ctx import Caller, Ctx, FileResult, KitError, invoke
from rook.kit.hooks import Scheduler

from .loader import LoadError, Plugin, load_builtin, load_folder, local_os, merge_settings
from .security import Call, NeedsElevation, WorkerGate, reply

__all__ = ["CallError", "Host", "LoadError"]

log = logging.getLogger("rook.worker.host")

_SETTING_REF = re.compile(r"\{\{\s*settings\.([A-Za-z0-9_-]+)")
_TMP_REF = re.compile(r"\{\{\s*tmp\s*\}\}")


class CallError(Exception):
    """A failed call; ``message`` is the reply's ``error`` (spec 6.4)."""

    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.message = message
        self.retry = retry


class Host:
    """One per worker process. Everything band-facing is injected:

    - ``dispatch(cap, args, caller)``: ``ctx.call`` and ``call:`` hooks (the hub's ``core.call``).
    - ``resolve_secret(name) -> str | None``: the vault, asked per call. Sync:
      ``run:`` code reads ``ctx.settings`` from its worker thread.
    - ``store_setting(plugin, key, value, secret)``: ``ctx.settings.set``.
    - ``emit(event, payload, plugin)``: events a plugin publishes.
    """

    def __init__(self, *, os_name: str | None = None, hardware=(),
                 dispatch: Callable | None = None, resolve_secret: Callable[[str], Any] | None = None,
                 store_setting: Callable | None = None, emit: Callable | None = None,
                 data_root: str | Path | None = None, streams: Any = None,
                 contracts: dict | None = None, may_receive: Callable | None = None,
                 hook_timeout: float = 30.0, gate: WorkerGate | None = None):
        self.os_name = os_name or local_os()
        self.hardware = set(hardware)
        self.dispatch = dispatch
        self.resolve_secret = resolve_secret
        self.store_setting = store_setting
        self.emit = emit
        self.data_root = Path(data_root) if data_root else None
        self.streams = streams
        self.contracts = contracts
        self.scheduler = Scheduler(dispatch=dispatch, timeout=hook_timeout, may_receive=may_receive)
        self.plugins: dict[str, Plugin] = {}
        self.previous: dict[str, Plugin] = {}    # one kept version per plugin, for rollback
        self._local: dict[str, dict] = {}         # worker-local setting values per plugin
        self._hub: dict[str, dict] = {}           # hub-provided setting values per plugin
        self._locks: dict[str, asyncio.Lock] = {}  # exclusive: lock name -> lock, per worker
        self._admin = asyncio.Lock()               # one load/unload at a time
        self.gate = gate
        self._sync()

    # -- loading ----------------------------------------------------------------

    def add_builtin(self, data: dict, handlers: Mapping[str, Callable[..., Any]]) -> Plugin:
        """Register a ``builtin: true`` manifest (core) and its handlers."""
        # Core caps that need the verified call are answered by handle(); a
        # stub handler marks them served.
        own = {cap: _handled_by_host for cap in _HOST_CAPS}
        p = load_builtin(data, {**own, **handlers}, os_name=self.os_name, hardware=self.hardware)
        self.plugins[p.name] = p
        self._sync()
        return p

    async def load(self, folder: str | Path) -> Plugin:
        """Hot-load the plugin in ``folder``, replacing any loaded version,
        which is kept for :meth:`rollback`. Raises LoadError and leaves the
        loaded version running when the new one won't load or start."""
        async with self._admin:
            return await self._load(Path(folder), keep=True)

    async def reload(self, name: str) -> Plugin:
        """Rebind a loaded plugin from its own folder (new settings, new code
        in place). The kept previous version doesn't change."""
        async with self._admin:
            return await self._load(self._loaded(name).folder, keep=False)

    async def rollback(self, name: str) -> Plugin:
        """Go back to the kept previous version (which becomes the kept one in
        turn). Refused when there is none or its folder changed since."""
        async with self._admin:
            prev = self.previous.get(name)
            if prev is None:
                raise LoadError(f"{name}: no previous version to roll back to")
            return await self._load(prev.folder, keep=True, expect=prev.hash)

    async def unload(self, name: str) -> None:
        """Stop the plugin's hooks and stop serving its caps. It becomes the
        kept version, so rollback brings it back."""
        async with self._admin:
            p = self._loaded(name)
            await self.scheduler.stop(name)
            del self.plugins[name]
            self._keep(p)
            self._sync()

    async def set_settings(self, name: str, *, local: Mapping | None = None,
                           hub: Mapping | None = None) -> None:
        """New setting values for a plugin (either source, or both). Changes
        to ``apply: live`` settings (the default) reach the next call as they
        are; any other ``apply``, or a setting some ``requires.settings``
        names, rebinds the plugin so availability and hooks see it."""
        if local is not None:
            self._local[name] = dict(local)
        if hub is not None:
            self._hub[name] = dict(hub)
        p = self.plugins.get(name)
        if p is None or p.builtin:
            return
        decls = p.manifest.get("settings") or {}
        new = merge_settings(decls, self._local.get(name, {}), self._hub.get(name, {}))
        changed = {k for k in decls if new.get(k) != p.settings.get(k)}
        if not changed:
            return
        if changed & _required_settings(p.manifest) or \
                any(decls[k].get("apply", "live") != "live" for k in changed):
            await self.reload(name)
        else:
            p.settings.clear()
            p.settings.update(new)

    async def close(self) -> None:
        async with self._admin:
            await self.scheduler.close()
            for p in self.plugins.values():
                p.forget_modules()
            self.plugins.clear()
            self._sync()

    def _loaded(self, name: str) -> Plugin:
        p = self.plugins.get(name)
        if p is None or p.builtin:
            raise LoadError(f"{name} is not a loaded plugin")
        return p

    async def _load(self, folder: Path, *, keep: bool, expect: str | None = None) -> Plugin:
        new = await asyncio.to_thread(self._bind, folder)
        if expect is not None and new.hash != expect:
            new.forget_modules()
            raise LoadError(f"{new.name}: kept version at {folder} changed since it was loaded")
        old = self.plugins.get(new.name)
        if old is not None and old.builtin:
            new.forget_modules()
            raise LoadError(f"{new.name} is a builtin plugin")
        if old is not None:
            await self.scheduler.stop(old.name)
        try:
            await self._start(new)
        except BaseException as e:
            new.forget_modules()
            if old is not None:
                try:
                    await self._start(old)
                except Exception as e2:   # the old one is still served; its hooks are off
                    log.warning("%s: restarting hooks after a failed load: %s", old.name, e2)
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            raise LoadError(f"{new.name}: start hook failed: {_message(e)}") from e
        self.plugins[new.name] = new
        self._sync()
        if old is not None:
            if keep:
                self._keep(old)
            else:
                old.forget_modules()
        log.info("loaded %s@%s (%d caps, %d unavailable)", new.name, new.version,
                 len({c.name for c in new.caps.values()}), len(new.unavailable))
        return new

    def _bind(self, folder: Path) -> Plugin:
        return load_folder(folder, os_name=self.os_name, hardware=self.hardware,
                           values=lambda n: (self._local.get(n, {}), self._hub.get(n, {})),
                           plugin_ctx=lambda p: self._ctx(p, Caller(plugin=p.name, why=f"{p.name} plugin")),
                           contracts=self.contracts)

    def _keep(self, p: Plugin) -> None:
        old = self.previous.get(p.name)
        if old is not None and old is not p:
            old.forget_modules()
        p.forget_modules()   # rollback rebinds from the folder: fresh modules, fresh state
        self.previous[p.name] = p

    async def _start(self, p: Plugin) -> None:
        if p.reason is None and p.manifest.get("runs_on", "worker") == "worker" \
                and p.manifest.get("hooks"):
            await self.scheduler.start(p.manifest, make_ctx=partial(self._ctx, p),
                                       resolve=p.resolve, band="")

    # -- calls ------------------------------------------------------------------

    def caps(self) -> dict[str, dict]:
        """``{cap: {"risk", "effects", "elevation"}}`` for every cap served
        now (aliases included): what the worker gate checks (spec 6.3, 7)."""
        return {name: {"risk": c.spec["risk"], "effects": list(c.spec.get("effects", ())),
                       "elevation": c.spec.get("elevation", "never")}
                for p in self.plugins.values() for name, c in p.caps.items()}

    def _sync(self) -> None:
        if self.gate is not None:
            self.gate.caps.clear()
            self.gate.caps.update(self.caps())

    async def handle(self, request: Any, band: str, now: int | None = None) -> dict | None:
        """One request that arrived on ``band``: the reply to send, or None
        when it isn't for this worker. Needs a ``gate``."""
        got = self.gate.check(request, band, now)
        if not isinstance(got, Call):
            # The gate only knows caps it can serve. One that a verified
            # ticket names but can't run now says why (the announce did too).
            cap = request.get("cap")
            if got is not None and got.get("error") == f"unknown capability: {cap}":
                why = next((p.unavailable[cap] for p in self.plugins.values() if cap in p.unavailable), None)
                if why is not None:
                    got["error"] = f"unavailable: {cap}: {why}"
            return got
        call, cap = got, got.ticket["cap"]
        args = request.get("args") or {}
        info = call.context()
        caller = Caller(person=info["person"], agent=info["agent"], session=info["session"],
                        band=band, why=info["why"])

        async def action(lease):
            if cap == "core.manifests":
                return self.manifests(args.get("plugins"), call.member.ceiling)
            if cap == "core.lease.drop":
                return self.gate.drop_leases(call.ticket["hub"], band, args)
            return await self.call(cap, args, caller, lease=lease)

        try:
            return await self.gate.execute(call, action, now)
        except CallError as e:
            out = reply(request["id"], self.gate.worker_id, error=e.message)
            if e.retry:
                out["retry"] = True
            return out
        except (NeedsElevation, PermissionError) as e:   # a cap that may not elevate hit access denied
            return reply(request["id"], self.gate.worker_id,
                         error=f"{cap} failed: access denied{': ' + str(e) if str(e) else ''}")

    async def call(self, cap: str, args: Mapping | None, caller: Caller, lease: dict | None = None) -> Any:
        """Run one cap for ``caller`` (from a verified ticket), elevated when
        ``lease`` is set. Returns the JSON-able result or raises CallError;
        access denied (``NeedsElevation``, ``PermissionError``) passes through
        so the gate can elevate and run it again."""
        p, c = self._find(cap)
        args = dict(args or {})
        errors = sorted(c.validator.iter_errors(args), key=lambda e: list(e.absolute_path))
        if errors:
            e = errors[0]
            where = "/".join(map(str, e.absolute_path))
            raise CallError(f"bad args: {where + ': ' if where else ''}{e.message}")
        for key, spec in c.spec.get("args", {}).items():
            if key not in args and "default" in spec:
                args[key] = copy.deepcopy(spec["default"])
        args.update(copy.deepcopy(c.bind))
        loop = asyncio.get_running_loop()
        deadline = None if c.timeout is None else loop.time() + c.timeout
        lock = None
        if c.spec.get("exclusive"):
            lock = self._locks.setdefault(c.spec["exclusive"], asyncio.Lock())
            try:
                async with asyncio.timeout_at(deadline):   # not wait_for: 3.11 can leak the lock
                    await lock.acquire()
            except TimeoutError:
                raise CallError(f"busy: {c.spec['exclusive']} is in use", retry=True) from None
        shown: list[str] = []   # secret values this call's code was handed
        work = asyncio.ensure_future(self._exec(p, c, args, caller, shown, lease))
        work.add_done_callback(_retrieve)
        if lock is not None:
            # Held until the work really ends: a timed-out sync function keeps
            # its thread, and the device it holds stays locked until it returns.
            work.add_done_callback(lambda _: lock.release())
        # Async work (actions included) is cancelled on timeout; a sync
        # function's thread can't be stopped, so its task is left to finish.
        stoppable = c.func is None or inspect.iscoroutinefunction(c.func)
        try:
            done, _ = await asyncio.wait(
                {work}, timeout=None if deadline is None else max(0.0, deadline - loop.time()))
        except asyncio.CancelledError:
            if stoppable:
                work.cancel()
            raise
        if not done:
            if stoppable:
                work.cancel()
            raise CallError(f"{cap} timed out after {c.timeout:g}s", retry=True)
        try:
            return _jsonable(work.result())
        except (CallError, NeedsElevation, PermissionError):
            raise
        except (KitError, ActionError) as e:
            msg = _redact(getattr(e, "message", None) or str(e), shown)
            raise CallError(msg, retry=e.retry) from None
        except asyncio.CancelledError:   # the plugin cancelled itself
            raise CallError(f"{cap} was cancelled") from None
        except BaseException as e:      # anything plugin code raises
            if isinstance(e, KeyboardInterrupt):
                raise
            msg = _redact(f"{cap} failed: {type(e).__name__}: {e}", shown)
            log.warning("%s", msg)
            log.debug("%s traceback", cap, exc_info=e)
            raise CallError(msg) from None

    def _find(self, cap: str):
        for p in self.plugins.values():
            c = p.caps.get(cap)
            if c is not None:
                return p, c
        for p in self.plugins.values():
            if cap in p.unavailable:
                raise CallError(f"unavailable: {cap}: {p.unavailable[cap]}")
        raise CallError(f"unknown capability: {cap}")

    async def _exec(self, p: Plugin, c, args: dict, caller: Caller, shown: list[str],
                    lease: dict | None = None) -> Any:
        if c.action is None:
            ctx = self._ctx(p, caller, shown, cap=c.name)
            # TODO(castling:wiring): the call context (6.6: hub, call id, risk,
            # lease) belongs on Caller; until then the lease rides on ctx so
            # plugin code can pass it to security.launch.
            ctx.lease = lease
            return await invoke(c.func, ctx, args)
        text = json.dumps(c.action)
        settings = self._action_settings(p, set(_SETTING_REF.findall(text)))
        tmp = tempfile.TemporaryDirectory(prefix=f"rook-{p.name}-") if _TMP_REF.search(text) else None
        try:
            # cap=: arg join/map and result coercion (manifest 1.1).
            return await run_action(
                c.action, args, settings=settings, worker={"os": self.os_name},
                secrets=self.resolve_secret, tmp=tmp.name if tmp else None,
                plugin_dir=str(p.folder) if p.folder else None,
                plugin_data=str(self._data_dir(p)) if self.data_root else None,
                timeout=c.timeout, cap=c.spec)
        finally:
            if tmp is not None:
                tmp.cleanup()

    def _action_settings(self, p: Plugin, used: set[str]) -> dict:
        """Setting values for an action; the secret ones it names are fetched
        from the vault now, per call, and marked so errors never show them."""
        out = {}
        for key, decl in (p.manifest.get("settings") or {}).items():
            value = p.settings.get(key, decl.get("default"))
            if value is None:
                continue
            if decl.get("secret"):
                if key not in used:
                    continue
                if self.resolve_secret is None:
                    raise CallError(f"setting {key} is a secret but no vault is available")
                value = self.resolve_secret(value)
                if value is None:
                    raise CallError(f"setting {key}: its secret is not in the vault")
                value = Secret(value)
            out[key] = value
        return out

    def _ctx(self, p: Plugin, caller: Caller, shown: list[str] | None = None,
             cap: str | None = None) -> Ctx:
        store = partial(self.store_setting, p.name) if self.store_setting else None
        resolve = self.resolve_secret
        if resolve is not None and shown is not None:
            def resolve(name, _get=resolve):
                value = _get(name)
                if value:
                    shown.append(str(value))
                return value
        return Ctx(p.manifest, caller, dispatch=self.dispatch, settings=p.settings,
                   resolve_secret=resolve, store_setting=store, emit=self.emit,
                   data_dir=self._data_dir(p) if self.data_root else None,
                   streams=self.streams, cap=cap)

    def _data_dir(self, p: Plugin) -> Path:
        return self.data_root / p.name

    # -- events -----------------------------------------------------------------

    def publish(self, event: str, payload: Mapping | None = None) -> None:
        """An event from the band, for plugins' ``on:`` hooks."""
        self.scheduler.publish(event, payload)

    # -- announce ---------------------------------------------------------------

    def _visible(self, ceiling: Mapping | None) -> list[Plugin]:
        """Filtered view (spec 9.4): plugins with a cap the band's ceiling grants name."""
        out = []
        for p in self.plugins.values():
            if ceiling is None or any(
                    cap in wire.ALWAYS or rights.matches(g.partition("@")[0], cap)
                    for cap in p.manifest["caps"] for g in ceiling.get("grants") or ()):
                out.append(p)
        return sorted(out, key=lambda p: p.name)

    def manifests(self, plugins=None, ceiling: Mapping | None = None) -> dict:
        """``core.manifests`` (spec 13): the parsed manifests of the band's view."""
        return {"manifests": [p.raw for p in self._visible(ceiling)
                              if plugins is None or p.name in plugins]}

    async def status(self, ceiling: Mapping | None = None) -> dict:
        """``{plugin: status}`` from ``status`` hooks; plugins without one are left out."""
        visible = self._visible(ceiling)
        got = await asyncio.gather(*(self.scheduler.status(p.name) for p in visible))
        return {p.name: s for p, s in zip(visible, got, strict=True) if s is not None}

    async def announce(self, ceiling: Mapping | None = None) -> dict:
        """The plugin part of the worker announce (spec 5.3): ``manifests``,
        ``manifests_hash``, ``unavailable`` and ``status`` for one band's view.
        Core adds the rest (worker_id, name, core, facts, role, ceiling...)."""
        visible = self._visible(ceiling)
        entries = []
        for p in visible:
            e = {"plugin": p.name, "version": p.version, "hash": p.hash}
            if p.builtin:
                e["builtin"] = True
            entries.append(e)
        unavailable = {cap: why for p in visible for cap, why in sorted(p.unavailable.items())}
        out = {"manifests": entries,
               "manifests_hash": hashlib.sha256(wire.canonical(entries)).hexdigest(),
               "unavailable": unavailable}
        status = await self.status(ceiling)
        if status:
            out["status"] = status
        return out


def _retrieve(task: asyncio.Future) -> None:
    """Mark a finished call's exception as seen (a timed-out call ends later)."""
    if not task.cancelled():
        task.exception()


_HOST_CAPS = ("core.manifests", "core.lease.drop")


def _handled_by_host(ctx, **args):
    raise KitError("served through Host.handle")


def _required_settings(manifest: Mapping) -> set:
    reqs = [manifest.get("requires") or {}, *((c.get("requires") or {}) for c in manifest["caps"].values())]
    return {s for r in reqs for s in r.get("settings") or ()}


def _redact(text: str, secrets: list[str]) -> str:
    for value in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def _jsonable(res: Any) -> Any:
    if isinstance(res, FileResult):
        data = res.data if res.data is not None else res.path.read_bytes()
        return file_result(data, res.name, res.mime)
    return res


def _message(e: BaseException) -> str:
    return getattr(e, "message", None) or str(e) or type(e).__name__
