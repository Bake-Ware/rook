"""Flat dot-namespaced capability registry, shared by the hub and workers.

Handlers are registered under a dotpath (``shell.exec``, ``hub.info``). A
handler may carry capability metadata (:class:`rook.core.plugin.CapMeta`, set
by ``@capability(..., scope="multi-user")``); when it does, the registry enforces the declared
output contract at dispatch time:

* ``limit``: a default page size. If the handler takes a ``limit`` parameter
  the default is injected when the caller omits it; otherwise core trims list
  results (top-level lists, or the ``items`` list of a dict result) to the
  caller's ``limit`` or the default.
* ``fields``: a default projection. ``"*"`` means projection is supported but
  everything is returned by default; a list names the default keys. The caller
  may pass ``fields=[...]`` / ``fields="a,b"`` / ``fields="*"``. Unless the
  handler takes a ``fields`` parameter itself, core strips it from the args
  and projects dict results, each dict in a list, or - for a dict carrying an
  ``items`` list, which is treated as an envelope - each item.

* ``timeout``: every call is bounded (``CapMeta.timeout``; the host default
  of :func:`rook.core.plugin.default_cap_timeout`, 300 s, when undeclared or
  without metadata). A handler that takes a ``timeout`` argument gets at least
  that value plus a margin. ``timeout=None`` opts a cap out (long-running caps
  that bound themselves). An expired call raises :class:`CapTimeout`; a sync
  handler's thread cannot be killed and finishes in the background, but the
  caller is answered and nothing waits on it.

Handlers without metadata keep their output unchanged.

This module is stdlib-only: it ships inside the worker bundle.
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import logging
from typing import Any, Callable

from .aiocompat import timeout as aio_timeout

log = logging.getLogger("rook.core.registry")

Handler = Callable[..., Any]

_UNSET = object()


class CapTimeout(TimeoutError):
    """A capability call ran past its timeout and was abandoned."""


def _accepts(fn: Handler, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.name == name and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            return True
    return False


def _parse_fields(value: Any) -> list[str] | None:
    """Normalise a caller's ``fields`` argument. ``None`` = all fields."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    if not isinstance(value, (list, tuple)):
        raise TypeError("fields must be a list of names or a comma-separated string")
    names = [str(v) for v in value if str(v).strip()]
    if not names or "*" in names:
        return None
    return names


def _project(result: Any, fields: list[str]) -> Any:
    def one(d: Any) -> Any:
        return {k: d[k] for k in fields if k in d} if isinstance(d, dict) else d
    if isinstance(result, list):
        return [one(r) for r in result]
    if isinstance(result, dict):
        if isinstance(result.get("items"), list):
            return {**result, "items": [one(r) for r in result["items"]]}
        return one(result)
    return result


def _trim(result: Any, limit: int) -> Any:
    if isinstance(result, list):
        return result[:limit]
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        items = result["items"]
        if len(items) > limit:
            return {**result, "items": items[:limit], "truncated": True,
                    "total": result.get("total", len(items))}
    return result


class CapabilityRegistry:
    """Maps dot-namespaced names to handlers and dispatches calls.

    Handlers may be sync or async. Async results are awaited; sync results are
    returned as-is (wrapped in `asyncio.to_thread` so a blocking handler can't
    stall the event loop).
    """

    def __init__(self) -> None:
        self._caps: dict[str, Handler] = {}
        self._meta: dict[str, Any] = {}
        self._timeout_param: dict[str, Any] = {}   # dotpath -> handler's timeout default (cache)

    def register(self, dotpath: str, fn: Handler, replace: bool = False,
                 meta: Any = None) -> None:
        if not dotpath:
            raise ValueError("capability dotpath cannot be empty")
        if dotpath in self._caps and not replace:
            raise ValueError(f"capability already registered: {dotpath}")
        self._caps[dotpath] = fn
        self._timeout_param.pop(dotpath, None)
        meta = meta if meta is not None else getattr(fn, "_rook_cap_meta", None)
        if meta is not None:
            self._meta[dotpath] = meta
        else:
            self._meta.pop(dotpath, None)
        log.debug("registered capability %s", dotpath)

    def unregister(self, dotpath: str) -> bool:
        """Drop a capability. Returns True if it was present. Used for runtime
        plugin disable and custom-cap teardown."""
        self._meta.pop(dotpath, None)
        self._timeout_param.pop(dotpath, None)
        return self._caps.pop(dotpath, None) is not None

    def has(self, dotpath: str) -> bool:
        return dotpath in self._caps

    def list(self) -> list[str]:
        return sorted(self._caps.keys())

    def meta(self, dotpath: str) -> Any:
        """The :class:`CapMeta` declared for a cap, or ``None``."""
        return self._meta.get(dotpath)

    def handler(self, dotpath: str) -> Handler | None:
        return self._caps.get(dotpath)

    def describe(self, prefix: str = "") -> dict:
        """Introspect every handler for the UI: for each cap, its docstring plus
        its parameters (name / required / default / type). Skips self and
        *args/**kwargs. Used by the dashboard to build accurate call forms.
        ``prefix`` limits it to caps whose name starts with it.
        Caps that declare metadata also carry ``risk``/``limit``/``fields``/
        ``tool`` keys (absent for legacy caps, so old consumers see no change)."""
        def _jsonable(v):
            return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
        out: dict[str, Any] = {}
        for name, fn in self._caps.items():
            if not name.startswith(prefix or ""):
                continue
            params = []
            try:
                sig = inspect.signature(fn)
                for p in sig.parameters.values():
                    if p.name == "self" or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                        continue
                    required = p.default is inspect.Parameter.empty
                    ann = p.annotation
                    typ = (None if ann is inspect.Parameter.empty
                           else getattr(ann, "__name__", None) or str(ann).replace("typing.", ""))
                    params.append({"name": p.name, "required": required,
                                   "default": None if required else _jsonable(p.default),
                                   "type": typ})
            except (ValueError, TypeError):
                pass
            doc = (inspect.getdoc(fn) or "").strip()
            entry = {"doc": doc.split("\n\n")[0].replace("\n", " ").strip(),
                     "params": params}
            meta = self._meta.get(name)
            if meta is not None and hasattr(meta, "describe"):
                entry.update(meta.describe())
            out[name] = entry
        return out

    def arg_hint(self, dotpath: str, kwargs: dict, suggest: bool = True) -> str:
        """For a call whose ``kwargs`` do not fit the handler: `` (cap takes:
        a, b?, ...; did you mean 'b' for 'x'?)``. Empty when they fit (the
        TypeError came from inside the cap) or the signature is unknown.
        Callers guess argument names; this turns a guess into one retry.
        ``suggest=False`` leaves out the did-you-mean (Python's own message
        may carry one)."""
        fn = self._caps.get(dotpath)
        if fn is None:
            return ""
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return ""
        params = [p for p in sig.parameters.values()
                  if p.name != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
        names = [p.name for p in params]
        open_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        # call() strips limit/fields for caps that declare them.
        given = {k: v for k, v in (kwargs or {}).items()
                 if k in names or open_kw or k not in ("limit", "fields")}
        try:
            sig.bind(**given)
            return ""
        except TypeError:
            pass
        takes = ", ".join(p.name + ("" if p.default is inspect.Parameter.empty else "?")
                          for p in params if p.kind != p.POSITIONAL_ONLY)
        tips = []
        if suggest and not open_kw:
            for k in given:
                if k not in names:
                    close = difflib.get_close_matches(str(k), names, 1, 0.5)
                    if close:
                        tips.append(f"{close[0]!r} for {k!r}")
        return (f" ({dotpath} takes: {takes or 'no arguments'}"
                + ("; did you mean " + ", ".join(tips) + "?" if tips else "") + ")")

    def _handler_timeout_default(self, dotpath: str, fn: Handler) -> Any:
        """``_UNSET`` if the handler takes no ``timeout`` argument, else its
        default (``None`` when it has none)."""
        if dotpath not in self._timeout_param:
            out: Any = _UNSET
            try:
                p = inspect.signature(fn).parameters.get("timeout")
            except (TypeError, ValueError):
                p = None
            if p is not None and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                out = None if p.default is inspect.Parameter.empty else p.default
            self._timeout_param[dotpath] = out
        return self._timeout_param[dotpath]

    def timeout_for(self, dotpath: str, kwargs: dict | None = None) -> float | None:
        """Seconds core lets this call run (``None`` = unbounded): the cap's
        declared ``timeout`` or the host default, raised to the caller's own
        ``timeout`` argument (or the handler's default for it) plus a margin."""
        from .plugin import DEFAULT_TIMEOUT, TIMEOUT_MARGIN, default_cap_timeout
        fn = self._caps.get(dotpath)
        declared = getattr(self._meta.get(dotpath), "timeout", DEFAULT_TIMEOUT)
        limit = default_cap_timeout() if declared == DEFAULT_TIMEOUT else declared
        if limit is None or fn is None:
            return limit
        default = self._handler_timeout_default(dotpath, fn)
        if default is _UNSET:
            return limit
        asked = (kwargs or {}).get("timeout", default)
        if isinstance(asked, (int, float)) and not isinstance(asked, bool) and asked > 0:
            limit = max(limit, float(asked) + TIMEOUT_MARGIN)
        return limit

    async def call(self, dotpath: str, **kwargs: Any) -> Any:
        fn = self._caps.get(dotpath)
        if fn is None:
            raise KeyError(f"no such capability: {dotpath}")
        meta = self._meta.get(dotpath)
        trim_to: int | None = None
        project: list[str] | None = None
        if meta is not None:
            limit = getattr(meta, "limit", None)
            if limit:
                if _accepts(fn, "limit"):
                    if kwargs.get("limit") is None:
                        kwargs["limit"] = limit
                else:
                    asked = kwargs.pop("limit", None)
                    trim_to = int(asked) if isinstance(asked, int) and asked > 0 else limit
            fields_default = getattr(meta, "fields", None)
            if fields_default is not None and not _accepts(fn, "fields"):
                asked = kwargs.pop("fields", _UNSET)
                if asked is _UNSET:
                    project = (None if fields_default == "*"
                               else _parse_fields(list(fields_default)))
                else:
                    project = _parse_fields(asked)
        limit = self.timeout_for(dotpath, kwargs)
        if inspect.iscoroutinefunction(fn):
            pending = fn(**kwargs)
        else:
            pending = asyncio.to_thread(fn, **kwargs)
        if limit is None:
            result = await pending
        else:
            # asyncio.timeout (not wait_for; aiocompat backports it to 3.10):
            # the handler stays in the caller's task, so context variables
            # behave as before, and its own TimeoutError is told apart from
            # ours by expired().
            try:
                async with aio_timeout(limit) as window:
                    result = await pending
            except TimeoutError:
                if window.expired():
                    log.warning("capability %s timed out after %gs", dotpath, limit)
                    raise CapTimeout(f"{dotpath} did not finish within {limit:g}s") from None
                raise
        if trim_to is not None:
            result = _trim(result, trim_to)
        if project is not None:
            result = _project(result, project)
        return result
