"""MCP tools generated from hub caps.

Caps are the single interface; a cap declared ``@capability(..., tool=True)``
on a hub-placed plugin also gets a dedicated MCP tool named
``rook_<cap with dots as underscores>`` (``hub.info`` -> ``rook_hub_info``).
The tool's parameters mirror the handler's signature (plus ``limit`` /
``fields`` when core enforces them); its description is the cap's
``description`` or the first paragraph of its docstring. Keep it short: every
connect pays for ``tools/list``. ``guidance.apply`` then advertises it like any
tool (``descriptions.for_tool``, ``envelope.slim_tool``).

A generated tool is a thin alias: it calls the bridge's own ``rook_call`` on
worker ``rook``, so attribution, the journal, secret substitution, guidance
tips and the reply envelope are exactly those of ``rook_call``. Existing tool
names always win; a cap whose tool name is taken is skipped with a warning.

A hub plugin may instead hand over ready-made tools with a ``mcp_tools(invoke)``
method (:func:`register_plugin_tools`). That is for tools whose shape predates
caps and must not change, such as the action-style ``rook_knowledge`` and
``rook_task``: each tool routes to the plugin's caps through ``invoke(cap,
args)``, which runs the cap in process with the caller's identity and returns
its result (or raises), and formats its own reply.
"""

from __future__ import annotations

import inspect
import logging
import typing
from typing import Any, Awaitable, Callable

log = logging.getLogger("rook.hub.mcp_tools")

CallFn = Callable[[str, dict], Awaitable[str]]


def tool_name(cap: str) -> str:
    return "rook_" + cap.replace(".", "_").replace("-", "_")


def _description(cap: str, fn: Callable[..., Any], meta: Any) -> str:
    desc = getattr(meta, "description", None) or (inspect.getdoc(fn) or "").split("\n\n")[0]
    return " ".join(desc.split()) or f"Hub cap {cap} on worker rook."


def _signature(fn: Callable[..., Any], meta: Any) -> inspect.Signature:
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    params: list[inspect.Parameter] = []
    names = set()
    for p in inspect.signature(fn).parameters.values():
        if p.name == "self" or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) or p.name.startswith("_"):
            continue
        ann = hints.get(p.name, Any)
        params.append(inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY,
                                        default=p.default, annotation=ann))
        names.add(p.name)
    if getattr(meta, "limit", None) and "limit" not in names:
        params.append(inspect.Parameter("limit", inspect.Parameter.KEYWORD_ONLY,
                                        default=None, annotation=typing.Optional[int]))
    if getattr(meta, "fields", None) is not None and "fields" not in names:
        params.append(inspect.Parameter("fields", inspect.Parameter.KEYWORD_ONLY, default=None,
                                        annotation=typing.Union[list[str], str, None]))
    # Required parameters first, so the signature is valid Python.
    params.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    return inspect.Signature(params, return_annotation=str)


def _make_tool(cap: str, name: str, sig: inspect.Signature, doc: str, call: CallFn):
    required = {p.name for p in sig.parameters.values() if p.default is inspect.Parameter.empty}

    async def tool(**kwargs: Any) -> str:
        # Omitted optionals arrive as their defaults; send only what differs
        # from "not given" so the cap's own defaults (and core's) apply.
        args = {k: v for k, v in kwargs.items() if k in required or v is not None}
        return await call(cap, args)

    tool.__name__ = name
    tool.__qualname__ = name
    tool.__doc__ = doc
    tool.__signature__ = sig  # type: ignore[attr-defined]
    tool.__annotations__ = {**{p.name: p.annotation for p in sig.parameters.values()},
                            "return": str}
    return tool


def register_cap_tools(mcp: Any, node: Any, call: CallFn) -> list[str]:
    """Add one MCP tool per ``tool=True`` cap on ``node`` (a HubNode).
    Returns the tool names added."""
    added: list[str] = []
    reg = node.host.registry
    for cap in node.host.tool_caps():
        name = tool_name(cap)
        try:
            exists = mcp._tool_manager.get_tool(name) is not None
        except Exception:
            exists = False
        if exists:
            log.warning("hub cap %s: MCP tool %s already exists; not generated", cap, name)
            continue
        fn, meta = reg.handler(cap), reg.meta(cap)
        try:
            sig = _signature(fn, meta)
            doc = _description(cap, fn, meta)
            mcp.add_tool(_make_tool(cap, name, sig, doc, call), name=name, description=doc)
        except Exception:
            log.exception("hub cap %s: generating MCP tool failed", cap)
            continue
        added.append(name)
    return added


def register_plugin_resources(mcp: Any, node: Any) -> list[str]:
    """Add the MCP resources hub plugins return from ``mcp_resources()`` as
    ``(uri, name, description, fn)``. Resources cost nothing in
    ``tools/list``; a client reads one when it wants it. Existing URIs win."""
    added: list[str] = []
    for plugin in node.host.plugins:
        hook = getattr(plugin, "mcp_resources", None)
        if not callable(hook):
            continue
        try:
            items = list(hook() or [])
        except Exception:
            log.exception("hub plugin %s: mcp_resources() failed", plugin.NAMESPACE)
            continue
        for uri, name, description, fn in items:
            try:
                taken = uri in {str(r.uri) for r in mcp._resource_manager.list_resources()}
            except Exception:
                taken = False
            if taken:
                log.warning("hub plugin %s: MCP resource %s already exists; not added",
                            plugin.NAMESPACE, uri)
                continue
            try:
                mcp.resource(uri, name=name, description=description,
                             mime_type="text/plain")(fn)
            except Exception:
                log.exception("hub plugin %s: adding MCP resource %s failed", plugin.NAMESPACE, uri)
                continue
            added.append(uri)
    return added


def register_plugin_tools(mcp: Any, node: Any,
                          identity: Callable[[], "str | None"] = lambda: None) -> list[str]:
    """Add the tools hub plugins return from ``mcp_tools(invoke)``. Each tool
    is an async function whose ``__name__`` is the tool name and whose
    docstring is its description. Existing tool names win."""
    async def invoke(cap: str, args: dict) -> Any:
        return await node.invoke(cap, args, identity())

    added: list[str] = []
    for plugin in node.host.plugins:
        hook = getattr(plugin, "mcp_tools", None)
        if not callable(hook):
            continue
        try:
            tools = list(hook(invoke) or [])
        except Exception:
            log.exception("hub plugin %s: mcp_tools() failed", plugin.NAMESPACE)
            continue
        for fn in tools:
            name = getattr(fn, "__name__", "")
            try:
                exists = mcp._tool_manager.get_tool(name) is not None
            except Exception:
                exists = False
            if not name or exists:
                log.warning("hub plugin %s: MCP tool %r already exists; not added",
                            plugin.NAMESPACE, name)
                continue
            try:
                mcp.tool()(fn)
            except Exception:
                log.exception("hub plugin %s: adding MCP tool %s failed", plugin.NAMESPACE, name)
                continue
            added.append(name)
    return added
