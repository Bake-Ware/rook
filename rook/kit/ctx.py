"""The call context every ``run:`` function gets (castling-manifest-2, "Python contract").

``def func(ctx, **args)`` (sync or async) returns JSON-able data,
``ctx.file(...)`` or ``ctx.stream(...)`` (the call answers ``{"stream": id}``
and the code keeps writing through the handle). ``ctx`` carries who is
asking (person, agent, session, band, why) and the plumbing a plugin would
otherwise rebuild: settings with secrets resolved (``where: person`` ones are
the caller's own; ``settings_for(person)`` reads someone else's), ``call``
limited by the manifest's ``uses`` (``call_on`` names the worker), records,
``emit``, a data dir, a per-call tmp dir and a logger.

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
import contextvars
import fnmatch
import functools
import inspect
import logging
import shutil
import sys
import tempfile
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from rook.streams import Producer, StreamError, cap_stream

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
    (hooks, ``ctx.call``); ``owner`` lets records show private rows.
    ``tainted``: it acts on outside content (an event with ``taints``), so
    the hub runs its calls in a tainted session (spec 8.4)."""

    person: str | None = None
    agent: str | None = None
    session: str | None = None
    band: str = ""
    why: str = ""
    owner: bool = False
    plugin: str | None = None
    tainted: bool = False

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


# dispatch(cap, args, caller) or, from call_on, dispatch(cap, args, caller, target=worker);
# may return an awaitable.
Dispatch = Callable[..., Any]


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
                 loop: asyncio.AbstractEventLoop | None = None, cap: str | None = None,
                 people: Any = None, index_streams: Callable[[str, Any], None] | None = None):
        self.plugin = manifest["plugin"]
        self.caller = caller
        # The cap this call runs (full or relative name); None for hooks.
        if cap is not None and not cap.startswith(self.plugin + "."):
            cap = f"{self.plugin}.{cap}"
        self.cap = cap
        caps = manifest.get("caps") or {}
        self._cap_spec = (caps.get(cap) or caps.get(cap[len(self.plugin) + 1:]) or {}) if cap else {}
        self._uses = list(manifest.get("uses") or [])
        self._emits = set(manifest.get("emits") or {})
        for rec in manifest.get("records") or {}:
            self._emits |= {f"{self.plugin}.{rec}.{e}" for e in ("created", "updated", "archived")}
        self._dispatch = dispatch
        # people: the hub's view of its people, ``owners()`` and
        # ``settings(person) -> {plugin: {key: value}}`` (where: person values).
        self._people = people
        self._decls = manifest.get("settings") or {}
        self._values = dict(settings or {})
        self._resolve = resolve_secret
        self.settings = _Settings(self._decls, {**self._values, **self._personal(caller.person)},
                                  resolve_secret, store_setting)
        self._records = records
        self._emit = emit
        self._data_dir = Path(data_dir) if data_dir else None
        self._streams = streams
        self._index_streams = index_streams
        self._loop = loop
        self._tmp: Path | None = None
        self.log = logging.getLogger(f"rook.plugin.{self.plugin}")

    person = property(lambda self: self.caller.person)
    agent = property(lambda self: self.caller.agent)
    session = property(lambda self: self.caller.session)
    band = property(lambda self: self.caller.band)
    why = property(lambda self: self.caller.why)

    # -- people -----------------------------------------------------------------

    def _personal(self, person: str | None) -> dict:
        """``person``'s own values of this plugin's ``where: person`` settings."""
        if not person or self._people is None:
            return {}
        mine = (self._people.settings(person) or {}).get(self.plugin) or {}
        return {k: v for k, v in mine.items() if (self._decls.get(k) or {}).get("where") == "person"}

    def settings_for(self, person: str) -> Mapping:
        """This plugin's settings as ``person`` sees them: their own
        ``where: person`` values over the rest (read only)."""
        if self._people is None:
            raise KitError("no people here: per-person settings live on the hub")
        return _Settings(self._decls, {**self._values, **self._personal(person)}, self._resolve, None)

    def owners(self) -> list[str]:
        """The hub's owners (``person:<name>``)."""
        if self._people is None:
            raise KitError("no people here: the hub knows its owners")
        return list(self._people.owners())

    # -- calls ----------------------------------------------------------------

    def call(self, cap: str, /, **args: Any) -> Any:
        """Call another cap as this plugin, still bounded by the caller's rights
        (the dispatcher intersects them). Refused unless ``uses`` allows it."""
        return self._call(cap, args, None)

    def call_on(self, target: str, cap: str, /, **args: Any) -> Any:
        """``call`` on one worker (name or id), e.g. a device contract such as
        ``notification.post`` on the person's phone."""
        if not isinstance(target, str) or not target:
            raise KitError("call_on needs a worker")
        return self._call(cap, args, target)

    def call_as(self, caller: Caller, cap: str, args: Mapping | None = None, *,
                target: str | None = None) -> Any:
        """Call a cap as ``caller`` rather than this call's caller, on
        ``target`` (a worker; None: wherever the host finds it). For hub
        plugins that act for someone else: a job runs as its creator, its
        persona or the plugin that ships it (spec 8.1, 10.1). Still held to
        this plugin's ``uses``; dispatch checks ``caller``'s own rights."""
        return self._call(cap, dict(args or {}), target, caller)

    def _call(self, cap: str, args: dict, target: str | None, caller: Caller | None = None) -> Any:
        if not allowed(self.plugin, self._uses, cap):
            raise KitError(f"{self.plugin} may not call {cap}: not in its manifest's uses")
        if self._dispatch is None:
            raise KitError("no dispatcher: ctx.call is unavailable here")
        caller = caller or replace(self.caller, plugin=self.plugin)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None:      # no host loop (a plain script, a test)
                res = _dispatch(self._dispatch, cap, dict(args), caller, target)
                if inspect.isawaitable(res):
                    if inspect.iscoroutine(res):
                        res.close()
                    raise KitError("ctx.call from a thread needs the host loop") from None
                return res
            # Sync plugin code in a worker thread: the dispatcher runs on the
            # host loop, where the hub's state lives, and this thread waits.
            return asyncio.run_coroutine_threadsafe(
                _await_call(self._dispatch, cap, dict(args), caller, target), self._loop).result()
        return _await_call(self._dispatch, cap, dict(args), caller, target)

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

    def index_streams(self, streams: Any) -> bool:
        """List a :class:`rook.streams.Streams` this plugin keeps itself (agent
        or voice sessions on the hub) in the hub's stream index, so
        ``stream.list`` finds them. Call it once per process, when the plugin
        makes its Streams; it takes the Streams' ``on_change``. False where
        there is no index (a worker's own streams are listed already)."""
        if self._index_streams is None:
            return False
        self._index_streams(self.plugin, streams)
        return True

    def stream(self, kind: str = "process", *, title: str = "",
               attrs: Mapping | None = None) -> Producer:
        """Open the stream this cap declares (``stream:`` in its manifest
        entry) with this cap as its producer, and return the producer handle:
        ``handle.id`` is the stream id, ``handle.append(type, body)`` writes,
        ``handle.inputs()`` takes input. Returning the handle from the
        function makes the call answer ``{"stream": id}``. Appends belong on
        the host loop: a thread hands them over with
        ``loop.call_soon_threadsafe`` (rook.streams)."""
        if self._streams is None:
            raise KitError("streams are not available here")
        block = self._cap_spec.get("stream")
        if block is None:
            raise KitError(f"{self.cap or self.plugin} declares no stream: in its manifest")
        who = [p for p in (self.caller.person, self.caller.agent) if p]
        # Identities are per hub: the band says whose person:x this is.
        extra = {"band": self.caller.band} if self.caller.band else {}
        try:
            return cap_stream(self._streams, block, producer=self.cap, kind=kind, title=title,
                              participants=who, attrs={**(attrs or {}), **extra})
        except StreamError as e:
            raise KitError(str(e)) from None


def _dispatch(dispatch: Dispatch, cap: str, args: dict, caller: Caller, target: str | None) -> Any:
    if target is None:
        return dispatch(cap, args, caller)
    try:
        inspect.signature(dispatch).bind(cap, args, caller, target=target)
    except TypeError:
        raise KitError("this host can't call a named worker") from None
    return dispatch(cap, args, caller, target=target)


async def _await_call(dispatch: Dispatch, cap: str, args: dict, caller: Caller,
                      target: str | None = None) -> Any:
    res = _dispatch(dispatch, cap, args, caller, target)
    return await res if inspect.isawaitable(res) else res


PLUGIN_THREADS = 8          # sync calls of one plugin running at once
_executors: dict[str, ThreadPoolExecutor] = {}
_executors_lock = threading.Lock()


def plugin_executor(plugin: str) -> ThreadPoolExecutor:
    """The plugin's own bounded thread pool for sync code. A call that hangs
    keeps its thread after it times out; with a pool per plugin, a plugin
    that hangs only queues its own calls, never every other plugin's or the
    loop's default executor (getaddrinfo, to_thread)."""
    with _executors_lock:
        ex = _executors.get(plugin)
        if ex is None:
            ex = _executors[plugin] = ThreadPoolExecutor(PLUGIN_THREADS, thread_name_prefix=f"rook-{plugin}")
        return ex


async def invoke(func: Callable[..., Any], ctx: Ctx, args: Mapping | None = None) -> Any:
    """Run a plugin function: await it if async, else in a thread of the
    plugin's own pool (so it may block and use sync ``ctx.call``). The tmp
    dir goes when it returns."""
    if ctx._loop is None:
        ctx._loop = asyncio.get_running_loop()
    try:
        if inspect.iscoroutinefunction(func):
            res = await func(ctx, **(args or {}))
        else:
            run = functools.partial(contextvars.copy_context().run, func, ctx, **(args or {}))
            res = await asyncio.get_running_loop().run_in_executor(plugin_executor(ctx.plugin), run)
            if inspect.isawaitable(res):
                res = await res
        return res.result() if isinstance(res, Producer) else res
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
