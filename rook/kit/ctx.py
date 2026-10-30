"""The call context every ``run:`` function gets (castling-manifest-2, "Python contract").

``def func(ctx, **args)`` (sync or async) returns JSON-able data,
``ctx.file(...)`` or ``ctx.stream(...)``. ``ctx`` carries who is asking
(person, agent, session, band, why) and the plumbing a plugin would otherwise
rebuild: settings with secrets resolved, ``call`` limited by the manifest's
``uses``, records, ``emit``, a data dir, a per-call tmp dir and a logger.

The host builds one :class:`Ctx` per call and runs the function through
:func:`invoke`, which awaits async functions, runs sync ones in a thread and
removes the tmp dir afterwards. Everything band-facing (the dispatcher, the
vault, the event bus, the record store, streams) is injected, so tests pass
fakes and the kit never imports the hub.

``ctx.call`` follows the function's colour: in an async function
``await ctx.call(...)``; in a sync one (running in a worker thread)
``ctx.call(...)`` returns the result.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
import shutil
import sys
import tempfile
import types
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

__all__ = ["Caller", "Ctx", "FileResult", "KitError", "allowed", "invoke", "load_run"]


class KitError(Exception):
    """A clean failure: the message is what the caller sees. ``retry=True``
    says trying again later may work (device busy, upstream down)."""

    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.message = message
        self.retry = retry


@dataclass(frozen=True)
class Caller:
    """Who a call runs for. ``plugin`` is set when a plugin acts on its own
    (hooks, ``ctx.call``); ``owner`` lets records show private rows."""

    person: str | None = None
    agent: str | None = None
    session: str | None = None
    band: str = ""
    why: str = ""
    owner: bool = False
    plugin: str | None = None

    @property
    def actor(self) -> str:
        """The identity a record names as its creator: the agent if one acts, else the person."""
        return self.agent or self.person or (f"plugin:{self.plugin}" if self.plugin else "")


@dataclass(frozen=True)
class FileResult:
    """``ctx.file(...)``: the host returns this file instead of JSON."""

    path: Path | None = None
    data: bytes | None = None
    name: str | None = None
    mime: str | None = None


def allowed(plugin: str, uses: Iterable[str], cap: str) -> bool:
    """A plugin may call its own caps, and anything matching a ``uses`` glob."""
    if cap.startswith(plugin + "."):
        return True
    return any(fnmatch.fnmatchcase(cap, g) for g in uses)


Dispatch = Callable[[str, dict, Caller], Any]   # may return an awaitable


class _Settings(Mapping):
    """Plugin settings: given values over manifest defaults; ``secret: true``
    values are vault names, resolved on read through the injected resolver."""

    def __init__(self, decls: Mapping, values: Mapping, resolve: Callable[[str], str] | None,
                 store: Callable[[str, Any, bool], None] | None):
        self._decls, self._values = decls, dict(values)
        self._resolve, self._store = resolve, store

    def __getitem__(self, key: str) -> Any:
        decl = self._decls.get(key) or {}
        if key in self._values:
            value = self._values[key]
        elif "default" in decl:
            value = decl["default"]
        elif key in self._decls:
            value = None
        else:
            raise KeyError(key)
        if decl.get("secret") and value:
            if self._resolve is None:
                raise KitError(f"setting {key} is a secret but no vault is available")
            return self._resolve(value)
        return value

    def __iter__(self):
        return iter(dict.fromkeys([*self._decls, *self._values]))

    def __len__(self) -> int:
        return len(set(self._decls) | set(self._values))

    def set(self, key: str, value: Any) -> None:
        """Write a setting (e.g. a pairing key). Secret ones go to the vault: that's the store's job."""
        if key not in self._decls:
            raise KitError(f"setting {key} is not declared")
        if self._store is None:
            raise KitError("settings can't be written here")
        self._store(key, value, bool(self._decls[key].get("secret")))
        if not self._decls[key].get("secret"):
            self._values[key] = value


class Ctx:
    """One call's context. Build with the manifest dict and the injected services."""

    Error = KitError

    def __init__(self, manifest: Mapping, caller: Caller, *, dispatch: Dispatch | None = None,
                 settings: Mapping | None = None, resolve_secret: Callable[[str], str] | None = None,
                 store_setting: Callable[[str, Any, bool], None] | None = None,
                 records: Any = None, emit: Callable[[str, dict, str], Any] | None = None,
                 data_dir: str | Path | None = None, streams: Any = None,
                 loop: asyncio.AbstractEventLoop | None = None):
        self.plugin = manifest["plugin"]
        self.caller = caller
        self._uses = list(manifest.get("uses") or [])
        self._emits = set(manifest.get("emits") or {})
        for rec in manifest.get("records") or {}:
            self._emits |= {f"{self.plugin}.{rec}.{e}" for e in ("created", "updated", "archived")}
        self._dispatch = dispatch
        self.settings = _Settings(manifest.get("settings") or {}, settings or {},
                                  resolve_secret, store_setting)
        self._records = records
        self._emit = emit
        self._data_dir = Path(data_dir) if data_dir else None
        self._streams = streams
        self._loop = loop
        self._tmp: Path | None = None
        self.log = logging.getLogger(f"rook.plugin.{self.plugin}")

    person = property(lambda self: self.caller.person)
    agent = property(lambda self: self.caller.agent)
    session = property(lambda self: self.caller.session)
    band = property(lambda self: self.caller.band)
    why = property(lambda self: self.caller.why)

    # -- calls ----------------------------------------------------------------

    def call(self, cap: str, /, **args: Any) -> Any:
        """Call another cap as this plugin, still bounded by the caller's rights
        (the dispatcher intersects them). Refused unless ``uses`` allows it."""
        if not allowed(self.plugin, self._uses, cap):
            raise KitError(f"{self.plugin} may not call {cap}: not in its manifest's uses")
        if self._dispatch is None:
            raise KitError("no dispatcher: ctx.call is unavailable here")
        caller = replace(self.caller, plugin=self.plugin)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None:      # no host loop (a plain script, a test)
                res = self._dispatch(cap, dict(args), caller)
                if inspect.isawaitable(res):
                    if inspect.iscoroutine(res):
                        res.close()
                    raise KitError("ctx.call from a thread needs the host loop") from None
                return res
            # Sync plugin code in a worker thread: the dispatcher runs on the
            # host loop, where the hub's state lives, and this thread waits.
            return asyncio.run_coroutine_threadsafe(
                _await_call(self._dispatch, cap, dict(args), caller), self._loop).result()
        return _await_call(self._dispatch, cap, dict(args), caller)

    def emit(self, event: str, payload: Mapping | None = None) -> None:
        """Publish an event this plugin declared under ``emits`` (or a record's auto event)."""
        if event not in self._emits:
            raise KitError(f"{self.plugin} doesn't declare event {event} in emits")
        if self._emit is not None:
            self._emit(event, dict(payload or {}), self.plugin)

    # -- storage --------------------------------------------------------------

    @property
    def records(self) -> Any:
        """This plugin's record types, as the caller: ``ctx.records.item.create(...)``."""
        if self._records is None:
            raise KitError("records live on the hub; this plugin has none here")
        return self._records.bind(self.plugin, self.caller)

    @property
    def data(self) -> Path:
        """The plugin's own data dir (``plugin.data``), created on first use."""
        if self._data_dir is None:
            raise KitError("no data dir for this plugin")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir

    @property
    def tmp(self) -> Path:
        """A temp dir for this call only; removed when the call ends."""
        if self._tmp is None:
            self._tmp = Path(tempfile.mkdtemp(prefix=f"rook-{self.plugin}-"))
        return self._tmp

    def cleanup(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    # -- results --------------------------------------------------------------

    def file(self, path: str | Path | None = None, *, data: bytes | None = None,
             name: str | None = None, mime: str | None = None) -> FileResult:
        """Return a file (by path, or bytes) instead of JSON."""
        if (path is None) == (data is None):
            raise KitError("ctx.file takes a path or data, not both")
        p = Path(path) if path is not None else None
        if p is not None and not p.is_file():
            raise KitError(f"no such file: {p}")
        return FileResult(p, data, name or (p.name if p else None), mime)

    def stream(self, **opts: Any) -> Any:
        """Open a stream and return its id (castling-streams).

        TODO(castling:streams): delegates to the injected streams service; the
        interface is the streams agent's (rook/streams/). Until it lands this
        passes ``opts`` through as ``open(plugin=..., caller=..., **opts)``."""
        if self._streams is None:
            raise KitError("streams are not available here")
        return self._streams.open(plugin=self.plugin, caller=self.caller, **opts)


async def _await_call(dispatch: Dispatch, cap: str, args: dict, caller: Caller) -> Any:
    res = dispatch(cap, args, caller)
    return await res if inspect.isawaitable(res) else res


async def invoke(func: Callable[..., Any], ctx: Ctx, args: Mapping | None = None) -> Any:
    """Run a plugin function: await it if async, else in a thread (so it may
    block and use sync ``ctx.call``). The tmp dir goes when it returns."""
    if ctx._loop is None:
        ctx._loop = asyncio.get_running_loop()
    try:
        if inspect.iscoroutinefunction(func):
            return await func(ctx, **(args or {}))
        res = await asyncio.to_thread(func, ctx, **(args or {}))
        return await res if inspect.isawaitable(res) else res
    finally:
        ctx.cleanup()


def load_run(plugin_dir: str | Path, ref: str, modules: dict | None = None) -> Callable[..., Any]:
    """Resolve a ``run:`` target (``scripts/x.py:func``) under the plugin folder.

    ``modules`` is the cache for one load of the plugin: pass the same dict
    for all of its caps and hooks so they share module state (a socket opened
    by ``start``); a fresh dict on reload runs the new code from scratch."""
    path_s, _, name = ref.partition(":")
    if not name:
        raise KitError(f"run target {ref!r} needs file.py:function")
    base = Path(plugin_dir).resolve()
    path = (base / path_s).resolve()
    if base not in path.parents or path.suffix != ".py" or not path.is_file():
        raise KitError(f"run target {ref!r}: no such script in the plugin")
    mod = None if modules is None else modules.get(path)
    if mod is None:
        # Compiled from source, not a .pyc: a cache keyed on mtime can miss a
        # same-second rewrite.
        mod = types.ModuleType(f"rook_plugin_{base.name}_{path.stem}".replace("-", "_"))
        mod.__file__ = str(path)
        sys.modules[mod.__name__] = mod
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)
        if modules is not None:
            modules[path] = mod
    func = getattr(mod, name, None)
    if not callable(func):
        raise KitError(f"run target {ref!r}: no function {name}")
    return func
