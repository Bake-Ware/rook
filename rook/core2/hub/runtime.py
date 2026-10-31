"""The 0.5 hub process: the wave-1/2 libraries assembled into one runtime
(docs/spec/core-v2.md; castling-design).

    hub = Hub(load_config("hub.yaml"))
    await hub.start()        # keys, stores, people, library, dispatch, band, doors
    ...
    await hub.stop()

What it owns:

- **Keys** (5.2): a root key (the hub's identity, ``hub_id``) and an
  operational key, made on first run under ``<data>/keys``. The root signs a
  ``rook-hubkey`` grant for the operational key, renewed daily; the
  operational key is replaced after 30 days.
- **People and agents** (8): users with presets (core.yaml's, plus config
  overrides and plugin grants), agents with rights, bearer tokens (only their
  hashes are stored, in ``<data>/tokens.json``).
- **Dispatch** (10): one :class:`~rook.core2.hub.dispatch.Dispatcher` every
  door ends at, with the library's gates; the journal (11) in
  ``<data>/journal.db``.
- **Plugins**: the trusted library (one folder per plugin) is validated at
  start; the hub hosts the parts that run on it (:mod:`.plugins`: hub caps,
  records in ``<data>/records.db``, hooks, gates, contract routing).
- **Workers** (5.3-5.6): announces and heartbeats keep a registry. Manifests
  are resolved from the library by hash, else fetched with ``core.manifests``
  and validated; caps stay known while a worker is offline (and across hub
  restarts, ``<data>/workers.json``). Presence follows heartbeats (90 s).
- **The hub node**: calls to ``rook`` (the hub's own caps) are dispatched like
  any other and run in-process; the band's ``core.elevate`` and ``core.call``
  requests are answered here (13).
- **Enrollment** (:mod:`rook.core2.enroll.hub`): devices signing in as a
  person, the config bundle, the owner's recovery key, other hubs' invites
  and the memberships this hub signs for its workers (spec 9, 12).
- **Doors** (:mod:`.doors`): the ways in. They authenticate a token and call
  the async door API below (:meth:`Hub.call`, :meth:`Hub.workers_view`, ...).

The band comes through a transport with three methods (``start(on_message)``,
``send(band, msg)``, ``close()``); :class:`Loopback` is the in-memory one.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import random
import re
import secrets as _secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import yaml
from nacl.signing import SigningKey

from rook.kit.contracts import Contracts
from rook.kit.ctx import Caller as KitCaller, KitError
from rook.kit.manifest import check, normalize, validate
from rook.kit.manifest.diff import semver_key
from rook.kit.records import RecordStore

from .. import CORE_VERSION, rights, wire
from ..builtin import CORE
from ..enroll.hub import Enrollment, check_options as check_enroll, recovery_key
from . import distribute
from .dispatch import Call, Dispatcher, Worker
from .doors.web import caps as builtin_caps
from .identity import Caller, Identities, digest
from .journal_store import JournalError, JournalStore
from .plugins import HubPlugins, short_caps

log = logging.getLogger("rook.hub")

HEX32 = re.compile(r"[0-9a-f]{32}")
ANNOUNCE_EVERY = 30.0         # hub announce, ±20 % (5.4)
OFFLINE_AFTER = 90.0          # roster eviction (5.5)
GRANT_DAYS = 7                # rook-hubkey lifetime; renewed daily (5.2)
OP_KEY_DAYS = 30              # operational key rotation (5.2)
DAY = 86400.0
SYSTEM = Caller(agent="system:hub")
# The hub-node call a hub cap runs inside: (session id, the door's layers, door).
# Its ctx.call in that session carries them (a page's caps, the door).
_PARENT: contextvars.ContextVar = contextvars.ContextVar("rook_hub_parent_call", default=None)


# -- config -------------------------------------------------------------------

@dataclass
class Config:
    data: Path
    band: str | None = None
    library: Path | None = None
    users: dict = field(default_factory=dict)       # name -> {preset, grants}
    agents: dict = field(default_factory=dict)      # name -> rights
    presets: dict = field(default_factory=dict)     # preset -> overrides of core.yaml's
    settings: dict = field(default_factory=dict)    # hub plugin -> {setting: value}
    contracts: dict = field(default_factory=dict)   # contract -> chosen provider cap
    sacred: list = field(default_factory=list)      # worker names: changes there are hard stops
    doors: dict = field(default_factory=lambda: {"http": {"bind": "127.0.0.1:8740"}})
    transport: dict | None = None                   # rook.core2.transport.hub_transport options
    enroll: dict = field(default_factory=dict)      # rook.core2.enroll.hub: url, relay, ws_url, discover, services
    log: dict = field(default_factory=dict)         # {level, format: text|json}


def load_config(path: str | Path) -> Config:
    """Read and check the YAML config. Relative paths are from its folder."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: not a mapping")
    known = set(Config.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"{path}: unknown keys: {', '.join(unknown)}")
    if not raw.get("data"):
        raise ValueError(f"{path}: data (the data folder) is required")
    base = path.resolve().parent
    raw["data"] = base / raw["data"]
    if raw.get("library"):
        raw["library"] = base / raw["library"]
    cfg = Config(**{k: v for k, v in raw.items() if v is not None})
    check_config(cfg)
    return cfg


def check_config(cfg: Config) -> None:
    if cfg.band is not None and not (isinstance(cfg.band, str) and HEX32.fullmatch(cfg.band)):
        raise ValueError("band must be a 32-hex band id")
    if cfg.band is None and not cfg.transport:
        raise ValueError("band is required without a transport")
    for name, kind in (("users", dict), ("agents", dict), ("presets", dict), ("settings", dict),
                       ("contracts", dict), ("sacred", list), ("doors", dict), ("log", dict)):
        if not isinstance(getattr(cfg, name), kind):
            raise ValueError(f"{name} must be a {kind.__name__}")
    for name, u in cfg.users.items():
        if not (isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_-]*", name)) or not isinstance(u, dict):
            raise ValueError(f"users: bad entry {name!r}")
    if not any(u.get("preset") == "owner" for u in cfg.users.values()):
        raise ValueError("users: seed at least one owner (preset: owner)")
    check_enroll(cfg.enroll)


# -- keys -----------------------------------------------------------------------

def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _key(path: Path, max_age: float | None = None, now: float | None = None) -> SigningKey:
    """The key in ``path`` (hex seed), made when missing or older than ``max_age``."""
    now = time.time() if now is None else now
    if path.exists() and (max_age is None or now - path.stat().st_mtime < max_age):
        return SigningKey(bytes.fromhex(path.read_text(encoding="ascii").strip()))
    sk = SigningKey.generate()
    _write_private(path, bytes(sk).hex() + "\n")
    log.info("new key", extra={"fields": {"key": path.name, "kid": wire.key_id(bytes(sk.verify_key))}})
    return sk


def env_secret(name: str) -> str:
    """A hub plugin's secret setting, by vault name. TODO(castling:vault):
    stand-in until the hub has its vault: the value comes from the hub
    process's environment, ``ROOK_SECRET_<NAME>`` (upper case, anything not a
    letter or digit as ``_``), so it is never in a config file."""
    var = "ROOK_SECRET_" + re.sub(r"[^A-Z0-9]", "_", str(name).upper())
    value = os.environ.get(var)
    if not value:
        raise KitError(f"secret {name} is not set (no vault yet: set {var} for the hub)")
    return value


def home_band(cfg: Config) -> str:
    """The hub's band: ``band``, else the first transport band's PSK's."""
    if cfg.band:
        return cfg.band
    from rook.core2.transport import band_of
    bands = (cfg.transport or {}).get("bands") or []
    psk = bands[0].get("psk") or os.environ.get(bands[0].get("psk_env", ""), "") if bands else ""
    if not psk:
        raise ValueError("transport: the first band needs psk or psk_env")
    return band_of(psk)


# -- transport ------------------------------------------------------------------

OnMessage = Callable[[str, dict], Awaitable[None]]


class Transport(Protocol):
    """The band, as the hub uses it (rook.core2.transport)."""

    async def start(self, on_message: OnMessage) -> None: ...
    async def send(self, band: str, msg: dict) -> None: ...
    async def close(self) -> None: ...


class Loopback:
    """An in-memory band: ``attach(band, handler)`` adds a peer whose
    ``handler(msg)`` gets everything the hub sends there, and returns the
    coroutine function that peer sends to the hub with. Deliveries run as
    tasks, as on a real band, and carry copies."""

    def __init__(self) -> None:
        self.on_message: OnMessage | None = None
        self.peers: dict[str, list] = {}
        self._tasks: set[asyncio.Task] = set()

    async def start(self, on_message: OnMessage) -> None:
        self.on_message = on_message

    async def send(self, band: str, msg: dict) -> None:
        for handler in self.peers.get(band, ()):
            self._spawn(handler(json.loads(json.dumps(msg))))

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.on_message = None

    def attach(self, band: str, handler: Callable[[dict], Awaitable[None]]) -> Callable[[dict], Awaitable[None]]:
        self.peers.setdefault(band, []).append(handler)

        async def to_hub(msg: dict) -> None:
            if self.on_message is not None:
                self._spawn(self.on_message(band, json.loads(json.dumps(msg))))
        return to_hub

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)


def make_transport(cfg: Config, node: str) -> Transport:
    if not cfg.transport:
        return Loopback()      # no band configured: doors and hub plugins only
    from rook.core2.transport import hub_transport   # TODO(castling:transport): lands in wave 3
    return hub_transport({**cfg.transport, "node_id": node})


# -- workers --------------------------------------------------------------------

@dataclass
class Known:
    """A worker as its last announce and heartbeat describe it."""
    id: str
    name: str
    band: str
    core: str | None = None              # negotiated version; None: incompatible
    announced_core: Any = None
    version: str | None = None
    description: str | None = None
    facts: dict = field(default_factory=dict)
    manifests: list = field(default_factory=list)       # [{plugin, version, hash, builtin?}]
    manifests_hash: str | None = None
    unavailable: dict = field(default_factory=dict)
    ceiling: dict | None = None
    role: str | None = None
    status: dict = field(default_factory=dict)
    last_seen: float = 0.0
    online: bool = False
    problem: str | None = None           # why some or all of it can't be used
    tried: str | None = None             # the last heartbeat manifests_hash a fetch was made for

    def saved(self) -> dict:
        d = asdict(self)
        for k in ("status", "online"):
            d.pop(k)
        return d


def _entries_ok(entries: Any) -> bool:
    return isinstance(entries, list) and all(
        isinstance(e, dict) and all(isinstance(e.get(k), str) and e[k] for k in ("plugin", "version", "hash"))
        for e in entries)


# -- logging --------------------------------------------------------------------

class Formatter(logging.Formatter):
    """``text``: time level logger: message k=v...; ``json``: one object per line.
    Structured fields ride in ``extra={"fields": {...}}``."""

    def __init__(self, fmt: str = "text"):
        super().__init__()
        self.json = fmt == "json"

    def format(self, r: logging.LogRecord) -> str:
        fields = getattr(r, "fields", None) or {}
        if self.json:
            d = {"ts": round(r.created, 3), "level": r.levelname.lower(), "logger": r.name,
                 "msg": r.getMessage(), **fields}
            if r.exc_info:
                d["exc"] = self.formatException(r.exc_info)
            return json.dumps(d, default=str)
        text = f"{self.formatTime(r)} {r.levelname} {r.name}: {r.getMessage()}"
        text += "".join(f" {k}={v}" for k, v in fields.items())
        return text + ("\n" + self.formatException(r.exc_info) if r.exc_info else "")


def setup_logging(opts: dict) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(Formatter(opts.get("format", "text")))
    root = logging.getLogger("rook")
    root.handlers[:] = [handler]
    root.setLevel(str(opts.get("level", "info")).upper())
    root.propagate = False


# -- the hub --------------------------------------------------------------------

class Hub:
    def __init__(self, config: Config, *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time):
        check_config(config)
        self.cfg, self.clock = config, clock
        self.data = Path(config.data)
        self._transport = transport
        self.started = False
        self._tasks: set[asyncio.Task] = set()
        self._loops: list[asyncio.Task] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._late: dict[str, str] = {}          # call id -> worker, for calls given up on (bounded)
        self._locks: dict[str, asyncio.Lock] = {}
        self._door_sessions: dict[tuple, str] = {}
        self._component_sessions: dict[str, str] = {}
        self._acting_sessions: dict[tuple, str] = {}   # (person, agent) -> session for ctx.call_as
        self._seq = 0
        self._last_announce = 0.0
        self.known: dict[str, Known] = {}
        self.manifests: dict[str, dict] = {}     # hash -> parsed manifest
        self._norm: dict[str, dict] = {}         # hash -> normalized
        self.doors: list = []
        self.listeners: list = []                # fn(event, payload, source) for every event

    # -- start and stop ---------------------------------------------------------

    async def start(self) -> None:
        try:
            await self._start()
        except BaseException:
            await self._teardown()      # a failed start leaves nothing open
            raise
        self.started = True
        log.info("hub started", extra={"fields": {"hub": self.hub_id, "node": self.node, "band": self.band,
                                                  "plugins": len(self.plugins.plugins), "workers": len(self.known),
                                                  "doors": [d.name for d in self.doors]}})

    async def _start(self) -> None:
        cfg = self.cfg
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.data / "keys").mkdir(exist_ok=True, mode=0o700)
        self.loop = asyncio.get_running_loop()
        self.root = _key(self.data / "keys" / "root.key")
        self.hub_id = wire.key_id(bytes(self.root.verify_key))
        self.op = _key(self.data / "keys" / "op.key", OP_KEY_DAYS * DAY, self.clock())
        self.node = self._node_id()
        self.transport = self._transport or make_transport(cfg, self.node)
        self.band = home_band(cfg)
        self.grant = self._grant()

        self.journal = JournalStore(str(self.data / "journal.db"), clock=self.clock)
        self.records = RecordStore(str(self.data / "records.db"), emit=self._emit, clock=self.clock)
        core_raw, issues = check(CORE)
        if core_raw is None or any(i.level == "error" for i in issues):
            raise RuntimeError(f"core.yaml: {issues}")
        self.core = normalize(core_raw)
        self._cache(core_raw)
        self.identities = Identities(self._presets())
        for name, u in cfg.users.items():
            self.identities.add_user(name, u.get("preset", "adult"), u.get("grants") or [], u.get("created", 0))
        for name, r in cfg.agents.items():
            self.identities.add_agent(name, r)
        self._load_tokens()

        self.contracts = Contracts(dict(cfg.contracts))
        self.plugins = HubPlugins(records=self.records, contracts=self.contracts, call=self.call_as,
                                  emit=self._emit, data_dir=self.data / "plugins", settings=cfg.settings,
                                  resolve_secret=env_secret, band=self.band)
        self.plugins.add_core(self.core)
        builtin_caps.install(self)               # pages, hub, approval, journal, setting: before the library
        self.enroll = Enrollment(self, cfg.enroll)
        self.dispatcher = Dispatcher(hub_id=self.hub_id, op_key=self.op, identities=self.identities,
                                     workers={}, send=self._send, journal=self._journal,
                                     hubkey=self.grant, clock=self.clock, secrets=self.enroll.resolve)
        self.dispatcher.closed_doors = set(_read_doors(self.data))
        self._sync_hub()
        await self._load_library()
        self.dispatcher.gates = self.plugins.gates()
        self._load_manifests()
        self._load_workers()
        self._sync_hub()
        await self.transport.start(self._on_message)
        self._loops = [self._every(ANNOUNCE_EVERY, self._announce_hub, jitter=0.2, now=True),
                       self._every(15.0, self._liveness),
                       self._every(3600.0, self._hourly),
                       self._every(DAY, self._daily)]
        from .doors import make_door
        for name, opts in cfg.doors.items():
            door = make_door(name, self, opts or {})
            await door.start()
            self.doors.append(door)
        if cfg.enroll.get("discover"):
            from ..enroll.mdns import Responder
            self.mdns = Responder(cfg.enroll["url"], self.hub_id)
            try:
                await self.mdns.start()
            except OSError as e:          # the port is taken: devices can still type the address
                log.warning("not discoverable", extra={"fields": {"error": str(e)}})
                self.mdns = None

    async def stop(self) -> None:
        """Doors first (nothing new comes in), then background work, the band,
        hub plugins' hooks and the stores."""
        if not self.started:
            return
        self.started = False
        await self._teardown()
        log.info("hub stopped", extra={"fields": {"hub": self.hub_id}})

    async def _teardown(self) -> None:
        """Close whatever start opened, in reverse; safe after a partial start."""
        for door in reversed(self.doors):
            try:
                await door.stop()
            except Exception as e:
                log.warning("door stop failed", extra={"fields": {"door": door.name, "error": str(e)}})
        self.doors.clear()
        if getattr(self, "mdns", None) is not None:
            await self.mdns.stop()
            self.mdns = None
        for t in self._loops + list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._loops, *self._tasks, return_exceptions=True)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"ok": False, "error": "hub stopping"})
        if hasattr(self, "transport"):
            await self.transport.close()
        if hasattr(self, "plugins"):
            await self.plugins.close()
        if hasattr(self, "dispatcher"):
            self._save_workers()
        for store in ("journal", "records"):
            if hasattr(self, store):
                getattr(self, store).close()

    def _every(self, seconds: float, fn: Callable[[], Any], jitter: float = 0.0, now: bool = False) -> asyncio.Task:
        async def loop():
            first = now
            while True:
                if not first:
                    await asyncio.sleep(seconds * (1 + random.uniform(-jitter, jitter)))
                first = False
                try:
                    res = fn()
                    if asyncio.iscoroutine(res):
                        await res
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("periodic task failed", extra={"fields": {"task": fn.__name__}})
        return asyncio.get_running_loop().create_task(loop())

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._done)

    def _done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("task failed", exc_info=t.exception())

    # -- identity, keys and state on disk ------------------------------------------

    def _node_id(self) -> str:
        path = self.data / "hub.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not HEX32.fullmatch(str(state.get("node", ""))):
            state["node"] = _secrets.token_hex(16)
            _write_private(path, json.dumps(state))
        return state["node"]

    def _grant(self) -> dict:
        now = int(self.clock())
        return wire.sign(self.root, {"typ": "rook-hubkey", "v": 2, "hub": self.hub_id,
                                     "key": wire.pub_text(self.op), "bands": [self.band],
                                     "iat": now, "exp": now + GRANT_DAYS * int(DAY)})

    def _presets(self) -> dict:
        presets = {k: dict(v) for k, v in (self.core.get("presets") or {}).items()}
        for name, over in self.cfg.presets.items():
            presets[name] = {**presets.get(name, {}), **(over or {})}
            if not rights.valid_rights({"grants": [], **presets[name]}):
                raise ValueError(f"presets: {name} is malformed")
        return presets

    @property
    def _tokens_path(self) -> Path:
        return self.data / "tokens.json"

    def _load_tokens(self) -> None:
        """Token hashes from ``tokens.json``. On first run, one token for the
        first owner, written to ``owner.token`` (0600) for the person to take."""
        path = self._tokens_path
        if not path.exists():
            owner = next(n for n, u in self.cfg.users.items() if u.get("preset") == "owner")
            token = self.add_token(Caller(person=f"person:{owner}"), label="first owner")
            _write_private(self.data / "owner.token", token + "\n")
            recovery_key(self.data)
            log.info("owner token written", extra={"fields": {"person": owner,
                                                              "file": str(self.data / "owner.token")}})
            log.info("recovery key made: an owner sees it with the recovery-key command or GET /recovery-key")
            return
        self._tokens_mtime = path.stat().st_mtime
        for d, rec in json.loads(path.read_text(encoding="utf-8")).items():
            try:
                self.identities.add_token_digest(d, Caller(rec.get("person"), rec.get("agent")))
            except ValueError as e:
                log.warning("bad token entry", extra={"fields": {"error": str(e)}})

    def add_token(self, caller: Caller, label: str = "") -> str:
        """A new door token for ``caller``, stored by hash. Returned once."""
        return add_token(self.data, caller, label, self.identities)

    def authenticate(self, token: str) -> Caller | None:
        """A door's bearer token to its caller; picks up tokens minted by
        ``python -m rook.core2.hub token`` while the hub runs."""
        caller = self.identities.authenticate(token)
        if caller is None and self._tokens_path.exists() and \
                self._tokens_path.stat().st_mtime != getattr(self, "_tokens_mtime", None):
            self._load_tokens()
            caller = self.identities.authenticate(token)
        if caller is not None and caller.person and self.identities.user(caller.person) is None:
            return None          # a person no longer in the config
        return caller

    # -- the library and manifests --------------------------------------------------

    async def _load_library(self) -> None:
        lib = self.cfg.library
        if lib is None:
            return
        if not lib.is_dir():
            raise ValueError(f"library {lib} is not a folder")
        for folder in sorted(p for p in lib.iterdir() if (p / "plugin.yaml").is_file()):
            raw, issues = check(folder, self._contract_specs())
            if raw is None or any(i.level == "error" for i in issues):
                log.error("library plugin refused", extra={"fields": {
                    "folder": folder.name, "issues": "; ".join(str(i) for i in issues if i.level == "error")}})
                continue
            try:
                p = await self.plugins.load(folder)
            except Exception as e:
                log.error("library plugin failed", extra={"fields": {"plugin": raw.get("plugin"), "error": str(e)}})
                continue
            self._cache(raw)
            self.identities.add_plugin_presets(p.name, p.manifest.get("presets") or {})
            self._sync_hub()

    def _contract_specs(self) -> dict:
        return {c: self.contracts.contract(c) for c in self.contracts.contracts() if self.contracts.contract(c)}

    def _cache(self, raw: dict) -> str:
        h = wire.manifest_hash(raw)
        self.manifests[h] = raw
        self._norm[h] = normalize(raw)
        return h

    def _load_manifests(self) -> None:
        folder = self.data / "manifests"
        for f in sorted(folder.glob("*.json")) if folder.is_dir() else ():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                if wire.manifest_hash(raw) == f.stem and not _errors(raw):
                    self._learn(raw)
            except (ValueError, OSError) as e:
                log.warning("cached manifest skipped", extra={"fields": {"file": f.name, "error": str(e)}})

    def _learn(self, raw: dict) -> str:
        """A manifest a worker runs that isn't in the library: known by hash,
        its contracts and providers registered. Its hub parts don't run here:
        the hub only runs library code."""
        h = self._cache(raw)
        try:
            self.contracts.load(short_caps(self._norm[h]))
        except KitError as e:
            log.warning("contracts not registered", extra={"fields": {"plugin": raw.get("plugin"), "error": str(e)}})
        return h

    def _save_manifest(self, h: str) -> None:
        folder = self.data / "manifests"
        folder.mkdir(exist_ok=True)
        (folder / f"{h}.json").write_text(json.dumps(self.manifests[h]), encoding="utf-8")

    # -- workers ----------------------------------------------------------------

    def _load_workers(self) -> None:
        path = self.data / "workers.json"
        if not path.exists():
            return
        for rec in json.loads(path.read_text(encoding="utf-8")):
            try:
                k = Known(**rec)
            except TypeError:
                continue
            if HEX32.fullmatch(k.id) and k.band == self.band:
                self.known[k.id] = k
                self._sync(k)

    def _save_workers(self) -> None:
        _write_private(self.data / "workers.json",
                       json.dumps([k.saved() for k in self.known.values()], default=str))

    def _sync(self, k: Known) -> None:
        """Put what the hub knows of ``k`` into dispatch (or take it out when
        no version is in common)."""
        if k.core is None:
            self.dispatcher.workers.pop(k.id, None)
            return
        caps = {n: m for n, m in self.core["caps"].items() if m.get("on") == "worker"}
        uses = {}
        for e in k.manifests:
            norm = self._norm.get(e["hash"])
            if norm is None or norm["plugin"] != e["plugin"]:
                continue
            plugin = norm["plugin"]
            caps.update({n: self._contracted(m) for n, m in norm["caps"].items() if m.get("on", "worker") == "worker"})
            uses[plugin] = list(norm.get("uses") or ())
        ceiling = k.ceiling if rights.valid_rights(k.ceiling) else {"grants": ["*"]}
        self.dispatcher.workers[k.id] = Worker(k.id, k.name, k.band, caps, ceiling, uses=uses,
                                               sacred=k.name in self.cfg.sacred)

    def _contracted(self, meta: dict) -> dict:
        """A cap's data for dispatch: its ``contracts`` (from provides) only
        those defined here, so a plugin can't make up a grantable name."""
        out = dict(meta)
        if out.get("contracts"):
            out["contracts"] = [c for c in out["contracts"] if self.contracts.contract(c) is not None]
        return out

    def _sync_hub(self) -> None:
        caps = {n: self._contracted(m) for n, m in self.plugins.caps(self._meta).items()}
        self.dispatcher.workers[self.node] = Worker(self.node, "rook", self.band, caps, {"grants": ["*"]},
                                                    uses={p: self.plugins.uses(p) for p in self.plugins.plugins})

    def _meta(self, cap: str) -> dict | None:
        for w in self.dispatcher.workers.values():
            if cap in w.caps:
                return w.caps[cap]
        for norm in self._norm.values():
            if cap in norm["caps"]:
                return norm["caps"][cap]
        return None

    def _lock(self, wid: str) -> asyncio.Lock:
        return self._locks.setdefault(wid, asyncio.Lock())

    async def _worker_announce(self, band: str, msg: dict) -> None:
        wid, name, entries = msg.get("worker_id"), msg.get("name"), msg.get("manifests")
        if not (isinstance(wid, str) and HEX32.fullmatch(wid) and isinstance(name, str) and name
                and name != "rook" and _entries_ok(entries)):
            log.warning("bad worker announce", extra={"fields": {"worker": str(wid)[:40]}})
            return
        try:
            if msg.get("manifests_hash") != _entries_hash(entries):
                raise ValueError("manifests_hash doesn't match")
        except ValueError as e:
            log.warning("bad worker announce", extra={"fields": {"worker": wid, "error": str(e)}})
            return
        if any(o.name == name and o.id != wid for o in self.known.values()):
            log.warning("a second worker id claims a known name", extra={"fields": {"worker": wid, "name": name}})
        async with self._lock(wid):
            k = self.known.get(wid) or Known(wid, name, band)
            before = (k.online, k.version)
            k.name, k.band = name, band
            k.announced_core = msg.get("core")
            k.core = wire.negotiate(CORE_VERSION, msg.get("core"))
            k.version = msg.get("version") if isinstance(msg.get("version"), str) else None
            k.description = str(msg["description"])[:280] if msg.get("description") else None
            k.facts = _facts(msg.get("facts"))
            k.manifests, k.manifests_hash = entries, msg["manifests_hash"]
            k.unavailable = msg.get("unavailable") if isinstance(msg.get("unavailable"), dict) else {}
            k.ceiling = msg.get("ceiling") if isinstance(msg.get("ceiling"), dict) else None
            k.role = msg.get("role") if isinstance(msg.get("role"), str) else None
            k.status = msg.get("status") if isinstance(msg.get("status"), dict) else {}
            k.last_seen, k.online = self.clock(), True
            k.problem = None if k.core else f"incompatible: core {k.announced_core!r}, hub {CORE_VERSION}"
            self.known[wid] = k
            self._sync(k)
            missing = [e["plugin"] for e in entries if e["hash"] not in self.manifests]
            if missing and k.core:
                await self._fetch(k, missing)
            self._sync(k)
            self._sync_hub()
            self._save_workers()
        self._presence(k, *before)
        if self.clock() - self._last_announce > 5:
            await self._announce_hub()     # a worker that just joined gets hub contact now

    async def _heartbeat(self, band: str, msg: dict) -> None:
        wid = msg.get("worker_id")
        if not (isinstance(wid, str) and HEX32.fullmatch(wid)):
            return
        async with self._lock(wid):
            k = self.known.get(wid)
            if k is None:      # an unknown worker: learn it from core.manifests (5.5)
                k = self.known[wid] = Known(wid, wid, band)
            before = (k.online, k.version)
            k.last_seen, k.online = self.clock(), True
            k.core = wire.negotiate(CORE_VERSION, msg.get("core"))
            if isinstance(msg.get("status"), dict):
                k.status = msg["status"]
            mhash = msg.get("manifests_hash")
            if k.core and mhash != k.manifests_hash and mhash != k.tried:
                k.tried = mhash       # once per new hash: a worker that keeps failing isn't asked every 30 s
                self._sync(k)
                await self._fetch(k, None)
                if k.manifests_hash != mhash:
                    k.problem = "manifests changed while the hub fetched them"
                self._sync_hub()
                self._save_workers()
            self._sync(k)
        self._presence(k, *before)

    async def _fetch(self, k: Known, plugins: list | None) -> None:
        """Get manifests the hub doesn't have from the worker (``core.manifests``),
        validate and keep them. ``plugins=None`` fetches the whole view and
        replaces the worker's entries with it."""
        args = {"plugins": plugins} if plugins is not None else {}
        reply = await self.dispatcher.dispatch(SYSTEM, Call(
            cap="core.manifests", target=k.id, args=args, session=self._system_session(),
            why=f"Learn the plugins {k.name} runs: this hub doesn't have all of their manifests."))
        got = (reply.get("result") or {}).get("manifests") if reply.get("ok") and \
            isinstance(reply.get("result"), dict) else None
        if not isinstance(got, list):
            k.problem = f"manifests: {reply.get('error', 'malformed reply')}"
            return
        wanted = {e["hash"] for e in k.manifests}
        fetched = []
        for raw in got:
            try:
                h = wire.manifest_hash(raw) if isinstance(raw, dict) else None
            except ValueError:
                h = None
            if h is None or (plugins is not None and h not in wanted):
                continue
            problems = _errors(raw)
            if problems:
                log.warning("worker manifest refused", extra={"fields": {
                    "worker": k.name, "plugin": raw.get("plugin"), "issues": problems}})
                continue
            if h not in self.manifests:
                self._learn(raw)
                self._save_manifest(h)
            e = {"plugin": raw["plugin"], "version": str(raw["version"]), "hash": h}
            if raw.get("builtin"):
                e["builtin"] = True
            fetched.append(e)
        if plugins is None:
            k.manifests = sorted(fetched, key=lambda e: e["plugin"])
            k.manifests_hash = _entries_hash(k.manifests)
        missing = sorted(e["plugin"] for e in k.manifests if e["hash"] not in self.manifests)
        k.problem = f"unresolved manifests: {', '.join(missing)}" if missing else None

    def _presence(self, k: Known, was_online: bool, old_version: str | None) -> None:
        if not was_online:
            self._emit("worker.online", {"worker": k.id, "name": k.name, "version": k.version}, "core")
        if old_version and k.version and old_version != k.version:
            self._emit("worker.updated", {"worker": k.id, "name": k.name, "from": old_version,
                                          "to": k.version}, "core")

    def _liveness(self) -> None:
        now = self.clock()
        for k in self.known.values():
            if k.online and now - k.last_seen > OFFLINE_AFTER:
                k.online = False
                self._emit("worker.offline", {"worker": k.id, "name": k.name, "last_seen": k.last_seen}, "core")

    # -- the band -----------------------------------------------------------------

    async def _on_message(self, band: str, msg: dict) -> None:
        """Everything the transport receives. Never blocks it: work that waits
        on the band (manifest fetches, sub-calls) runs as its own task."""
        if not isinstance(msg, dict) or band != self.band:
            return
        cap = msg.get("cap")
        if isinstance(cap, str) and cap:
            if msg.get("target") == self.node:
                self._spawn(self._band_request(band, msg))
            return
        if "cap" in msg:
            return
        kind = msg.get("kind")
        if kind == "announce":
            if msg.get("typ") != "rook-announce":     # another hub's announce is not a worker
                self._spawn(self._worker_announce(band, msg))
        elif kind == "heartbeat":
            self._spawn(self._heartbeat(band, msg))
        elif kind is None and isinstance(msg.get("id"), str) and "ok" in msg and "from" in msg:
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                fut.set_result(msg)
            elif self._late.get(msg["id"]) == msg.get("from"):    # only calls this hub gave up on
                del self._late[msg["id"]]
                self._journal({"kind": "late_reply", "ts": self.clock(), "hub": self.hub_id,
                               "call": msg["id"], "worker": msg["from"], "ok": msg.get("ok") is True,
                               **({"error": str(msg.get("error"))[:500]} if msg.get("ok") is not True else {})})

    async def _band_request(self, band: str, req: dict) -> None:
        """``core.elevate`` and ``core.call`` from a worker, each authenticated
        by the ticket of a call in flight (13)."""
        rid, args = req.get("id"), req.get("args") if isinstance(req.get("args"), dict) else {}
        if not isinstance(rid, str) or not rid:
            return
        ticket = args.get("ticket")
        worker = ticket.get("worker") if isinstance(ticket, dict) else None
        if req["cap"] == "core.elevate":
            out = self.dispatcher.elevate(worker, ticket, args.get("reason"))
        elif req["cap"] == "core.call":
            target, cap = args.get("target"), args.get("cap")
            if not (isinstance(cap, str) and isinstance(target, str)):
                out = {"ok": False, "error": "bad args: core.call needs cap and target"}
            else:
                out = await self.dispatcher.sub_call(
                    worker, ticket, cap, target, args.get("args") if isinstance(args.get("args"), dict) else None,
                    args.get("why") if isinstance(args.get("why"), str) else "")
        else:
            out = {"ok": False, "error": f"unknown capability: {req['cap']}"}
        reply = {"id": rid, "from": self.node, "ok": bool(out.get("ok"))}
        reply.update({k: out[k] for k in ("result", "error", "retry", "held", "hold") if k in out})
        if reply["ok"]:
            reply.setdefault("result", None)
        await self.transport.send(band, reply)

    async def _send(self, band: str, request: dict) -> dict:
        """Dispatch's ``send``: the hub's own caps run here, the rest go over
        the band and wait for the reply (dispatch owns the timeout)."""
        if request["target"] == self.node:
            return await self._local(request)
        fut = self.loop.create_future()
        self._pending[request["id"]] = fut
        try:
            await self.transport.send(band, request)
            return await fut
        finally:
            if self._pending.get(request["id"]) is fut:
                del self._pending[request["id"]]
            if not fut.done():           # timed out or cancelled: a reply may still come
                self._late[request["id"]] = request["target"]
                while len(self._late) > 1000:
                    del self._late[next(iter(self._late))]

    async def _local(self, req: dict) -> dict:
        """A call to the hub node. Dispatch minted its ticket a moment ago in
        this process, so it is read, not verified; who it is for comes only
        from it (6.6)."""
        t = req["ticket"]
        caller = KitCaller(person=t.get("person"), agent=t.get("agent"), session=t["session"], band=t["band"],
                           why=t["why"], owner=self.identities.is_owner(t.get("person")))
        out = {"id": req["id"], "from": self.node}
        # The layers the door put on this call (a page's caps, its authors) and
        # the door ride on its ctx.call; plugin ``uses`` don't compose: each
        # hub plugin's own apply at each hop (the agents runtime relies on it).
        held = self.dispatcher._inflight.get(t["nonce"])
        parent = _PARENT.set((t["session"], [layer for layer in held[3] if layer[0] != "uses"], held[4])
                             if held else None)
        try:
            out.update(ok=True, result=await self.plugins.run(req["cap"], req.get("args"), caller))
        except asyncio.CancelledError:
            raise
        except (Exception, SystemExit) as e:    # plugin code never takes the hub down
            msg = getattr(e, "message", None) or f"{req['cap']} failed: {type(e).__name__}: {e}"
            out.update(ok=False, error=msg)
            if getattr(e, "retry", False):
                out["retry"] = True
            if not isinstance(e, KitError):
                log.warning("hub cap failed", extra={"fields": {"cap": req["cap"], "error": msg}})
        finally:
            _PARENT.reset(parent)
        try:
            json.dumps(out.get("result"), allow_nan=False)
        except (TypeError, ValueError):
            out = {"id": req["id"], "from": self.node, "ok": False, "error": f"{req['cap']}: result is not JSON"}
        return out

    async def _announce_hub(self) -> None:
        """The signed hub announce (5.4), every 30 s and when a worker joins."""
        self._seq = max(self._seq + 1, int(self.clock()))
        self._last_announce = self.clock()
        msg = wire.sign(self.op, {"kind": "announce", "typ": "rook-announce", "v": 2, "worker_id": self.node,
                                  "name": "rook", "hub": self.hub_id, "band": self.band, "core": CORE_VERSION,
                                  "ts": int(self.clock()), "seq": self._seq, "features": []})
        msg["hubkey"] = self.grant
        try:
            await self.transport.send(self.band, msg)
        except Exception as e:
            log.warning("hub announce not sent", extra={"fields": {"error": str(e)}})

    # -- housekeeping -----------------------------------------------------------

    async def _hourly(self) -> None:
        self.dispatcher.prune_sessions()
        # Walking every chain takes seconds once the journal is large: off the
        # loop, so calls keep flowing (the store reads in pages under its lock).
        for problem in await asyncio.to_thread(self.journal.check):
            log.critical("journal chain broken", extra={"fields": problem})

    def _daily(self) -> None:
        op = _key(self.data / "keys" / "op.key", OP_KEY_DAYS * DAY, self.clock())
        if bytes(op) != bytes(self.op):
            self.op = self.dispatcher.op_key = op
        self.grant = self.dispatcher.hubkey = self._grant()
        for e in self.journal.prune():     # TODO(castling:workbench): closed projects' chains
            log.info("journal pruned", extra={"fields": {"chain": e.get("pruned"), "count": e.get("count")}})
        self.records.prune()

    def _journal(self, entry: dict) -> None:
        session = {"id": entry["session"], "project": entry.get("project")} if entry.get("session") else None
        try:
            self.journal.append(entry, session)
        except (JournalError, ValueError, TypeError, RecursionError) as e:
            # Arguments with no canonical form still get their entry, without them.
            log.error("journal entry had no canonical form", extra={"fields": {"error": str(e)}})
            self.journal.append({**entry, "args": "[no canonical form]"}, session)

    def _emit(self, event: str, payload: dict, source: str | None = None) -> None:
        log.debug("event", extra={"fields": {"event": event, "source": source}})
        self.plugins.scheduler.publish(event, payload, source)
        for fn in self.listeners:
            try:
                fn(event, payload, source)
            except Exception:
                log.exception("event listener failed")

    # -- sessions -----------------------------------------------------------------

    def _session(self, cache: dict, key, caller: Caller, caps: list) -> str:
        sid = cache.get(key)
        s = self.dispatcher.sessions.get(sid) if sid else None
        if s is None or s.ended or self.clock() > s.exp:
            sid = cache[key] = self.dispatcher.start_session(caller, caps).id
        return sid

    def _system_session(self) -> str:
        return self._session(self._component_sessions, "hub", SYSTEM, ["core.*"])

    def _door_session(self, caller: Caller) -> str:
        """A door caller's default session: every cap their identities allow."""
        return self._session(self._door_sessions, (caller.person, caller.agent), caller, ["*"])

    # -- calls from hub plugins (ctx.call, hooks, contract routing) ---------------

    async def call_as(self, who: KitCaller, cap: str, args: dict | None, uses: list | None = None,
                      target: str | None = None, contract: str | None = None) -> dict:
        """Dispatch a call a hub plugin makes. With a person or agent it runs
        as them: in the call's session, or (a job acting for its creator or
        persona, ``ctx.call_as``) in a session of their own for such calls,
        apart from their door session so its taint stays there. TODO(castling:jobs):
        a session per run, tainted by a webhook's body. Without (a hook),
        as ``system:plugin.<name>`` (a dot no plugin name has, so no plugin
        can be ``system:hub``). ``uses`` is the plugin's extra layer;
        ``target`` names the worker (``ctx.call_on``, ``ctx.call_as``), else
        the one that serves ``cap``; ``contract``: a provider-routed contract's
        call to its chosen provider, so rights match the contract's name. A
        call in the session of the hub-node call it runs inside also carries
        the layers its door put on that call (a page's caps and authors) and
        the door."""
        if who.person or who.agent:
            caller = Caller(who.person, who.agent)
            if caller.problem():
                return {"ok": False, "error": f"denied: caller: {caller.problem()}"}
            sid = who.session or self._session(self._acting_sessions, (caller.person, caller.agent),
                                               caller, ["*"])
        else:
            component = f"plugin.{who.plugin}" if who.plugin else "hub"
            caller = Caller(agent=f"system:{component}")
            if who.plugin in self.plugins.plugins:
                self.identities.add_component(component, {"grants": self.plugins.uses(who.plugin)})
            sid = self._session(self._component_sessions, component, caller, ["*"])
        target = target or self._target_for(cap)
        if target is None:
            return {"ok": False, "error": f"no single worker serves {cap}"}
        layers, door = [["uses", {"grants": list(uses)}]] if uses is not None else [], None
        parent = _PARENT.get()
        if parent and sid == parent[0]:     # inside the call it runs in: that call's layers and door too
            layers, door = [*parent[1], *layers], parent[2]
        return await self.dispatcher.dispatch(caller, Call(
            cap=cap, target=target, args=args, why=who.why or f"{who.plugin or 'hub'} plugin", session=sid,
            layers=layers, door=door, contract=contract))

    def _installed(self) -> dict:
        """{plugin: version} the band runs: the hub's plugins and every known
        worker's (the newest when they differ)."""
        out: dict = {}
        found = [(n, p.manifest.get("version")) for n, p in self.plugins.plugins.items()]
        found += [(e["plugin"], (self.manifests.get(e["hash"]) or {}).get("version"))
                  for k in self.known.values() for e in k.manifests]
        for name, version in found:
            try:
                if isinstance(version, str) and (name not in out or semver_key(version) > semver_key(out[name])):
                    out[name] = version
            except ValueError:
                continue
        return out

    def _target_for(self, cap: str) -> str | None:
        """The hub if it serves ``cap``, else the one worker that does (the
        one online, when several do)."""
        hub = self.dispatcher.workers.get(self.node)
        if hub and cap in hub.caps:
            return self.node
        serving = [w.id for w in self.dispatcher.workers.values() if w.id != self.node and (
            cap in w.caps or any(cap in (m.get("aliases") or ()) or cap in (m.get("contracts") or ())
                                 for m in w.caps.values()))]
        if len(serving) > 1:
            serving = [w for w in serving if self.known.get(w) and self.known[w].online]
        return serving[0] if len(serving) == 1 else None

    # -- the door API ---------------------------------------------------------------

    async def call(self, caller: Caller, body: dict, door: str, layers: list | None = None) -> dict:
        """``{worker, cap, args?, why, session?, appliance?, elevate?}`` from a door.
        ``layers`` narrows it further (a dashboard page's declared caps)."""
        cap, target = body.get("cap"), body.get("worker")
        args, sid = body.get("args"), body.get("session")
        if not (isinstance(cap, str) and cap and isinstance(target, str) and target):
            return {"ok": False, "error": "bad request: worker and cap are required"}
        if args is not None and not isinstance(args, dict):
            return {"ok": False, "error": "bad request: args must be an object"}
        if sid is not None and not isinstance(sid, str):
            return {"ok": False, "error": "bad request: session must be a string"}
        appliance = body.get("appliance") if isinstance(body.get("appliance"), str) else None
        why = body.get("why") if isinstance(body.get("why"), str) else ""
        reply = await self.dispatcher.dispatch(caller, Call(
            cap=cap, target=target, args=args, why=why, session=sid or self._door_session(caller),
            appliance=appliance, elevate=body.get("elevate") is True, door=door, layers=list(layers or ())))
        if reply.get("held"):
            h = self.dispatcher.holds.get(reply["hold"])
            if h is not None:
                self._emit("hold.created", {"hold": h.name, "cap": h.call.cap, "worker": h.worker.id,
                                            "person": caller.person, "agent": caller.agent, "why": h.call.why,
                                            "session": h.session.id, "reasons": h.reasons()}, "core")
        return reply

    async def start_session(self, caller: Caller, body: dict) -> dict:
        project = body.get("project")
        if project is not None and (refused := self._project_refused(caller, project)):
            return {"ok": False, "error": f"denied: project: {refused}"}
        try:
            s = self.dispatcher.start_session(caller, body.get("caps"), project, body.get("task"))
        except ValueError as e:
            return {"ok": False, "error": f"bad request: {e}"}
        return {"ok": True, "session": s.id, "exp": s.exp}

    def _project_refused(self, caller: Caller, project: Any) -> str | None:
        """A session's project opens that project's journal to its agent
        (11.4), so naming one needs access to it: an owner (who reads the
        whole journal anyway), or a caller whose rights read projects and who
        sees that project (by id or slug) on the workbench."""
        if not isinstance(project, str) or not project:
            return "a project id or slug"
        if self.identities.is_owner(caller.person):
            return None
        if not rights.check(self.identities.layers(caller), {"cap": "project.get", "risk": "read",
                                                             "effects": [], "target": "rook"})["ok"]:
            return "you may not read projects"
        try:
            projects = self.records.type("project.project")
        except KitError:
            return f"no project {project} (no projects on this hub)"
        who = KitCaller(person=caller.person, agent=caller.agent, band=self.band)
        try:
            projects.get(who, project)
            return None
        except KitError:
            pass
        if projects.list(who, where={"slug": project}, limit=1):
            return None
        return f"no project {project}"

    async def end_session(self, caller: Caller, sid: Any) -> dict:
        s = self.dispatcher.sessions.get(sid) if isinstance(sid, str) else None
        if s is None or (s.person, s.agent) != (caller.person, caller.agent):
            return {"ok": False, "error": "unknown session"}
        self.dispatcher.end_session(sid)
        for wid in self.dispatcher.lease_holders(sid):      # its elevation ends with it (7.4)
            self._spawn(self.dispatcher.dispatch(SYSTEM, Call(
                cap="core.lease.drop", target=wid, args={"session": sid}, session=self._system_session(),
                why="The session ended: drop its elevation leases.")))
        return {"ok": True}

    async def workers_view(self, caller: Caller) -> dict:
        out = [{"id": self.node, "name": "rook", "online": True, "hub": True, "core": CORE_VERSION,
                "plugins": sorted(self.plugins.plugins)}]
        for k in sorted(self.known.values(), key=lambda k: k.name):
            out.append({"id": k.id, "name": k.name, "online": k.online, "last_seen": k.last_seen,
                        "core": k.core, "version": k.version, "description": k.description,
                        "facts": k.facts, "plugins": [e["plugin"] for e in k.manifests],
                        "unavailable": k.unavailable, "status": k.status, "problem": k.problem,
                        "owner": self.enroll.owner_of(k.id)})
        return {"ok": True, "workers": out}

    async def caps_view(self, caller: Caller, worker: str | None = None) -> dict:
        out = {}
        for w in self.dispatcher.workers.values():
            if worker and worker not in (w.id, w.name):
                continue
            k = self.known.get(w.id)
            out[w.id if w.name in out else w.name] = {
                "id": w.id, "online": True if w.id == self.node else bool(k and k.online),
                "caps": {n: {f: m[f] for f in ("doc", "risk", "scope", "effects", "args", "aliases", "contracts",
                                                 "timeout")
                             if f in m} for n, m in sorted(w.caps.items())},
                "unavailable": dict(k.unavailable) if k else {}}
        return {"ok": True, "workers": out}

    async def journal_view(self, caller: Caller, filters: dict) -> dict:
        """Journal entries (11.4): owners read everything, an agent its own
        calls and its sessions' projects, a person their own."""
        if self.identities.is_owner(caller.person):
            viewer: dict = {"owner": True}
        elif caller.agent:
            projects = sorted(self.dispatcher.projects.get(caller.agent, ()))
            viewer = {"agent": caller.agent, "projects": projects}
        else:
            viewer = {"person": caller.person}
        q = {k: v for k, v in filters.items() if k in ("call", "cap", "worker", "person", "agent", "project",
                                                       "chain", "kind", "session") and isinstance(v, str)}
        try:
            for k in ("since", "until"):
                if filters.get(k) is not None:
                    q[k] = float(filters[k])
            limit = min(max(int(filters.get("limit", 50)), 1), 500)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad request: since, until and limit are numbers"}
        return {"ok": True, "entries": self.journal.query(**q, viewer=viewer, limit=limit)}

    async def holds_view(self, caller: Caller) -> dict:
        owner = self.identities.is_owner(caller.person)
        return {"ok": True, "holds": [
            {"hold": h.name, "cap": h.call.cap, "worker": h.worker.name, "why": h.call.why, "args": h.call.args,
             "person": h.caller.person, "agent": h.caller.agent, "reasons": h.reasons()}
            for h in self.dispatcher.holds.values()
            if owner or (caller.person and h.caller.person == caller.person) or h.caller == caller]}

    async def decide(self, caller: Caller, hold: str, decision: Any, present: bool = False) -> dict:
        """A person's answer to a hold: ``approve`` runs the exact call, ``refuse`` is final.
        An agent acting for a person answers as the agent, not the person.
        ``present``: the answer comes from a door where the person is there
        (the site, ``POST /holds`` with their own token, a phone card), the
        only way a hard stop is answered (10.3); the ``approval.decide`` cap
        never is, so a job or plugin acting as the person can't."""
        by = caller.agent or caller.person
        present = present and caller.agent is None and caller.person is not None
        held = self.dispatcher.holds.get(hold) if isinstance(hold, str) else None
        if decision == "approve":
            out = await self.dispatcher.release(hold, by, present)
        elif decision == "refuse":
            out = self.dispatcher.refuse(hold, by, present)
        else:
            return {"ok": False, "error": "bad request: decision is approve or refuse"}
        if not str(out.get("error", "")).startswith(("unknown hold", f"{by} may not")):
            self._emit("hold.decided", {"hold": hold, "decision": "approved" if decision == "approve"
                                        else "denied", "by": by,
                                        "person": held.caller.person if held else None}, "core")
        return out

    def door_refused(self, caller: Caller, door: str) -> str | None:
        """A door's own check, before it serves anything (views, sessions,
        calls): the error when a closed door refuses this caller."""
        closed = self.dispatcher.door_closed(caller, door)
        return f"denied: door {closed} closed" if closed else None

    async def set_door(self, caller: Caller | str, door: Any, open: Any) -> dict:
        """Close or open a door (the kill switch): an owner, or a hub
        component closing one (the security plugin). Kept across restarts."""
        by = caller if isinstance(caller, str) else caller.agent or caller.person
        if not (isinstance(door, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", door)) or not isinstance(open, bool):
            return {"ok": False, "error": "bad request: door (a name) and open (true or false)"}
        try:
            (self.dispatcher.open_door if open else self.dispatcher.close_door)(door, by)
        except PermissionError as e:
            return {"ok": False, "error": f"denied: {e}"}
        _write_private(self.data / "doors.json", json.dumps({"closed": sorted(self.dispatcher.closed_doors)}))
        log.warning("door " + ("opened" if open else "closed"), extra={"fields": {"door": door, "by": by}})
        return {"ok": True, "closed": sorted(self.dispatcher.closed_doors)}

    async def push_plugin(self, caller: Caller, body: dict, door: str) -> dict:
        """``{worker, plugin, why}``: sign the library's copy of ``plugin``
        for that worker (plugin.sig, spec 13) and send it with
        ``core.plugin.install``, which dispatch checks and journals like any
        call. Approving code for a machine is an owner's act. The worker
        installs, runs the plugin's checks and rolls back on a failure; the
        reply carries its report, or why it refused."""
        if not self.identities.is_owner(caller.person):
            return {"ok": False, "error": "denied: only an owner pushes plugins"}
        name, target = body.get("plugin"), body.get("worker")
        why = body.get("why") if isinstance(body.get("why"), str) else ""
        if not (isinstance(name, str) and name and isinstance(target, str) and target):
            return {"ok": False, "error": "bad request: worker and plugin are required"}
        w = self.dispatcher.resolve(target)
        k = self.known.get(w.id) if w else None
        if k is None:
            return {"ok": False, "error": f"unknown worker: {target}"}
        folder = self._library_folder(name)
        if folder is None:
            return {"ok": False, "error": f"{name} is not in the hub's library"}
        current = next((self.manifests.get(e["hash"]) for e in k.manifests if e["plugin"] == name), None)
        env = distribute.WorkerEnv(k.id, os=str(k.facts.get("os", "")), python=str(k.facts.get("python", "")))
        try:     # uv resolution and asset downloads block: off the loop
            plan = await asyncio.to_thread(distribute.prepare, folder, env, signer=self.op, hub=self.hub_id,
                                           current=current, cache=self.data / "assets",
                                           installed=self._installed())
        except distribute.Refused as e:
            return {"ok": False, "error": f"refused: {e}"}
        if plan.sig is None:
            return {"ok": False, "error": f"{name} has no worker part to push"}
        # Know the manifest before the worker announces it: else its caps
        # are gone from dispatch until a core.manifests fetch comes back.
        raw, issues = check(plan.folder, self._contract_specs())
        if raw is not None and not any(i.level == "error" for i in issues):
            self._save_manifest(self._cache(raw))
        blob = await asyncio.to_thread(_bundle_bytes, plan)
        try:
            desc = self.transport.offer(w.band, blob, to=w.id)
        except Exception as e:      # no band (Loopback), too large
            return {"ok": False, "error": f"bundle not offered: {e}"}
        return await self.dispatcher.dispatch(caller, Call(
            cap="core.plugin.install", target=w.id, args={"bundle": desc}, why=why,
            session=self._door_session(caller), door=door))

    def _library_folder(self, name: str) -> Path | None:
        """The library folder whose plugin.yaml names ``name``, read now: a
        new version dropped into the library is pushed without a restart."""
        lib = self.cfg.library
        for folder in sorted(lib.iterdir()) if lib and lib.is_dir() else ():
            try:
                raw = yaml.safe_load((folder / "plugin.yaml").read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(raw, dict) and raw.get("plugin") == name:
                return folder
        return None


def _read_doors(data: Path) -> list:
    """The doors closed when the hub last ran (``<data>/doors.json``)."""
    try:
        closed = json.loads((data / "doors.json").read_text(encoding="utf-8")).get("closed")
    except FileNotFoundError:
        return []
    except (OSError, ValueError, AttributeError) as e:
        # A switch someone pulled must not come undone: refuse to start blind.
        raise RuntimeError(f"doors.json unreadable ({e}); fix or remove it") from None
    return [d for d in closed if isinstance(d, str)] if isinstance(closed, list) else []


def _bundle_bytes(plan) -> bytes:
    with tempfile.TemporaryDirectory(prefix="rook-bundle-") as tmp:
        return distribute.write_bundle(plan, Path(tmp) / "bundle.tar").read_bytes()


def _facts(facts: Any) -> dict:
    """Self-reported facts (5.3): at most 1024 bytes, grant-looking keys dropped."""
    if not isinstance(facts, dict) or len(json.dumps(facts, separators=(",", ":"), default=str)) > 1024:
        return {}
    return {f: v for f, v in facts.items() if not str(f).startswith("is_") and f not in ("role", "roles", "grants")}


def _entries_hash(entries: list) -> str:
    return hashlib.sha256(wire.canonical(entries)).hexdigest()


def _errors(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "not a mapping"
    return "; ".join(str(i) for i in validate(raw) if i.level == "error")


def add_token(data: Path, caller: Caller, label: str = "", identities: Identities | None = None) -> str:
    """Mint a door token for ``caller`` and store its hash in
    ``<data>/tokens.json``. The token itself is returned once, never stored."""
    if caller.problem():
        raise ValueError(caller.problem())
    token = _secrets.token_urlsafe(32)
    path = Path(data) / "tokens.json"
    tokens = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    tokens[digest(token)] = {"person": caller.person, "agent": caller.agent, "label": label,
                             "created": int(time.time())}
    _write_private(path, json.dumps(tokens, indent=1))
    if identities is not None:
        identities.add_token_digest(digest(token), caller)
    return token
