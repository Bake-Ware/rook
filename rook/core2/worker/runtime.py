"""The 0.5 worker process: the wave-2 worker libraries assembled into one
runtime (docs/spec/core-v2.md sections 5, 6, 7, 9 and 13).

    worker = Worker(load_config("worker.yaml"))
    await worker.start()     # trust, replay cache, gate, host, plugins, bands
    ...
    await worker.stop()

What it wires together:

- **Gate** (:mod:`.security`): the trust store (the anchor plus the home
  hub's membership documents with their per-hub ceilings), the replay cache
  in ``<data>/replay``, hub keys and contact, elevation leases.
- **Host** (:mod:`.host`): core's builtin manifest (core.yaml's worker caps),
  the plugin folders the config names, and the plugins hubs installed.
  Every request goes ``Host.handle``: ticket check, ceiling, run, reply;
  elevation is a ``core.elevate`` round trip to the hub inside the call.
  A plugin failure is an error reply, never a crash.
- **Installer** (:mod:`.install`): ``core.plugin.install`` pulls the bundle
  over Drop and installs it (signature, files, checks, automatic rollback);
  ``core.plugin.rollback`` goes back a version. Installed plugins load again
  at start-up, re-checked.
- **Updater** (:mod:`.update`): ``worker.update`` pulls a hub-signed core
  release over Drop, verifies and smoke-checks it and restarts into it as a
  trial (:mod:`.boot`); the trial is confirmed by the first call from the
  home hub it serves, else dropped after ``ROOK_UPDATE_TRIAL_SECS`` and the worker
  restarts on the core it had. ``worker.update {version}`` goes back by hand.
- **Transport** (:mod:`rook.core2.transport`): one connection per band, each
  with its own announce, heartbeat and hub-loss clock (9.4). Bands come from
  the config, enrollment and ``core.trust.set`` (the last two kept in
  ``<data>/bands.json``).
  Losing the hub on a band drops its leases there (7.4).

Plugin events (``ctx.emit``) run this worker's ``on:`` hooks and go to every
hub whose ceiling grants them, as ``core.event`` batches from a bounded
queue per hub (:mod:`.events`) that waits out a hub that is away.

Its streams are listed in the hub's stream index (N28): a stream opened for
a band (``attrs.band``) is reported to that band's hub only, as
``stream.changed`` / ``stream.deleted`` events, and after hub contact the
worker sends them all again with ``stream.synced`` (what it holds now).

Plugin code's ``ctx.call`` becomes ``core.call`` on the hub, authenticated
by the ticket of the call in flight it runs inside (13); hooks have no call
in flight, so their ``ctx.call`` is refused.

The worker's own key (spec 5.8) is ``<data>/worker.key``, made on
first start (or by enrollment) and never sent anywhere: its public half goes
in every announce, and every ``core.call`` and ``core.elevate`` it sends a
hub carries a ``rook-request`` proof signed with it (13.1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import secrets
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nacl.signing import SigningKey

from rook.kit.ctx import Caller, KitError
from rook.kit.manifest import check

from .. import CORE_VERSION, wire
from ..builtin import CORE, STREAM, WORKER
from ..transport import FEATURES, Transport, TransportError, link_from
from rook.streams import Reporter, Streams

from . import boot, builtins, events
from .host import Host
from .install import Installer
from .update import TRIAL_SECS, UpdateError, Updater
from .loader import LoadError, local_os
from .security import ELEVATE_TIMEOUT, Call, Member, ReplayStore, TrustStore, WorkerGate, _write_atomic
from . import security as _security

log = logging.getLogger("rook.worker")

HEX32 = re.compile(r"[0-9a-f]{32}")
SYNC_IDS = 500                # stream ids one stream.synced lists
SUB_CALL_TIMEOUT = 300.0      # ctx.call through the hub; the parent cap's timeout ends it sooner
EVENT_TIMEOUT = 15.0          # core.event: one batch of events to a hub
PULL_TIMEOUT = 300.0          # a plugin bundle over Drop
STOP_GRACE = 10.0             # seconds in-flight calls get to finish on stop
APPROVERS = ("home", "co-owner")


# -- config -------------------------------------------------------------------

@dataclass
class Config:
    data: Path
    anchor: Path                                      # the home hub (TrustStore anchor), read-only
    name: str = field(default_factory=socket.gethostname)
    worker_id: str | None = None                      # from enrollment; else made once in <data>
    memberships: Path | None = None                   # default <data>/memberships.json
    plugins: list = field(default_factory=list)       # plugin folders, or folders of them
    bands: list = field(default_factory=list)         # transport link specs: {psk | psk_env, relay | ws_url}
    settings: dict = field(default_factory=dict)      # plugin -> {setting: value}, worker-local
    hardware: list = field(default_factory=list)      # what requires.hardware can match
    description: str | None = None                    # operator-written role, <= 280 characters
    log: dict = field(default_factory=dict)           # {level, format: text|json}
    run_as: str | None = None                         # root workers: the account normal calls run as


def load_config(path: str | Path) -> Config:
    """Read and check the YAML config. Relative paths are from its folder."""
    import yaml

    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: not a mapping")
    unknown = sorted(set(raw) - set(Config.__dataclass_fields__))
    if unknown:
        raise ValueError(f"{path}: unknown keys: {', '.join(unknown)}")
    for key in ("data", "anchor"):
        if not raw.get(key):
            raise ValueError(f"{path}: {key} is required")
    base = path.resolve().parent
    for key in ("data", "anchor", "memberships"):
        if raw.get(key):
            raw[key] = base / raw[key]
    if not (isinstance(raw.get("plugins") or [], list) and all(isinstance(p, str) for p in raw.get("plugins") or ())):
        raise ValueError("plugins must be a list of folders")
    raw["plugins"] = [base / p for p in raw.get("plugins") or ()]
    cfg = Config(**{k: v for k, v in raw.items() if v is not None})
    check_config(cfg)
    return cfg


def check_config(cfg: Config) -> None:
    if not (isinstance(cfg.name, str) and cfg.name and cfg.name != "rook"):
        raise ValueError("name must be a non-empty string other than 'rook' (the hub's)")
    if cfg.worker_id is not None and not (isinstance(cfg.worker_id, str) and HEX32.fullmatch(cfg.worker_id)):
        raise ValueError("worker_id must be 32 hex")
    if not isinstance(cfg.bands, list) or not all(isinstance(b, dict) for b in cfg.bands):
        raise ValueError("bands must be a list of {psk | psk_env, relay | ws_url}")
    for name, kind in (("settings", dict), ("hardware", list), ("log", dict)):
        if not isinstance(getattr(cfg, name), kind):
            raise ValueError(f"{name} must be a {kind.__name__}")
    if cfg.description is not None and len(str(cfg.description)) > 280:
        raise ValueError("description is at most 280 characters")
    if cfg.run_as is not None:
        if not (isinstance(cfg.run_as, str) and cfg.run_as):
            raise ValueError("run_as must be an account name")
        try:
            import pwd
            pwd.getpwnam(cfg.run_as)
        except ImportError:
            raise ValueError("run_as is for Linux workers") from None
        except KeyError:
            raise ValueError(f"run_as: no account {cfg.run_as!r} on this machine") from None


# -- logging --------------------------------------------------------------------
# TODO(castling:hub-runtime): the hub has the same formatter; share one module.

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


def _f(**fields: Any) -> dict:
    return {"fields": fields}


# -- the worker -----------------------------------------------------------------

class _Gate(WorkerGate):
    """The gate, telling the worker about every call it lets through: that
    is hub contact on the band, and the call's ticket authenticates the
    plugin's ``core.call`` sub-calls while it runs."""

    def __init__(self, worker: "Worker", *args: Any, **kw: Any):
        super().__init__(*args, **kw)
        self.worker = worker

    async def execute(self, call: Call, action, now: int | None = None) -> dict:
        self.worker._contact(call.band)
        self.worker.inflight.append(call)
        started, out = time.time(), {"ok": False, "error": "worker error"}
        try:
            out = await super().execute(call, action, now)
            if out.get("ok") and self.worker.on_trial:
                self.worker._served(call.band)
            return out
        except Exception as e:             # Host.handle turns it into the error reply
            out = {"ok": False, "error": getattr(e, "message", None) or str(e) or type(e).__name__}
            raise
        finally:
            self.worker.inflight.remove(call)
            if call.ticket["cap"] not in builtins.UNLOGGED:
                self.worker.ran.add(builtins.entry(call.context(), call.ticket["cap"], started, out))


class Worker:
    """One per process. ``make_link(spec)`` turns a band spec (the config's
    ``bands`` entries, ``core.trust.set``'s ``connect``) into a transport
    link; tests pass loopback links. ``transport_opts`` go to
    :class:`~rook.core2.transport.Transport` (``every``, ``lost_after``...).
    ``extra_builtins`` are more ``builtin: true`` manifests this process
    implements, ``[(manifest data, {cap: handler})]``: a service that runs
    its own worker (the voice service, services/voice/band.py)."""

    def __init__(self, cfg: Config, *, make_link: Callable[[dict], Any] = link_from,
                 transport_opts: dict | None = None, extra_builtins=(), config_path: Path | None = None):
        check_config(cfg)
        self.cfg = cfg
        self.config_path = config_path        # for the smoke check of a core update
        self.trial_secs = float(os.environ.get("ROOK_UPDATE_TRIAL_SECS") or TRIAL_SECS)
        self._trial_timer: asyncio.TimerHandle | None = None
        self.on_trial = False                 # running a core update that hasn't proved itself yet
        self.extra_builtins = list(extra_builtins)
        self.data = Path(cfg.data)
        self.make_link = make_link
        self.transport_opts = dict(transport_opts or {})
        self.inflight: list[Call] = []
        # worker.restart's last step; tests replace it, None where a process can't exec itself.
        self.reexec: Callable[[], None] | None = builtins.reexec
        self.hub_nodes: dict[tuple[str, str], str] = {}     # (hub_id, band) -> the hub's node id there
        self.outboxes: dict[tuple[str, str], events.Outbox] = {}   # (hub_id, band) -> events on their way
        self.started = False
        self._tasks: set[asyncio.Task] = set()
        self._settings_lock = threading.Lock()

    # -- start and stop ----------------------------------------------------------

    async def start(self) -> None:
        """Open the stores, load plugins, connect every band. Raises on a bad
        anchor or data folder (closing what it opened); a plugin that won't
        load is logged and skipped."""
        self.loop = asyncio.get_running_loop()
        self.host = self.transport = None
        try:
            await self._open()
        except BaseException:
            await self._close()
            raise
        log.info("worker started", extra=_f(worker=self.worker_id, name=self.cfg.name,
                                             bands=len(self.transport.bands), plugins=len(self.host.plugins)))

    async def _open(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        _security.RUN_AS = self.cfg.run_as          # who a root worker's normal calls run as
        self.worker_id = self._worker_id()
        self.key = worker_key(self.data / KEY_FILE)
        self.trust = TrustStore(self.cfg.anchor, self.cfg.memberships or self.data / "memberships.json",
                                self.worker_id)
        for why in self.trust.skipped:
            log.warning("membership not loaded", extra=_f(reason=why))
        if not self.trust.anchor_protected:
            log.warning("anchor is writable by the worker", extra=_f(anchor=str(self.trust.anchor_path)))
        self.gate = _Gate(self, self.worker_id, self.cfg.name, self.trust, ReplayStore(self.data / "replay"),
                          {}, ask=self._elevate)
        self.streams = Streams(host=self.cfg.name)
        self.stream_reporter = Reporter(self.streams, self._stream_changed)
        self.ran = builtins.RanLog(self.data / "ran.jsonl")
        self.host = Host(hardware=self.cfg.hardware, dispatch=self._dispatch,
                         store_setting=self._store_setting, emit=self._emit,
                         data_root=self.data / "plugin-data", gate=self.gate, streams=self.streams)
        own = builtins.handlers(self, self.streams, self.ran,
                                restart=self._restart if self.reexec is not None else None)
        own["worker.update"] = self._core_update
        for path, caps in ((CORE, {"core.trust.set": self._trust_set, "core.trust.get": self._trust_get,
                                   "core.plugin.install": self._plugin_install,
                                   "core.plugin.rollback": self._plugin_rollback}),
                           (WORKER, own), (STREAM, own)):
            data, issues = check(path)
            if data is None or any(i.level == "error" for i in issues):
                raise RuntimeError(f"{path.name} is invalid: {[str(i) for i in issues]}")
            self.host.add_builtin(data, {c: f for c, f in caps.items() if c.startswith(data["plugin"] + ".")})
        for data, handlers in self.extra_builtins:
            self.host.add_builtin(data, handlers)
        self.installer = Installer(self.data / "plugins", worker_id=self.worker_id, host=self.host,
                                   keys=self._approver_keys, approves=lambda hub: bool(self._approver_bands(hub)))
        self.updater = Updater(boot.root_of(self.data), keys=self._approver_keys, running=boot.running(),
                               config=self.config_path)
        self._check_release()
        for name, values in self._local_settings().items():
            await self.host.set_settings(name, local=values)
        await self._load_plugins()
        self.transport = Transport(self.worker_id, [], hello=self._announce, beat=self._heartbeat,
                                   on_lost=self._lost, on_contact=self._regained, **self.transport_opts)
        for spec in [*self.cfg.bands, *self._joined().values()]:
            try:
                await self.transport.add(self.make_link(spec))
            except (ValueError, KeyError, TypeError) as e:     # a duplicate band, a missing PSK
                log.error("band not joined", extra=_f(error=str(e)))
        self.started = True
        await self.transport.start(self._on_message)

    async def stop(self, grace: float = STOP_GRACE) -> None:
        """Stop taking calls, give in-flight ones ``grace`` seconds, then
        close the bands and the plugins (their stop hooks run)."""
        if not self.started:
            return
        self.started = False
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=grace)
        await self._close()
        log.info("worker stopped", extra=_f(worker=self.worker_id))

    async def _close(self) -> None:
        self.started = False
        if self._trial_timer is not None:
            self._trial_timer.cancel()
            self._trial_timer = None
        while self._tasks:              # late calls, and announces they spawned
            late = set(self._tasks)
            for t in late:
                t.cancel()
            await asyncio.gather(*late, return_exceptions=True)
        if getattr(self, "stream_reporter", None) is not None:
            self.stream_reporter.close()
        outboxes, self.outboxes = list(self.outboxes.values()), {}
        await asyncio.gather(*(o.stop() for o in outboxes))
        if self.transport is not None:
            await self.transport.close()
        if self.host is not None:
            await self.host.close()

    def _restart(self) -> None:
        """worker.restart: stop cleanly (in-flight calls get their grace),
        then re-exec on the same command line."""
        async def go():
            try:
                await self.stop()
            finally:                    # a stop that fails must not leave a worker that serves nothing
                self.reexec()
        self._restarting = self.loop.create_task(go())

    def _worker_id(self) -> str:
        path = self.data / "worker_id"
        try:
            held = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            held = None
        wid = self.cfg.worker_id or (held if held and HEX32.fullmatch(held) else secrets.token_hex(16))
        if wid != held:
            _write_atomic(path, wid + "\n")
        return wid

    async def _load_plugins(self) -> None:
        """The config's plugin folders, then the hub-installed ones (which
        win: a hub signed them for this machine)."""
        folders = []
        for p in self.cfg.plugins:
            p = Path(p)
            if (p / "plugin.yaml").is_file():
                folders.append(p)
            elif p.is_dir():
                folders += sorted(q for q in p.iterdir() if (q / "plugin.yaml").is_file())
            else:
                log.error("plugin folder not found", extra=_f(folder=str(p)))
        installed, skipped = self.installer.installed()
        for name, why in skipped.items():
            log.error("installed plugin not loaded", extra=_f(plugin=name, reason=why))
        for folder in [*folders, *installed.values()]:
            try:
                await self.host.load(folder)
            except LoadError as e:
                log.error("plugin not loaded", extra=_f(folder=str(folder), error=str(e)))

    # -- the band ----------------------------------------------------------------

    async def _on_message(self, band: str, msg: dict) -> None:
        """Everything the transport hands up. Never blocks it: each request
        runs as its own task."""
        if isinstance(msg.get("cap"), str):
            if msg.get("target") == self.worker_id and self.started:
                self._spawn(self._serve(band, msg))
        elif msg.get("kind") == "announce" and msg.get("typ") == "rook-announce":
            if self.gate.hub_announce(band, msg):
                self.hub_nodes[(msg["hub"], band)] = msg["worker_id"]
                self._contact(band)

    async def _serve(self, band: str, msg: dict) -> None:
        try:
            out = await self.host.handle(msg, band)
        except Exception as e:             # Host.handle turns plugin failures into replies; this is a bug
            log.exception("request failed", extra=_f(cap=msg.get("cap"), band=band[:8]))
            out = {"id": msg.get("id") if isinstance(msg.get("id"), str) else "", "from": self.worker_id,
                   "ok": False, "error": f"worker error: {type(e).__name__}"}
        if out is None:
            return
        level = logging.INFO if out["ok"] else logging.WARNING
        log.log(level, "call", extra=_f(cap=msg.get("cap"), band=band[:8], ok=out["ok"],
                                        **({"error": _loggable(out["error"])} if not out["ok"] else {}),
                                        **({"elevated": out["elevated"]} if out.get("elevated") else {})))
        try:
            await self.transport.send(band, out)
        except TransportError as e:
            log.warning("reply not sent", extra=_f(cap=msg.get("cap"), band=band[:8], error=str(e)))

    def _contact(self, band: str) -> None:
        try:
            self.transport.contact(band)
        except TransportError:             # left the band meanwhile
            pass

    def _lost(self, band: str) -> None:
        """No hub contact on ``band`` for a while: its leases end now (7.4)."""
        dropped = sum(self.gate.drop_leases(m.hub, band, {})["dropped"] for m in self.trust.members(band))
        log.warning("hub lost", extra=_f(band=band[:8], leases_dropped=dropped))

    def _regained(self, band: str) -> None:
        """First hub contact on ``band``, or contact after losing it: the hub
        may have (re)started since this worker last announced, so announce."""
        log.info("hub contact", extra=_f(band=band[:8]))
        self._spawn(self._announce_on(band))
        for (_, b), outbox in self.outboxes.items():
            if b == band:
                outbox.kick()
        self._sync_streams(band)

    def _stream_changed(self, change: str, row: dict) -> None:
        """The stream index (N28): a stream opened for a band goes to that
        band's hub; one opened for none stays off every index."""
        band = row["attrs"].get("band")
        if isinstance(band, str) and band:
            self._queue_event(f"stream.{change}", {"id": row["id"]} if change == "deleted" else row,
                              "stream", band=band)

    def _sync_streams(self, band: str) -> None:
        """Every stream of ``band`` again, then which ones there are: the
        hub may have missed events while it was away, or this worker restarted."""
        rows = [r for r in self.stream_reporter.rows() if r["attrs"].get("band") == band]
        for r in rows:
            self._queue_event("stream.changed", r, "stream", band=band)
        self._queue_event("stream.synced", {"ids": [r["id"] for r in rows[:SYNC_IDS]],
                                            "complete": len(rows) <= SYNC_IDS}, "stream", band=band)

    async def _announce_on(self, band: str) -> None:
        try:
            await self.transport.send(band, await self._announce(band))
        except TransportError as e:
            log.debug("announce not sent", extra=_f(band=band[:8], error=str(e)))

    def _view(self, band: str) -> tuple[Member | None, dict]:
        """The band's membership and the ceiling its announce is filtered by
        (9.4). A band normally has one hub; with several, their grants add up."""
        members = self.trust.members(band)
        if len(members) == 1:
            return members[0], members[0].ceiling
        return None, {"grants": [g for m in members for g in m.ceiling.get("grants") or ()]}

    async def _announce(self, band: str) -> dict:
        """The full announce (5.3) for one band's filtered view."""
        member, ceiling = self._view(band)
        msg = {"kind": "announce", "worker_id": self.worker_id, "name": self.cfg.name, "core": CORE_VERSION,
               "key": wire.pub_text(self.key), "version": _version(), "facts": _facts(), **await self.host.announce(ceiling),
               "ceiling": ceiling, "features": FEATURES}
        if member is not None:
            msg["role"] = member.role
        if self.cfg.description:
            msg["description"] = str(self.cfg.description)
        return msg

    async def _heartbeat(self, band: str) -> dict:
        """The heartbeat (5.5); also the clock that expires leases."""
        self.gate.tick()
        view = await self.host.announce(self._view(band)[1])
        msg = {"kind": "heartbeat", "worker_id": self.worker_id, "core": CORE_VERSION, "ts": int(time.time()),
               "manifests_hash": view["manifests_hash"]}
        if view.get("status"):
            msg["status"] = view["status"]
        return msg

    async def reannounce(self) -> None:
        """Send a fresh announce on every band (after a plugin or trust change)."""
        for band in self.transport.bands:
            await self._announce_on(band)

    async def _hub_request(self, band: str, hub: str, cap: str, args: dict, timeout: float) -> dict:
        node = self.hub_nodes.get((hub, band))
        if node is None:
            raise TransportError(f"hub {hub} not heard on band {band[:8]} yet")
        req = {"id": secrets.token_hex(16), "cap": cap, "target": node, "args": args}
        if cap in ("core.call", "core.elevate"):        # 13.1: the hub checks it is this worker's
            req["proof"] = wire.request_proof(self.key, req, worker=self.worker_id, hub=hub, band=band,
                                              nonce=secrets.token_hex(16), ts=int(time.time()))
        return await self.transport.request(band, req, timeout)

    async def _elevate(self, band: str, args: dict) -> dict:
        """The gate's ``ask``: ``core.elevate`` to the hub that minted the ticket."""
        return await self._hub_request(band, args["ticket"]["hub"], "core.elevate", args, ELEVATE_TIMEOUT)

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # -- what the host needs from the band ----------------------------------------

    def _parent(self, caller: Caller, cap: str | None = None) -> Call | None:
        """The call in flight ``caller`` is running inside: same band, person,
        agent and session, and a cap of the calling plugin (or ``cap``)."""
        for call in reversed(self.inflight):
            t = call.ticket
            if (call.band, t.get("person"), t.get("agent"), t["session"]) != \
                    (caller.band, caller.person, caller.agent, caller.session):
                continue
            if (t["cap"] == cap) if cap else t["cap"].startswith(f"{caller.plugin}."):
                return call
        return None

    async def _dispatch(self, cap: str, args: dict, caller: Caller) -> Any:
        """``ctx.call``: ``core.call`` on the hub with the parent call's ticket
        (13). The hub dispatches it with the caller's rights and the plugin's
        ``uses``, to this worker when it serves the cap, else to the hub.
        TODO(castling:kit): let ctx.call name a target (another worker)."""
        parent = self._parent(caller)
        if parent is None:
            raise KitError(f"ctx.call {cap}: only a cap running for a hub call can call another cap")
        target = self.worker_id if cap in self.gate.caps else "rook"
        try:
            out = await self._hub_request(
                parent.band, parent.ticket["hub"], "core.call",
                {"ticket": parent.ticket, "cap": cap, "target": target, "args": args,
                 "why": caller.why or parent.ticket["why"]}, SUB_CALL_TIMEOUT)
        except (TransportError, asyncio.TimeoutError) as e:
            raise KitError(f"ctx.call {cap}: hub unreachable: {e or 'timed out'}", retry=True) from None
        if not out.get("ok"):
            raise KitError(str(out.get("error") or f"{cap} failed"), retry=bool(out.get("retry")))
        return out.get("result")

    def _emit(self, event: str, payload: dict, plugin: str | None = None) -> None:
        """A plugin's event (``ctx.emit``, from any thread): this worker's
        ``on:`` hooks, and every hub whose ceiling grants it (``core.event``)."""
        self.host.publish(event, payload)
        if plugin is not None and self.started:
            try:
                self.loop.call_soon_threadsafe(self._queue_event, event, payload, plugin)
            except RuntimeError:            # the loop is closed: stopping
                pass

    def _queue_event(self, event: str, payload: dict, plugin: str, band: str | None = None) -> None:
        """Queue ``event`` for every hub whose ceiling grants it (only the
        hubs on ``band`` when given)."""
        if not self.started or self.transport is None:
            return
        try:
            size = len(json.dumps(payload, allow_nan=False))     # ASCII: characters are bytes
        except (TypeError, ValueError):
            log.warning("event not sent: payload is not JSON", extra=_f(event=event, plugin=plugin))
            return
        if size > events.MAX_EVENT:
            log.warning("event not sent: too large", extra=_f(event=event, plugin=plugin, bytes=size))
            return
        ev = events.make(event, plugin, payload, size)
        for b in self.transport.bands if band is None else [band]:
            for m in self.trust.members(b):
                if self.host.sends(plugin, event, m.ceiling, self.cfg.name):
                    self._outbox(m.hub, b).put(ev)

    def _outbox(self, hub: str, band: str) -> events.Outbox:
        box = self.outboxes.get((hub, band))
        if box is None:
            async def send(batch: list, dropped: int) -> None:
                await self._send_events(hub, band, batch, dropped)
            box = self.outboxes[(hub, band)] = events.Outbox(f"{hub}/{band[:8]}", send)
            box.start()
        return box

    async def _send_events(self, hub: str, band: str, batch: list, dropped: int) -> None:
        """One batch to one hub; raises (so the outbox keeps it and tries
        again) unless the hub answered. A batch the hub refuses for good is
        logged and let go: sending it again would only be refused again."""
        if self.transport.lost(band):       # don't send into the void: hub contact kicks the outbox
            raise TransportError(f"hub lost on band {band[:8]}")
        args = {"worker_id": self.worker_id, "events": batch, **({"dropped": dropped} if dropped else {})}
        out = await self._hub_request(band, hub, "core.event", args, EVENT_TIMEOUT)
        if not out.get("ok"):
            if out.get("retry"):
                raise TransportError(str(out.get("error")))
            log.warning("events refused by the hub", extra=_f(band=band[:8], error=_loggable(out.get("error"))))

    def _store_setting(self, plugin: str, key: str, value: Any, secret: bool) -> None:
        """``ctx.settings.set`` for a worker-local value: kept in
        ``<data>/settings.json`` (secrets belong in the hub's vault)."""
        p = self.host.plugins.get(plugin)
        decl = ((p.manifest.get("settings") or {}).get(key) or {}) if p else {}
        if secret or decl.get("where", "worker") != "worker":
            raise KitError(f"setting {key} is kept by the hub, not this worker")
        with self._settings_lock:
            stored = self._stored_settings()
            stored.setdefault(plugin, {})[key] = value
            _write_atomic(self.data / "settings.json", json.dumps(stored, indent=1, sort_keys=True))
        values = {**(self.cfg.settings.get(plugin) or {}), **stored[plugin]}
        self.loop.call_soon_threadsafe(lambda: self._spawn(self.host.set_settings(plugin, local=values)))

    def _stored_settings(self) -> dict:
        try:
            got = json.loads((self.data / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in got.items() if isinstance(v, dict)} if isinstance(got, dict) else {}

    def _local_settings(self) -> dict:
        stored = self._stored_settings()
        return {name: {**(self.cfg.settings.get(name) or {}), **stored.get(name, {})}
                for name in {*self.cfg.settings, *stored}}

    # -- who may approve plugins here (spec 13) -------------------------------------

    def _approver_bands(self, hub: Any) -> list[str]:
        """Bands where ``hub`` has role home or co-owner."""
        pairs = {(self.trust.home.hub, self.trust.home.band), *self.trust.docs}
        return sorted(b for h, b in pairs if h == hub and (m := self.trust.member(h, b)) and m.role in APPROVERS)

    def _approver_keys(self, hub: str) -> dict[str, str]:
        return {kid: pub for band in self._approver_bands(hub) for kid, pub in self.gate.op_keys(hub, band).items()}

    # -- core caps the worker answers itself (13) -------------------------------------

    async def _trust_get(self, ctx) -> dict:
        return self.trust.describe()

    async def _trust_set(self, ctx, membership: dict, connect: dict | None = None) -> dict:
        """Accept a membership (only the home hub gets here: the ticket check
        refuses ``core.trust.set`` from anyone else), then join, keep or
        leave its band."""
        ok, reason = self.gate.trust_set(membership)
        if not ok:
            raise KitError(f"membership refused: {reason}")
        band, role = membership["band"], membership["role"]
        joined = self._joined()
        if role == "removed":
            box = self.outboxes.pop((membership["hub"], band), None)
            if box is not None:
                await box.stop()
            if band in joined and not self.trust.members(band):
                del joined[band]
                self._save_joined(joined)
                await self.transport.remove(band)
                log.info("left band", extra=_f(band=band[:8], hub=membership["hub"]))
        elif connect and band not in self.transport.bands:
            try:
                link = self.make_link(connect)
            except (ValueError, KeyError, TypeError) as e:
                raise KitError(f"membership kept, but the band can't be joined: {e}") from None
            if link.band != band:
                raise KitError("membership kept, but connect is for another band (its PSK)")
            joined[band] = {k: connect[k] for k in ("psk", "psk_env", "relay", "ws_url") if connect.get(k)}
            self._save_joined(joined)
            await self.transport.add(link)
            log.info("joined band", extra=_f(band=band[:8], hub=membership["hub"], role=role))
        self._spawn(self.reannounce())
        return {"ok": True}

    def _joined(self) -> dict[str, dict]:
        """Bands joined at enrollment and through ``core.trust.set``: band ->
        link spec. The file holds band PSKs, so it is private to the worker's
        account."""
        try:
            got = json.loads((self.data / "bands.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {b: s for b, s in got.items() if isinstance(s, dict)} if isinstance(got, dict) else {}

    def _save_joined(self, joined: dict) -> None:
        _write_atomic(self.data / "bands.json", json.dumps(joined, indent=1, sort_keys=True))

    async def _plugin_install(self, ctx, bundle: dict) -> dict:
        """Pull the bundle over Drop from the band the call came on and hand
        it to the installer, which trusts nothing but plugin.sig."""
        band = ctx.caller.band
        parent = self._parent(ctx.caller, "core.plugin.install")
        source = self.hub_nodes.get((parent.ticket["hub"], band)) if parent else None
        try:
            blob = await self.transport.pull(band, bundle, source, PULL_TIMEOUT)
        except TransportError as e:
            raise KitError(f"bundle not received: {e}", retry=True) from None
        incoming = self.data / "incoming"
        incoming.mkdir(exist_ok=True, mode=0o700)
        path = incoming / f"{uuid.uuid4().hex}.tar"
        try:
            await asyncio.to_thread(path.write_bytes, blob)
            report = await self.installer.receive(path)
        finally:
            path.unlink(missing_ok=True)
        log.info("plugin install", extra=_f(**{k: report.get(k) for k in
                                               ("ok", "plugin", "version", "previous", "rolled_back", "reason")}))
        if not report["ok"]:
            back = " (rolled back)" if report.get("rolled_back") else ""
            raise KitError(f"install failed: {report['reason']}{back}")
        self._spawn(self.reannounce())
        return report

    async def _plugin_rollback(self, ctx, plugin: str) -> dict:
        report = await self.installer.rollback(plugin)
        if not report["ok"]:
            raise KitError(report["reason"])
        log.info("plugin rolled back", extra=_f(plugin=plugin, version=report["version"]))
        self._spawn(self.reannounce())
        return report

    # -- core updates (docs/castling-updates.md) ---------------------------------------

    def _check_release(self) -> None:
        """At start: a release whose hub no longer approves code here goes
        (back to the installed core); a trial gets its clock."""
        up = self.updater
        if up.running is None:
            return
        try:
            folder = boot.running_folder()
            hub = json.loads((folder / boot.SIG_FILE).read_text(encoding="utf-8")).get("hub")
        except (OSError, ValueError, AttributeError, TypeError):
            hub = None
        if not self._approver_bands(hub):
            up.refuse_running(f"hub {hub} no longer approves code on this machine")
            self._restart_soon()
        elif up.in_trial():
            self.on_trial = True
            self._trial_timer = self.loop.call_later(self.trial_secs, self._trial_expired)
            log.info("core update on trial", extra=_f(version=up.running, seconds=self.trial_secs))

    def _served(self, band: str) -> None:
        """A call from the home hub went all the way through this core
        (ticket, gate, host, plugin): the trial has proved itself."""
        if band != self.trust.home.band or not self.on_trial:
            return
        self.on_trial = False
        if self._trial_timer is not None:
            self._trial_timer.cancel()
            self._trial_timer = None
        self.updater.confirm()

    def _trial_expired(self) -> None:
        self._trial_timer = None
        if self.on_trial and self.updater.in_trial():
            self.on_trial = False
            self.updater.abandon(f"no call from the home hub served within {self.trial_secs:g}s")
            self._restart_soon()

    def _restart_soon(self) -> None:
        if self.reexec is not None:
            self.loop.call_later(builtins.RESTART_DELAY, self._restart)

    def _restartable(self) -> None:
        if self.reexec is None:
            raise KitError("this worker can't restart itself: its core updates come with its app")
        busy = self.streams.list(open_only=True)
        if busy:
            raise KitError(f"busy: {len(busy)} terminal or stream(s) open here would end; "
                           "close them, or update this worker later", retry=True)

    async def _core_update(self, ctx, bundle: dict | None = None, version: str | None = None) -> dict:
        """worker.update: with ``bundle``, pull the release over Drop from the
        hub that sent the call, verify, unpack, smoke-check, then restart into
        it on trial; with ``version``, restart on a core this worker keeps."""
        if (bundle is None) == (version is None):
            raise KitError("bad args: bundle (a release from update.rollout) or version (a kept core), one of them")
        if version is not None:
            self._restartable()
            try:
                report = self.updater.switch(version)
            except UpdateError as e:
                raise KitError(str(e)) from None
            self._restart_soon()
            return report
        if os.name == "nt":
            # execv starts a new process there and the old one exits: whatever runs the
            # worker would see it end. Not tried yet (docs/castling-updates.md).
            raise KitError("core updates over the band aren't supported on Windows yet")
        self._restartable()
        band = ctx.caller.band
        parent = self._parent(ctx.caller, "worker.update")
        source = self.hub_nodes.get((parent.ticket["hub"], band)) if parent else None
        try:
            blob = await self.transport.pull(band, bundle, source, PULL_TIMEOUT)
        except TransportError as e:
            raise KitError(f"release not received: {e}", retry=True) from None
        incoming = self.data / "incoming"
        incoming.mkdir(exist_ok=True, mode=0o700)
        path = incoming / f"{uuid.uuid4().hex}.tar.gz"
        try:
            await asyncio.to_thread(path.write_bytes, blob)
            report = await self.updater.receive(path)
        except UpdateError as e:
            log.warning("core update refused", extra=_f(error=str(e)))
            raise KitError(f"update refused: {e}") from None
        finally:
            path.unlink(missing_ok=True)
        self._restart_soon()
        return report


# Errors whose text the worker writes itself. Anything else (bad args, a
# plugin's message) can quote argument values, and the hub substitutes
# secrets into args: only its first part goes to the local log.
_OWN_ERRORS = ("denied by worker: ", "unknown capability: ", "elevation: ", "unavailable: ", "busy: ")


def _loggable(error: Any) -> str:
    error = str(error)
    return error if error.startswith(_OWN_ERRORS) else error.partition(": ")[0]


KEY_FILE = "worker.key"


def worker_key(path: Path) -> SigningKey:
    """The worker's own Ed25519 key (5.8): the 32-byte seed in hex, private
    to the worker's account; made the first time (a 2.0 worker upgrading
    gets its key here, and the hub pins it at its next announce)."""
    try:
        seed = bytes.fromhex(path.read_text(encoding="ascii").strip())
        if len(seed) == 32:
            return SigningKey(seed)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, UnicodeDecodeError) as e:
        # Never replace a key that is there but unreadable: the hub pinned it.
        raise RuntimeError(f"{path}: not a worker key ({e}); move it aside and enroll again") from None
    else:
        raise RuntimeError(f"{path}: not a worker key; move it aside and enroll again")
    sk = SigningKey.generate()
    _write_atomic(path, bytes(sk).hex() + "\n")
    log.info("worker key made", extra=_f(key=wire.key_id(wire.pub_text(sk))))
    return sk


def _version() -> str:
    """The core release this process runs, else the installed package's version."""
    running = boot.running()
    if running:
        return running
    try:
        from importlib.metadata import version
        return version("rook")
    except Exception:
        return "unknown"


def _facts() -> dict:
    return {"os": local_os(), "arch": platform.machine().lower(), "cpus": os.cpu_count() or 1,
            "python": "{}.{}".format(*sys.version_info[:2])}
