"""The hub's own plugin host: the parts of plugins that run on the hub
(castling-plugins; manifest ``runs_on: hub``, caps ``on: hub``, ``records``,
``gate``, contracts).

    plugins = HubPlugins(records=RecordStore(...), contracts=Contracts(), call=hub.call_as, ...)
    plugins.add_core(core_manifest)            # core.yaml: contracts
    await plugins.load("library/homework")     # records, hub caps, hooks, gate
    plugins.caps()                             # {cap: manifest data} served by the hub node
    await plugins.run("homework.item.create", args, ctx_info)

Calls reach these caps the way they reach any worker: through dispatch, with
a ticket minted for the hub's own node; the runtime hands the request here
instead of to the band. What gets served:

- caps with ``on: hub`` (``run:`` functions get the kit's ctx; ``action:``
  caps run in the action engine);
- the generated caps of ``records:`` types (rook.kit.records);
- one service cap per ``route: provider`` contract (core.yaml), which goes on
  to the chosen provider as the same caller (rook.kit.contracts);
- the ``routes:`` of ``runs_on: hub`` plugins, which the HTTP door serves
  under ``/p/<plugin><path>`` (:meth:`HubPlugins.serve`).

Hooks start only for ``runs_on: hub`` plugins (the rest run on workers). They
act as ``system:plugin.<name>``, whose rights are the plugin's own namespace plus
its ``uses``. A plugin's ``gate:`` becomes a hub gate (rook.core2.hub.gate).
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from jsonschema import Draft202012Validator

from rook.kit._types import parse_duration
from rook.kit.actions import run_action
from rook.kit.actions.parse import file_result
from rook.kit.contracts import Contracts
from rook.kit.ctx import Caller, Ctx, FileResult, KitError, invoke, load_run
from rook.kit.hooks import Scheduler
from rook.kit.manifest import compile_fields, load
from rook.kit.records import RecordStore
from rook.kit.records.caps import record_caps

from .gate import Gate

log = logging.getLogger("rook.hub.plugins")

# call(caller, cap, args, uses=, target=) -> dispatch reply {"ok", "result" | "error"}
CallAs = Callable[..., Awaitable[dict]]


@dataclass
class HubCap:
    meta: dict                                    # manifest data, as dispatch reads it
    handler: Callable[..., Any] | None = None     # handler(ctx, **args)
    action: dict | None = None
    validator: Draft202012Validator | None = None
    plugin: str | None = None


@dataclass
class Route:
    path: str
    methods: list
    auth: str                                     # user | none | hmac | token
    handler: Callable[..., Any]                   # handler(ctx, request)
    secret: str | None = None                     # hmac/token: "{{settings.x}}", "{{secret:NAME}}"
    header: str | None = None                     # hmac/token: the header that carries it


# The header a signature or token arrives in when the route names none:
# GitHub's for HMAC-SHA256 ("sha256=<hex>"), a bearer token for token.
ROUTE_HEADERS = {"hmac": "x-hub-signature-256", "token": "authorization"}
_ONE_TEMPLATE = re.compile(r"\{\{\s*(settings\.([A-Za-z0-9_-]+)|secret:([A-Za-z0-9_.-]+))\s*\}\}")


@dataclass
class HubPlugin:
    name: str
    manifest: dict                                # normalized
    folder: Path | None
    caps: list = field(default_factory=list)      # names served by the hub
    hooks: bool = False
    gate: Gate | None = None
    routes: dict = field(default_factory=dict)    # path -> Route


class HubPlugins:
    def __init__(self, *, records: RecordStore, contracts: Contracts, call: CallAs,
                 emit: Callable[[str, dict, str], Any] | None = None, data_dir: str | Path | None = None,
                 settings: dict | None = None, resolve_secret: Callable[[str], Any] | None = None,
                 hook_timeout: float = 30.0, people: Any = None, band: str = "",
                 streams: Any = None):
        self.records, self.contracts, self._call = records, contracts, call
        self.band = band                        # the hub's band: hooks and routes act in it
        self.emit = emit
        self.data_dir = Path(data_dir) if data_dir else None
        self.settings = settings or {}          # plugin -> {setting: value}
        self.resolve_secret = resolve_secret    # the vault: name -> value (Vault.setting)
        self.people = people                    # owners() and settings(person): ctx.settings_for
        self.streams = streams                  # ctx.streams: the hub's one stream store (castling-streams)
        self.scheduler = Scheduler(dispatch=self._hook_call, timeout=hook_timeout)
        self.plugins: dict[str, HubPlugin] = {}
        self._caps: dict[str, HubCap] = {}
        self._modules: dict[str, dict] = {}     # plugin -> module cache, shared by its caps and hooks

    # -- loading ------------------------------------------------------------

    def add_core(self, manifest: dict) -> None:
        """Core's builtin manifest: its contracts (route: provider ones get a
        service cap here). Its own caps run on workers."""
        self.contracts.load(manifest)

    async def load(self, folder: str | Path) -> HubPlugin:
        """Load the hub part of the plugin in ``folder``. Raises KitError or
        ManifestError; a plugin that fails leaves nothing behind."""
        m = load(folder, self._contract_specs())
        data, name = m.data, m.plugin
        if name in self.plugins:
            raise KitError(f"{name} is already loaded on the hub")
        p = HubPlugin(name, data, m.folder)
        modules = self._modules.setdefault(name, {})
        resolve = lambda ref: load_run(m.folder, ref, modules)  # noqa: E731
        try:
            self.contracts.load(short_caps(data))
            self.records.register(data, plugin_dir=m.folder)
            for cap, gen in record_caps(self.records, name).items():
                self._add(p, cap, HubCap({**data["caps"].get(cap, {}), **gen.spec}, gen.handler))
            for cap, meta in data["caps"].items():
                if meta.get("on") != "hub" or cap in self._caps or meta.get("unavailable"):
                    continue
                if "run" in meta:
                    self._add(p, cap, HubCap(meta, resolve(meta["run"])))
                elif "action" in meta:
                    self._add(p, cap, HubCap(meta, action=meta["action"]))
            if data.get("gate"):
                g = data["gate"]
                p.gate = Gate.from_manifest(name, g, resolve(g["run"]), resolve(g["result"]) if "result" in g else None)
            if data.get("runs_on") == "hub":
                for route in (data.get("routes") or {}).values():
                    auth = route.get("auth", "user")
                    kind = auth if isinstance(auth, str) else ("hmac" if "hmac" in auth else "token")
                    p.routes[route["path"]] = Route(route["path"], list(route.get("methods") or ["GET", "POST"]),
                                                    kind, resolve(route["run"]),
                                                    None if isinstance(auth, str) else auth[kind],
                                                    None if isinstance(auth, str) else auth.get("header"))
            self.plugins[name] = p          # its hooks' calls need its uses
            if data.get("runs_on") == "hub" and data.get("hooks"):
                await self.scheduler.start(data, make_ctx=lambda caller: self._ctx(p, caller),
                                           resolve=resolve, band=self.band)
                p.hooks = True
        except BaseException:
            self.plugins.pop(name, None)
            self._drop(p)
            raise
        log.info("hub plugin loaded", extra={"fields": {"plugin": name, "version": data["version"],
                                                        "caps": len(p.caps), "hooks": p.hooks}})
        return p

    def add_builtin(self, folder: str | Path, handlers: dict) -> HubPlugin:
        """A builtin plugin the hub implements itself (``builtin: true``, no
        ``run:``): each of its ``on: hub`` caps runs ``handlers[cap](ctx, **args)``."""
        m = load(folder)
        if m.plugin in self.plugins:
            raise KitError(f"{m.plugin} is already loaded on the hub")
        p = HubPlugin(m.plugin, m.data, m.folder)
        for cap, meta in m.data["caps"].items():
            if meta.get("on") == "hub":
                self._add(p, cap, HubCap(meta, handlers[cap]))
        self.plugins[p.name] = p
        return p

    async def unload(self, name: str) -> None:
        p = self.plugins.pop(name, None)
        if p is not None:
            await self.scheduler.stop(name)
            self._drop(p)

    async def close(self) -> None:
        await self.scheduler.close()
        for name in list(self.plugins):
            self._drop(self.plugins.pop(name))

    def _drop(self, p: HubPlugin) -> None:
        for cap in p.caps:
            self._caps.pop(cap, None)
        self.contracts.unload(p.name)
        self.records.unregister(p.name)
        self._modules.pop(p.name, None)

    def _add(self, p: HubPlugin, cap: str, hc: HubCap) -> None:
        hc.plugin = p.name
        hc.validator = Draft202012Validator(compile_fields(hc.meta.get("args") or {}))
        self._caps[cap] = hc
        p.caps.append(cap)

    def _contract_specs(self) -> dict:
        return {c: self.contracts.contract(c) for c in self.contracts.contracts() if self.contracts.contract(c)}

    def gates(self) -> list[Gate]:
        return [p.gate for p in self.plugins.values() if p.gate is not None]

    def uses(self, plugin: str) -> list:
        """What a hub plugin may call: its own namespace plus its ``uses``."""
        p = self.plugins.get(plugin)
        return [f"{plugin}.*", *((p.manifest.get("uses") or []) if p else [])]

    # -- what the hub node serves --------------------------------------------

    def caps(self, metas: Callable[[str], dict | None] = lambda cap: None) -> dict:
        """``{cap: manifest data}`` for the hub node. ``metas(cap)`` gives a
        provider cap's data wherever it runs, so a contract's service cap
        carries the chosen provider's risk, scope and effects."""
        out = {cap: hc.meta for cap, hc in self._caps.items()}
        for contract in self.contracts.contracts():
            spec = self.contracts.contract(contract)
            if not spec or spec.get("route") != "provider" or contract in out:
                continue
            try:
                provider = metas(self.contracts.choose(contract)) or {}
            except KitError:
                provider = {}
            out[contract] = {"doc": spec.get("doc", ""), "args": spec.get("args") or {},
                             "risk": provider.get("risk", "read"), "scope": provider.get("scope", "self"),
                             "effects": list(provider.get("effects") or ()), "contract": True,
                             **({"timeout": provider["timeout"]} if "timeout" in provider else {})}
        return out

    async def run(self, cap: str, args: dict | None, caller: Caller) -> Any:
        """Run one hub cap for ``caller`` (from the ticket). Returns the result;
        raises KitError (message for the caller) on failure."""
        args = dict(args or {})
        hc = self._caps.get(cap)
        if hc is None:
            spec = self.contracts.contract(cap)
            if spec and spec.get("route") == "provider":
                return await self._route(cap, args, caller)
            raise KitError(f"unknown capability: {cap}")
        errors = sorted(hc.validator.iter_errors(args), key=lambda e: list(e.absolute_path))
        if errors:
            where = "/".join(map(str, errors[0].absolute_path))
            raise KitError(f"bad args: {where + ': ' if where else ''}{errors[0].message}")
        for key, spec in (hc.meta.get("args") or {}).items():
            if key not in args and "default" in spec:
                args[key] = copy.deepcopy(spec["default"])
        args.update(copy.deepcopy(hc.meta.get("bind") or {}))
        p = self.plugins[hc.plugin]
        if hc.action is not None:
            return await run_action(hc.action, args, settings=self.settings.get(p.name, {}),
                                    worker={"os": "hub"}, secrets=self.resolve_secret,
                                    plugin_dir=str(p.folder) if p.folder else None,
                                    plugin_data=str(self._data(p)) if self.data_dir else None,
                                    timeout=_timeout(hc.meta))
        res = await invoke(hc.handler, self._ctx(p, caller, cap), args)
        if isinstance(res, FileResult):     # ctx.file: the same file map a worker returns
            res = file_result(res.data if res.data is not None else res.path.read_bytes(), res.name, res.mime)
        return res

    def route(self, plugin: str, path: str) -> Route | None:
        p = self.plugins.get(plugin)
        return p.routes.get(path) if p else None

    def route_allows(self, plugin: str, route: Route, headers: dict, body: bytes) -> bool:
        """An ``hmac`` or ``token`` route's check: the request's header
        (lower-case keys) carries HMAC-SHA256 of the body (hex, ``sha256=``
        optional) or the token itself (``Bearer `` optional), under the
        route's secret. A secret that isn't set refuses everything."""
        try:
            secret = self._route_secret(plugin, route.secret or "")
        except (KitError, KeyError, ValueError):
            return False
        given = str(headers.get((route.header or ROUTE_HEADERS[route.auth]).lower()) or "").strip()
        if not secret or not given:
            return False
        if route.auth == "hmac":
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            given = given[7:] if given.lower().startswith("sha256=") else given
            return hmac.compare_digest(expected.encode(), given.lower().encode())
        given = given[7:].strip() if given.lower().startswith("bearer ") else given
        return hmac.compare_digest(secret.encode(), given.encode())

    def _route_secret(self, plugin: str, template: str) -> str:
        m = _ONE_TEMPLATE.fullmatch(template.strip())
        if m is None:
            return template            # a literal (the validator warns: it is public)
        if m.group(3):
            if self.resolve_secret is None:
                raise KitError("no vault")
            return str(self.resolve_secret(m.group(3)) or "")
        p = self.plugins[plugin]
        value = self._ctx(p, Caller(plugin=plugin, band=self.band)).settings[m.group(2)]
        return "" if value is None else str(value)

    async def serve(self, plugin: str, path: str, request: dict, caller: Caller | None = None) -> Any:
        """Run a hub plugin's route for one HTTP request. The door checked the
        method and ``auth``: ``caller`` is the signed-in one for ``user``, None
        for ``none``, ``hmac`` and ``token`` (it runs as the plugin). ``request`` is ``{method, path,
        query, headers, body}``, the body as bytes. The handler answers with
        JSON-able data; KitError or PermissionError refuse."""
        p, route = self.plugins[plugin], self.route(plugin, path)
        who = caller or Caller(plugin=plugin, band=self.band, why=f"{plugin} route {path}")
        return await invoke(route.handler, self._ctx(p, who), {"request": request})

    async def _route(self, contract: str, args: dict, caller: Caller) -> Any:
        async def dispatch(cap, a, c):    # as the contract: grants on its name cover the provider
            return _result(await self._call(c, cap, a, contract=contract))
        return await self.contracts.route(contract, args, caller, dispatch)

    # -- ctx ------------------------------------------------------------------

    def _ctx(self, p: HubPlugin, caller: Caller, cap: str | None = None) -> Ctx:
        async def dispatch(c: str, a: dict, who: Caller, target: str | None = None,
                           contract: str | None = None) -> Any:
            return _result(await self._call(who, c, a, uses=self.uses(p.name), target=target, contract=contract))
        return Ctx(p.manifest, caller, dispatch=dispatch, settings=self.settings.get(p.name, {}),
                   resolve_secret=self.resolve_secret, records=self.records, emit=self.emit,
                   data_dir=self._data(p) if self.data_dir else None, cap=cap, people=self.people,
                   streams=self.streams)

    async def _hook_call(self, cap: str, args: dict, caller: Caller) -> Any:
        """A hook's ``call:`` target: as the plugin, held to its uses."""
        return _result(await self._call(caller, cap, args, uses=self.uses(caller.plugin or "")))

    def _data(self, p: HubPlugin) -> Path:
        return self.data_dir / p.name


def short_caps(data: dict) -> dict:
    """A normalized manifest with caps keyed by their short names, as
    rook.kit.contracts reads them."""
    p = data["plugin"] + "."
    return {**data, "caps": {k[len(p):]: v for k, v in data["caps"].items() if k.startswith(p)}}


def _result(reply: dict) -> Any:
    if not reply.get("ok"):
        hold = f" (hold {reply['hold']})" if reply.get("held") else ""   # the inner call waits for a person
        raise KitError(str(reply.get("error", "failed")) + hold, retry=bool(reply.get("retry")))
    return reply.get("result")


def _timeout(meta: dict) -> float:
    try:
        return parse_duration(meta["timeout"]) if meta.get("timeout") else 15.0
    except ValueError:
        return 15.0
