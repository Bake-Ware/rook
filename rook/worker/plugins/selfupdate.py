"""worker.* — self-management: status, restart, reconfigure, update.

Lets the band migrate or upgrade a worker WITHOUT the curl|bash self-kill
problem. The trick is the same one the installer uses: never tear down the old
worker from inside the process being torn down. Here the worker hands its own
restart to the service manager (``systemctl --user restart`` / runit), which
owns the lifecycle independently; on hosts with no service manager it re-execs
in place. The restart is scheduled *after* the reply is sent, so the caller
gets an ack before the worker drops.

Capabilities:
    worker.status                         — pid / argv / pyz / supervisor
    worker.restart                        — clean restart, same config
    worker.reconfigure(hub?, psk?, name?) — repoint at a new band, then restart
    worker.update(manifest?, url?, hub?, psk?, name?) — install a signed bundle, then restart
    worker.deauth(payload)                — signed (deauth v2) remove/ban from band

Hub/PSK changes need a hub-signed order (a verified call ticket, see
``rook.worker.authz_guard``); bundle installs need a signed manifest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from ...core.paths import legacy_state_path, worker_install_dir, worker_state_dir
from ...core.db import atomic_write_json, atomic_write_text
from ...core.pluginkit import RunTimeout, is_termux, is_windows, run
from ..plugin import ANDROID_APP_REPLACED, Plugin, capability, place

log = logging.getLogger("rook.worker.plugins.selfupdate")

_HOME = Path(os.path.expanduser("~"))
# The bundle lives where the installer and the service manager put it, which
# is always under the home dir; flags and identity live in the state dir
# (ROOK_WORKER_HOME, else the same directory).
_WORKER_DIR = worker_install_dir()
_STATE_DIR = worker_state_dir()
_PYZ = _WORKER_DIR / "band-worker.pyz"
_PREV = _WORKER_DIR / "band-worker.pyz.prev"     # previous bundle, for rollback
_STATE = _STATE_DIR / "update_state.json"        # in-flight update tracking
# Outcome of the last update (confirmed / rolled back, and why). A separate
# file because builds that predate it delete update_state.json after a
# rollback; this survives them, so a later build can still report it.
_RESULT = _STATE_DIR / "update_result.json"
_HOLD = _STATE_DIR / "hold"                       # presence pins this node (no auto-update)
_BANNED = _STATE_DIR / "banned"                   # presence = deauthed; refuse to rejoin
_WORKER_ID_FILE = _STATE_DIR / "worker_id"        # stable identity (see core.stable_worker_id)
_UNIT = _HOME / ".config" / "systemd" / "user" / "rook-band-worker.service"
_SVC = "rook-band-worker"
_WIN_TASK = "RookBandWorker"      # matches PS_BOOTSTRAP's Register-ScheduledTask name

# OTA convergence tuning.
_POLL_SECS = float(os.environ.get("ROOK_UPDATE_POLL", "300"))  # manifest check interval
_HEALTH_SECS = 60.0        # alive-only check, used when the band gate is not enforced
_MAX_BOOT_ATTEMPTS = 3     # boots into a new build without health before rolling back
# Boots of any kind (crashes, or restarts by a run that had stayed up, such as
# a settings restart) before a build still unverified is treated as a crash
# loop: each boot opens a fresh gate window, so this bounds the total.
_MAX_BOOTS = 8
# Band-reachability gate (see _band_gate): a new build that cannot complete a
# band round trip within ROOK_UPDATE_HEALTH_SECS is rolled back at once.
_GATE_SECS = 180.0
_PROBE_SECS = 15.0         # between round-trip probes while the gate is open
_PROBE_TIMEOUT = 10.0      # one probe's reply wait
_PROBE_CAP = "caps.describe"   # every rook node answers it (always-open tier)
# The gate is enforced only when the old build heard the band this recently
# before it swapped: if the band was already unreachable, rolling back can't
# help, and every worker would otherwise drop a good build during an outage.
_CONTACT_WINDOW = 600.0
# A rolled-back build is not re-applied by the poll loop or a hub push for
# this long (worker.check(force=true) still installs it). Without it the hub's
# push loop would re-push the same build every few minutes.
_RETRY_BAD_SECS = 24 * 3600.0
# After a rollback the restored build checks the band too. If it can't reach
# it either, the band (hub outage, sleep, network) was the likely cause, not
# the new build: the result is "inconclusive" and the retry wait is short.
_RETRY_INCONCLUSIVE_SECS = 3600.0
# A gate loop tick that comes back this much later than planned means the
# host was suspended; the deadline moves out by the gap.
_SUSPEND_GAP_SECS = 30.0


def _now() -> float:
    """The gate's clock (monotonic; a seam for tests)."""
    return time.monotonic()


def _boot_id() -> str:
    """This process's id (rook.worker.core.BOOT_ID): a plugin reload keeps
    it, a restart or re-exec gets a new one."""
    from ..core import BOOT_ID
    return BOOT_ID
_UA = "rook-worker"        # Cloudflare blocks the default urllib User-Agent


def _gate_secs() -> float:
    """The band gate's window (ROOK_UPDATE_HEALTH_SECS; 0 turns the gate off
    and falls back to the alive-only check)."""
    raw = os.environ.get("ROOK_UPDATE_HEALTH_SECS", "").strip()
    if not raw:
        return _GATE_SECS
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("ignoring ROOK_UPDATE_HEALTH_SECS=%r (not a number)", raw)
        return _GATE_SECS


def _supervisor() -> str:
    if _UNIT.exists():
        return "systemd-user"
    if is_termux():
        return "runit"
    if is_windows():
        return "windows-task"     # informational only — live restart still uses os.execv
    return "exec"


def _my_worker_id() -> str | None:
    """This node's stable worker_id (written by core.stable_worker_id)."""
    try:
        return _WORKER_ID_FILE.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def _ban_files() -> list[Path]:
    """This state directory's flag, plus the one in ~/.rook-band-worker when
    ROOK_WORKER_HOME points elsewhere: a node deauthed before the variable
    moved its state (or with the copy disabled) must stay off the band."""
    legacy = legacy_state_path("banned")
    return [_BANNED] + ([legacy] if legacy is not None else [])


def is_banned() -> bool:
    """True if this node has been deauthed (worker.deauth). Checked at boot by
    the CLI so a banned worker parks itself off-band instead of rejoining."""
    return any(p.exists() for p in _ban_files())


def ban_info() -> dict:
    """Details of the standing ban, or ``{}`` if not banned."""
    for path in _ban_files():
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"at": 0}
    return {}


class SelfUpdatePlugin(Plugin):
    NAMESPACE = "worker"
    PLACEMENT = place(ANDROID_APP_REPLACED)  # the APK has native replacements

    def __init__(self) -> None:
        super().__init__()
        self._stopping = False
        self._converge_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._recheck_task: asyncio.Task | None = None
        # In-band OTA (telesthete Drop) state, wired by bind_worker.
        self._worker = None
        self._ota_rx = None          # active OtaDropReceiver, if a push is running
        self._ota_manifest: dict = {}
        # One bundle install at a time (download -> verify -> swap): a second
        # worker.apply/check/update, or an in-band push finishing, while one
        # runs or after one swapped in (restart pending) is refused, never
        # interleaved: a second swap would overwrite .prev, the rollback
        # copy, with the unverified build.
        self._applying = False
        self._swapped_to = 0

    # -- in-band OTA over telesthete Drop ------------------------------------

    def bind_worker(self, worker) -> None:
        """Grab a handle to the Worker so we can receive in-band bundle pushes
        (raw Drop packets) and send REQUEST/DONE back onto the band. Called at
        worker start and again for each instance a reload builds; stop()
        drops this instance's handler."""
        self._worker = worker
        worker.register_binary_handler(self._ota_binary)

    def _ota_binary(self, payload: bytes, peer_id: tuple) -> bool:
        """Feed inbound telesthete DROP packets to the active receiver. Returns
        True (consumed) for any DROP-channel packet on our band. Native DROP
        frames arrive here already re-wrapped by the codec, so both forms
        look the same."""
        if self._worker is None or self._ota_rx is None:
            return False
        from ..ota_drop import is_drop_packet
        if not is_drop_packet(payload, self._worker.transport.band_id):
            return False
        self._ota_rx.feed(payload)
        return True

    @capability("ota_begin", risk="admin")
    async def _ota_begin(self, manifest: dict, drop_id: int = 0) -> dict:
        """Arm this worker to receive a bundle pushed **in band** over telesthete
        Drop (§8), for the given ``drop_id``. The ed25519-signed manifest is
        verified up front — being on the band is not enough — and its build must
        be newer. The controller then offers the bytes; we pull, verify sha256
        against the manifest, smoke-test, swap, and restart. The delivery channel
        is untrusted; only the signature + hash grant the swap."""
        from .._build_info import BUILD
        from .._update_verify import verify_manifest
        from ..ota_drop import REPLY_DROP_BASE, OtaDropReceiver, valid_ota_drop_id

        if self._worker is None:
            return {"ok": False, "error": "worker not bound (no transport)"}
        try:
            drop_id = int(drop_id)
        except (TypeError, ValueError):
            drop_id = -1
        if not valid_ota_drop_id(drop_id):
            return {"ok": False, "error": f"drop_id must be 0-{REPLY_DROP_BASE - 1} "
                                          "(higher ids carry large replies)"}
        if not isinstance(manifest, dict):
            return {"ok": False, "error": "manifest must be an object"}
        if not verify_manifest(manifest):
            return {"ok": False, "error": "manifest signature invalid (fail-closed)"}
        target = int(manifest.get("build", 0))
        if target <= BUILD and not _HOLD.exists():
            # Nothing to do — tell the controller so it doesn't bother offering.
            return {"ok": True, "action": "up-to-date", "build": BUILD}
        skip = self._skip_reason(target) or self._busy_reason()
        if skip:
            return {"ok": True, "action": "skipped", "build": BUILD, "reason": skip}
        if self._ota_rx is not None:
            return {"ok": False, "error": "an in-band update is already in progress"}

        self._ota_manifest = dict(manifest)
        # The controller's route (its request's ``rr``): our REQUEST/DONE go
        # to it alone on a routing hub. 0 = broadcast, as before.
        from ..wire.envelope import current_reply_opts
        rx = OtaDropReceiver(
            band_id=self._worker.transport.band_id,
            drop_id=int(drop_id),
            send_coro=self._worker.send_raw,
            route=current_reply_opts.get().route,
        )
        rx.on_complete(self._on_ota_complete)
        rx.start()
        self._ota_rx = rx
        log.info("in-band OTA armed: drop_id=%d target_build=%s", drop_id, target)
        return {"ok": True, "action": "receiving", "drop_id": int(drop_id),
                "from_build": BUILD, "to_build": target}

    # -- one install at a time -------------------------------------------------

    def _apply_busy(self) -> dict | None:
        """The refusal for a second install, or None (and the caller may go)."""
        if self._applying:
            return {"ok": False, "error": "an update is already in progress"}
        if self._swapped_to:
            return {"ok": False, "error": f"update to build {self._swapped_to} already "
                                          "installed; restarting"}
        return None

    async def _exclusive(self, coro_fn, *args, **kwargs) -> dict:
        """Run one install exclusively. The check and the claim have no await
        between them, so two calls on the event loop cannot both get in."""
        busy = self._apply_busy()
        if busy:
            return busy
        self._applying = True
        try:
            return await coro_fn(*args, **kwargs)
        finally:
            self._applying = False

    @staticmethod
    def _stage(data: bytes) -> Path:
        """Write a downloaded bundle to a file of its own beside the
        installed one (same filesystem, so the swap is an atomic rename)."""
        import tempfile
        _WORKER_DIR.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="band-worker.", suffix=".pyz.dl", dir=str(_WORKER_DIR))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        return Path(name)

    def _on_ota_complete(self, data: bytes, sha_ok: bool) -> None:
        # Called from the Drop receiver when the file is whole + self-consistent.
        asyncio.ensure_future(self._finish_inband(data, sha_ok))

    async def _finish_inband(self, data: bytes, sha_ok: bool) -> None:
        from .._build_info import BUILD
        from .._update_verify import sha256_file
        manifest = self._ota_manifest
        rx, self._ota_rx = self._ota_rx, None   # clear active state either way
        if rx is not None:
            rx.stop()
        if not sha_ok:
            log.warning("in-band OTA: received bundle failed internal verify; discarding")
            return
        target = int(manifest.get("build", 0))
        _WORKER_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _WORKER_DIR / "band-worker.pyz.inband"
        try:
            tmp.write_bytes(data)
        except Exception:
            log.exception("in-band OTA: could not stage received bundle")
            return
        # The signed manifest's sha256 is the trust anchor — the Drop transport
        # is untrusted, exactly like the HTTP download path.
        if sha256_file(tmp) != manifest.get("sha256"):
            tmp.unlink(missing_ok=True)
            log.warning("in-band OTA: sha256 mismatch vs signed manifest; discarding")
            return
        res = await self._exclusive(self._swap_and_restart, tmp, target, BUILD)
        if not res.get("ok"):
            tmp.unlink(missing_ok=True)
        log.info("in-band OTA result: %s", res)

    # -- lifecycle: OTA convergence ------------------------------------------

    async def start(self) -> None:
        """Finalize any in-flight update, then start the converge loop.

        The loop is inert unless ROOK_UPDATE_URL is set (opt-in per node, done
        by the installer), and it fails closed on manifest signature/hash — so
        auto-update never runs on an unconfigured or untrusted input."""
        try:
            await self._boot_check()
        except Exception:
            log.exception("update boot-check failed")
        self._converge_task = asyncio.create_task(self._converge_loop())

    async def stop(self) -> None:
        self._stopping = True
        for t in (self._converge_task, self._health_task, self._recheck_task):
            if t is not None:
                t.cancel()
        # A reload binds a new instance, which registers its own handler; the
        # old one would otherwise keep a handler per reload (a second
        # receiver for the same OTA push).
        unregister = getattr(self._worker, "unregister_binary_handler", None)
        if callable(unregister):
            unregister(self._ota_binary)

    @capability("check", risk="admin", timeout=None)
    async def _check(self, force: bool = False) -> dict:
        """Check the signed manifest now and update if a newer build is
        published (bypasses the poll interval). ``force=true`` overrides a hold
        pin for this explicit call. Used to drive canary rollouts."""
        return await self._check_and_update(force=force)

    @capability("apply", risk="admin", timeout=None)
    async def _apply(self, manifest: dict) -> dict:
        """Apply a signed update manifest pushed in-band by the controller.
        The ed25519 signature is verified before anything happens, so being on
        the band is not enough to trigger it — only a validly signed manifest is."""
        if not isinstance(manifest, dict):
            return {"ok": False, "error": "manifest must be an object"}
        return await self._apply_manifest(manifest)

    @capability("hold", risk="admin")
    def _hold(self, enable: bool = True) -> dict:
        """Pin this node so it won't auto-update (survives restarts). Use to
        freeze critical hosts / the dongle box. ``worker.check(force=true)``
        still updates it on explicit demand."""
        _HOLD.parent.mkdir(parents=True, exist_ok=True)
        if enable:
            _HOLD.write_text(f"held at {int(time.time())}\n", encoding="utf-8")
        else:
            _HOLD.unlink(missing_ok=True)
        return {"ok": True, "held": enable}

    @capability("deauth", risk="admin", tags=("destructive",))
    async def _deauth(self, payload: dict) -> dict:
        """Remove/ban this worker from the band — but ONLY on an ed25519-signed
        order from the controller. Being on the band (knowing the shared PSK) is
        not enough: ``payload`` is verified against the same signing key as OTA
        manifests, and is bound to THIS node's stable worker_id so a signed order
        for one worker can't be replayed against another.

        payload = {"typ": "rook-deauth", "v": 2, "worker_id": "<target>",
                   "name": "<label>", "issued_at": <unix>, "reason": "...",
                   "sig": "<base64 ed25519 over b'rook-deauth-v2\\n' + canonical body>"}

        (or a legacy v1 payload carrying that order under ``v2``). The v2 order
        has its own signature domain, so an OTA manifest can never pass for a
        deauth order, and ``worker_id`` and ``issued_at`` are required
        (permissions 6.3). Unprefixed v1 orders are refused.

        On success the node persists a ``banned`` flag and restarts into the CLI
        boot gate, which parks it OFF the band (no transport, no announce). The
        flag survives reboots; clear ``~/.rook-band-worker/banned`` and restart
        to rejoin.

        LIMITATION: this stops a COOPERATIVE worker (one running this code). A
        compromised node running modified code can ignore it and keep using the
        shared PSK — the controller still hides/ignores it and denies it commands
        and updates, but truly evicting a hostile node needs per-worker identity
        or a PSK rotation."""
        from ...core.authz import default_anchors, verify_deauth
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be an object"}
        mine = _my_worker_id() or (self._worker.worker_id if self._worker is not None else None)
        ok, why = verify_deauth(payload, default_anchors(), mine)
        if not ok:
            # Fail closed: unsigned, wrong key, wrong domain, wrong target or stale.
            return {"ok": False, "error": why}
        if isinstance(payload.get("v2"), dict):
            payload = payload["v2"]
        atomic_write_text(_BANNED, json.dumps({
            "at": int(time.time()),
            "reason": str(payload.get("reason", ""))[:500],
        }) + "\n")
        log.warning("deauthed from band (reason=%r); restarting into dormant mode",
                    payload.get("reason", ""))
        # Ack first, then restart INTO the boot gate — the fresh process sees the
        # banned flag and parks off-band. (Same deferred-restart trick as the
        # rest of this plugin so the caller gets the reply before we drop.)
        self._schedule_restart(self._current_argv())
        return {"ok": True, "worker_id": mine, "banned": True, "restarting": True}

    # -- introspection -------------------------------------------------------

    @capability("status", risk="read")
    def _status(self) -> dict:
        """Build, version, supervisor and update state of this worker (band key redacted)."""
        from .._build_info import as_dict as _build
        argv=list(sys.argv)
        for index,value in enumerate(argv):
            if index and argv[index-1] in ('--psk','--pair-code'):
                argv[index]='<redacted>'
            elif value.startswith(('--psk=','--pair-code=')):
                argv[index]=value.split('=',1)[0]+'=<redacted>'
        return {
            "pid": os.getpid(),
            "python": sys.executable,
            "argv": argv,
            "pyz": str(_PYZ),
            "pyz_exists": _PYZ.exists(),
            "supervisor": _supervisor(),
            # In-flight update (stage, target) and the last update's outcome,
            # e.g. a rollback and its reason.
            "update": {"state": self._read_state(), "last": self._last_result()},
            **_build(),
        }

    def heartbeat(self) -> dict | None:
        """Rides the announce (``hb.worker``) only after a rollback, so the
        hub, dashboard and rook_workers show why this node is behind."""
        from .._build_info import BUILD
        r = self._last_result()
        if not r or r.get("result") not in ("rolled_back", "inconclusive"):
            return None
        # The restored build, or the bad one in its last seconds before the
        # restart (a stale record never shows on a later build).
        rolling = (r.get("from_build") == BUILD
                   and (self._read_state() or {}).get("stage") == "rolledback")
        if r.get("to_build") != BUILD and not rolling:
            return None
        return {"update": f"rolled back: {r.get('reason', 'unknown')}",
                "bad_build": r.get("from_build"), "at": r.get("at")}

    # -- lifecycle -----------------------------------------------------------

    @capability("restart", risk="admin", tags=("destructive",))
    async def _restart(self) -> dict:
        """Restart this worker with its current config, via the service manager
        (or re-exec). Kill-safe."""
        self._schedule_restart(self._current_argv())
        return {"ok": True, "supervisor": _supervisor(), "restarting": True}

    @capability("reconfigure", risk="admin", tags=("destructive",))
    async def _reconfigure(self, hub: str | None = None, psk: str | None = None,
                           name: str | None = None, restart: bool = True) -> dict:
        """Repoint this worker at a new band (hub/psk) and/or rename it, then
        restart. The new config is persisted to the systemd unit so it survives
        reboots. Used to migrate bands without a curl|bash reinstall.

        Changing ``hub`` or ``psk`` needs a hub-signed order: the call must
        carry a valid ticket from the hub (permissions 6.3). Renaming does not.
        """
        if hub is None and psk is None and name is None:
            return {"ok": False, "error": "nothing to change (pass hub/psk/name)"}
        if hub is not None or psk is not None:
            from ..authz_guard import require_hub_order
            refused = require_hub_order("changing hub/psk")
            if refused:
                return {"ok": False, "error": refused}
        new_argv = self._current_argv(hub=hub, psk=psk, name=name)
        persisted = self._persist(new_argv)
        if restart:
            self._schedule_restart(new_argv)
        return {
            "ok": True,
            "supervisor": _supervisor(),
            "persisted_unit": persisted,
            "restarting": restart,
            # never echo the psk value back onto the band
            "changed": [k for k, v in (("hub", hub), ("psk", psk), ("name", name)) if v is not None],
        }

    @capability("update", risk="admin", tags=("destructive",), timeout=None)
    async def _update(self, url: str | None = None, hub: str | None = None,
                      psk: str | None = None, name: str | None = None,
                      manifest: dict | None = None) -> dict:
        """Install a worker bundle, optionally repoint at a new band, then
        restart. Kill-safe.

        Installing needs a signed OTA ``manifest`` (the same object
        ``worker.apply`` takes): the bundle is fetched from ``url`` (or the
        manifest's own ``url``), its sha256 must match the manifest and it
        must pass ``--selftest``. A bare ``url`` is refused, so this cap can't
        bypass OTA signing, and an older build than the running one is
        refused (no downgrades). Changing ``hub``/``psk`` needs a hub-signed
        order (a valid call ticket), as for ``worker.reconfigure``."""
        from .._build_info import BUILD
        from .._update_verify import sha256_file, verify_manifest
        notes = []
        if hub is not None or psk is not None:
            from ..authz_guard import require_hub_order
            refused = require_hub_order("changing hub/psk")
            if refused:
                return {"ok": False, "error": refused}
        if url or manifest is not None:
            if not isinstance(manifest, dict) or not verify_manifest(manifest):
                return {"ok": False, "error": (
                    "worker.update needs a signed OTA manifest (manifest=...); an "
                    "unsigned url is refused (fail-closed)")}
            try:
                target = int(manifest.get("build", 0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "manifest has no valid build number"}
            if target < BUILD:
                return {"ok": False, "error": f"refusing to downgrade from build {BUILD} to {target}"}
            src = url or manifest.get("url")
            if not src:
                return {"ok": False, "error": "no download url (pass url= or a manifest with url)"}
            res = await self._exclusive(self._install_for_update, manifest, src, target)
            if not res.get("ok"):
                return res
            notes.append(f"bundle build {target} installed from {src}")
        new_argv = self._current_argv(hub=hub, psk=psk, name=name)
        persisted = self._persist(new_argv)
        self._schedule_restart(new_argv)
        return {"ok": True, "supervisor": _supervisor(), "notes": notes,
                "persisted_unit": persisted, "restarting": True}

    async def _install_for_update(self, manifest: dict, src: str, target: int) -> dict:
        """worker.update's install: download, sha256 against the signed
        manifest, --selftest, swap (keeping .prev). Run via _exclusive."""
        from .._update_verify import sha256_file
        try:
            data = await self._http_get(src, timeout=120)
        except Exception as e:
            return {"ok": False, "error": f"download failed: {type(e).__name__}: {e}"}
        tmp = self._stage(data)
        if sha256_file(tmp) != manifest.get("sha256"):
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": "sha256 mismatch vs signed manifest (fail-closed)"}
        if not await self._smoke_test(tmp):
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": "bundle failed --selftest; not installing"}
        try:
            import shutil
            if _PYZ.exists():
                shutil.copy2(_PYZ, _PREV)
            # chmod before the swap: the new bundle is never in place
            # without its execute bit.
            os.chmod(tmp, 0o755)
            os.replace(tmp, _PYZ)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": f"swap failed: {type(e).__name__}: {e}"}
        self._swapped_to = target
        return {"ok": True}

    # -- OTA convergence internals -------------------------------------------

    @staticmethod
    def _manifest_url() -> str:
        return os.environ.get("ROOK_UPDATE_URL", "").strip()

    async def _http_get(self, url: str, timeout: float) -> bytes:
        def _f() -> bytes:
            import urllib.request
            # Explicit UA: Cloudflare 403s the default "Python-urllib/x" agent.
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        return await asyncio.get_event_loop().run_in_executor(None, _f)

    async def _converge_loop(self) -> None:
        """Poll the manifest and self-update when a newer signed build appears.
        Inert if no manifest URL is configured."""
        import random
        if not self._manifest_url():
            return
        await asyncio.sleep(5 + random.random() * 10)  # small startup jitter
        while not self._stopping:
            try:
                res = await self._check_and_update()
                if res.get("action") == "updated":
                    return  # restart scheduled; stop polling from this process
            except Exception:
                log.exception("converge check failed")
            # jittered interval so a fleet doesn't stampede the installer
            await asyncio.sleep(_POLL_SECS * (1.0 + random.random() * 0.2))

    async def _check_and_update(self, *, force: bool = False) -> dict:
        """Poll path: fetch the manifest from ROOK_UPDATE_URL, then apply it."""
        url = self._manifest_url()
        if not url:
            return {"ok": False, "error": "no manifest url (ROOK_UPDATE_URL unset)"}
        try:
            manifest = json.loads(await self._http_get(url, timeout=15))
        except Exception as e:
            return {"ok": False, "error": f"manifest fetch failed: {type(e).__name__}: {e}"}
        return await self._apply_manifest(manifest, force=force, base_url=url)

    async def _apply_manifest(self, manifest: dict, *, force: bool = False,
                              base_url: str | None = None) -> dict:
        """Apply a signed manifest — from the poll loop OR pushed in-band via
        worker.apply. Verifies signature + hash + selftest before swapping, so
        the delivery channel is untrusted; only the signature grants the swap.
        One at a time (:meth:`_exclusive`)."""
        return await self._exclusive(self._apply_manifest_now, manifest, force=force,
                                     base_url=base_url)

    async def _apply_manifest_now(self, manifest: dict, *, force: bool = False,
                                  base_url: str | None = None) -> dict:
        from .._build_info import BUILD
        from .._update_verify import verify_manifest, sha256_file

        if _HOLD.exists() and not force:
            return {"ok": True, "action": "held", "build": BUILD}
        if not verify_manifest(manifest):
            log.warning("update manifest failed signature verification; ignoring")
            return {"ok": False, "error": "manifest signature invalid (fail-closed)"}

        target = int(manifest.get("build", 0))
        if target <= BUILD:
            return {"ok": True, "action": "up-to-date", "build": BUILD}
        skip = None if force else (self._skip_reason(target) or self._busy_reason())
        if skip:
            return {"ok": True, "action": "skipped", "build": BUILD, "reason": skip}

        # Download origin: a signed `url` in the manifest wins (self-contained
        # push); otherwise derive from the manifest's own URL / ROOK_UPDATE_URL.
        pyz_url = manifest.get("url")
        if not pyz_url:
            base = base_url or self._manifest_url()
            if base:
                from urllib.parse import urljoin
                pyz_url = urljoin(base, manifest.get("filename", "band-worker.pyz"))
        if not pyz_url:
            return {"ok": False, "error": "no download url (manifest lacks url and ROOK_UPDATE_URL unset)"}
        try:
            data = await self._http_get(pyz_url, timeout=120)
        except Exception as e:
            return {"ok": False, "error": f"bundle download failed: {type(e).__name__}: {e}"}
        tmp = self._stage(data)
        if sha256_file(tmp) != manifest.get("sha256"):
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": "sha256 mismatch (fail-closed)"}
        return await self._swap_and_restart(tmp, target, BUILD)

    async def _swap_and_restart(self, tmp: Path, target: int, from_build: int) -> dict:
        """Smoke-test the staged bundle, swap it in (keeping .prev for rollback),
        record the update state, and schedule a restart. Shared by the HTTP
        manifest path and the in-band Drop push — both stage a verified file
        here after their own sha256 check."""
        if not await self._smoke_test(tmp):
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": "bundle failed --selftest; not swapping"}
        try:
            import shutil
            if _PYZ.exists():
                shutil.copy2(_PYZ, _PREV)   # keep the old bundle for rollback
            os.chmod(tmp, 0o755)
            os.replace(tmp, _PYZ)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": f"swap failed: {type(e).__name__}: {e}"}
        self._swapped_to = target

        self._write_state({"stage": "swapped", "target_build": target,
                           "prev_build": from_build, "attempts": 0, "at": int(time.time()),
                           # Seconds since this (old) build last heard the band,
                           # None if never: decides whether the new build's
                           # band gate is enforced (see _gate_enforced).
                           "band_seen_ago": self._band_seen_ago(),
                           # Whether this build had someone to probe (a peer
                           # announced, or a request was answered): on a band
                           # where nothing announces (a hub without its node)
                           # the gate could never pass, so it isn't enforced.
                           "probe_target": self._had_probe_target()})
        log.info("updated bundle build %s -> %s; restarting", from_build, target)
        self._schedule_restart(self._current_argv())
        return {"ok": True, "action": "updated", "from_build": from_build,
                "to_build": target, "restarting": True}

    async def _smoke_test(self, pyz_path: Path) -> bool:
        """Run the downloaded bundle's --selftest in a subprocess. A broken or
        incompatible bundle exits non-zero here, so we never swap it in."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(pyz_path), "--selftest",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except Exception:
            log.exception("smoke test could not start")
            return False
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return rc == 0

    # -- post-restart health / rollback --------------------------------------

    def _read_state(self) -> dict | None:
        try:
            return json.loads(_STATE.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_state(self, d: dict) -> None:
        atomic_write_json(_STATE, d)

    def _clear_state(self) -> None:
        _STATE.unlink(missing_ok=True)

    def _last_result(self) -> dict | None:
        try:
            r = json.loads(_RESULT.read_text(encoding="utf-8"))
        except Exception:
            return None
        return r if isinstance(r, dict) else None

    def _write_result(self, d: dict) -> None:
        try:
            atomic_write_json(_RESULT, {**d, "at": int(time.time())})
        except Exception:
            log.exception("could not record the update result")

    def _skip_reason(self, target: int) -> str | None:
        """Why ``target`` must not be applied again yet: it was rolled back
        here recently. None when it may be applied."""
        r = self._last_result()
        if not r or r.get("from_build") != target:
            return None
        wait = {"rolled_back": _RETRY_BAD_SECS,
                "inconclusive": _RETRY_INCONCLUSIVE_SECS}.get(r.get("result"))
        if wait is None:
            return None
        try:
            if time.time() - float(r.get("at", 0)) >= wait:
                return None
        except (TypeError, ValueError):
            return None
        return f"build {target} was rolled back here: {r.get('reason', 'unknown')}"

    def _busy_reason(self) -> str | None:
        """Set while this boot is still verifying an update: swapping again
        now would overwrite .prev, the last build known to work."""
        task = self._health_task
        if task is None or task.done():
            return None
        st = self._read_state() or {}
        return f"still verifying build {st.get('target_build')}"

    def _had_probe_target(self) -> bool | None:
        """Whether the bound worker has (had) someone to probe; None without
        a worker that tracks it."""
        w = self._worker
        peers = getattr(w, "band_peers", None)
        if not callable(peers):
            return None
        try:
            return bool(peers()) or getattr(w, "band_rtt_at", None) is not None
        except Exception:
            return None

    def _band_seen_ago(self) -> int | None:
        """Seconds since the bound worker last received an authenticated band
        message, or None (never, or no worker bound)."""
        rx = getattr(self._worker, "band_rx_at", None)
        if not isinstance(rx, (int, float)):
            return None
        return max(0, int(time.monotonic() - rx))

    async def _boot_check(self) -> None:
        """On startup, resolve any in-flight update: confirm the new build is
        healthy (and drop .prev), or roll back after too many failed boots."""
        from .._build_info import BUILD
        st = self._read_state()
        if not st:
            return
        if st.get("stage") == "rolledback":
            # We are the build a rollback restored; the reason stays in the
            # result file for status / heartbeat.
            log.warning("running build %s after rolling back build %s: %s", BUILD,
                        st.get("prev_build"), st.get("reason", "unknown"))
            self._clear_state()
            if st.get("recheck") and st.get("target_build") == BUILD:
                self._recheck_task = asyncio.create_task(
                    self._recheck_band(st.get("prev_build")))
            return
        if st.get("stage") not in ("swapped", "verifying"):
            self._clear_state()
            return
        target = st.get("target_build")
        if BUILD == target:
            boot = _boot_id()
            if st.get("boot") != boot:
                # A real boot (a plugin reload keeps the boot id and counts
                # for nothing). It is a crash boot only if the previous run
                # died before it had stayed up _HEALTH_SECS: a settings
                # restart in the window is not a crash.
                prev = st.get("boot")
                if prev is None or st.get("survived") != prev:
                    st["attempts"] = int(st.get("attempts", 0)) + 1
                st["boots"] = int(st.get("boots", 0)) + 1
                st["boot"] = boot
            st["stage"] = "verifying"
            if st.get("attempts", 0) > _MAX_BOOT_ATTEMPTS or st.get("boots", 0) > _MAX_BOOTS:
                log.error("build %s failed to stay healthy (%d crash boots, %d boots); "
                          "rolling back", target, st.get("attempts", 0), st.get("boots", 0))
                await self._rollback(st, f"crash loop: {st.get('attempts', 0)} crash "
                                         f"boot(s) of {st.get('boots', 0)} without "
                                         "being verified")
                return
            self._write_state(st)
            if self._gate_enforced(st):
                self._health_task = asyncio.create_task(self._band_gate(target))
            else:
                self._health_task = asyncio.create_task(self._finalize_health(target))
        else:
            # Running some other build than intended (manual reconfigure, etc.).
            log.info("update state target=%s but running build=%s; clearing", target, BUILD)
            self._clear_state()

    def _gate_enforced(self, st: dict) -> bool:
        """Whether this update boot must prove band contact. Not when the gate
        is off or no worker (transport) is bound; and not unless the old build
        swapped recently (_CONTACT_WINDOW), had heard the band within that
        window, and had a probe target. A swap by a build that predates the
        gate records none of that. Otherwise the band was likely unreachable
        (or unprobeable) for reasons a rollback can't fix."""
        secs = _gate_secs()
        if secs <= 0:
            return False
        if self._worker is None or not callable(getattr(self._worker, "request", None)):
            return False
        ago = st.get("band_seen_ago")
        why = None
        if not isinstance(ago, (int, float)) or ago > _CONTACT_WINDOW:
            why = "no band contact before the update"
        elif st.get("probe_target") is not True:
            why = "no peer to probe before the update"
        else:
            try:
                if time.time() - float(st.get("at", 0)) >= _CONTACT_WINDOW:
                    why = "the update was swapped in too long ago"
            except (TypeError, ValueError):
                why = "no swap time"
        if why:
            log.info("update to build %s: band gate not enforced (%s); using the "
                     "%ds alive check", st.get("target_build"), why, int(_HEALTH_SECS))
            return False
        return True

    async def _finalize_health(self, target: int) -> None:
        try:
            await asyncio.sleep(_HEALTH_SECS)
        except asyncio.CancelledError:
            return
        self._confirm(target, "alive")

    def _mark_survived(self) -> None:
        """Record that this run stayed up _HEALTH_SECS, so its restart (a
        settings change, worker.restart) isn't counted as a crash boot."""
        st = self._read_state()
        if st and st.get("stage") == "verifying" and st.get("boot") == _boot_id():
            st["survived"] = st["boot"]
            self._write_state(st)

    def _confirm(self, target: int, check: str) -> None:
        st = self._read_state()
        if st and st.get("target_build") == target:
            self._clear_state()
            try:
                _PREV.unlink(missing_ok=True)
            except Exception:
                pass
            self._write_result({"result": "ok", "build": target,
                                "from_build": st.get("prev_build"), "check": check})
            log.info("update to build %s verified healthy (%s)", target, check)

    async def _await_round_trip(self, window: float, on_tick=None) -> tuple[bool, int]:
        """Wait up to ``window`` seconds for the bound worker to complete a
        band round trip, probing a peer it heard announce every _PROBE_SECS.
        Returns (proven, probes sent). A tick that comes back much later than
        planned means the host slept: the deadline moves out by the gap, so a
        suspend doesn't use up the window. ``on_tick(elapsed)`` runs each
        tick."""
        w = self._worker
        began = _now()
        deadline = began + window
        next_probe = began
        tries = 0

        def resumed(planned: float) -> None:
            nonlocal deadline, next_probe
            gap = _now() - planned
            if gap > _SUSPEND_GAP_SECS:
                log.info("band gate: %.0fs gap (host suspended?); extending the window", gap)
                deadline += gap
                next_probe = _now()

        while getattr(w, "band_rtt_at", None) is None:
            now = _now()
            if on_tick is not None:
                on_tick(now - began)
            if now >= deadline:
                break
            peers = w.band_peers() if callable(getattr(w, "band_peers", None)) else []
            if peers and now >= next_probe:
                peer = peers[tries % len(peers)]
                tries += 1
                next_probe = now + _PROBE_SECS
                timeout = max(0.1, min(_PROBE_TIMEOUT, deadline - now))
                try:
                    await w.request(_PROBE_CAP, {"prefix": "worker.status"}, target=peer,
                                    timeout=timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as e:     # timeout, or the link is down
                    log.debug("band probe to %s failed: %s", peer, e)
                resumed(now + timeout)
                continue
            # Tick at most 1 s: catch a reply to another request, or the first
            # announce that gives us someone to probe.
            wake = min(deadline, now + 1.0, next_probe if peers else now + 1.0)
            await asyncio.sleep(max(0.05, wake - now))
            resumed(max(wake, now + 0.05))
        return getattr(w, "band_rtt_at", None) is not None, tries

    async def _band_gate(self, target: int) -> None:
        """Keep the new build only once it has proven two-way band contact: a
        reply to one of its own requests. That covers everything the old build
        relied on — our frames are sealed so peers open them, the relay
        forwards both ways, our route declaration lets the reply find us, and
        we decrypt what comes back — where staying alive proves none of it.
        It proves *outbound* round trips (our calls answered), not that peers'
        calls reach our caps; those ride the same path, but the gate does not
        test them separately.

        Any reply counts, even a late one (e.g. the hub answering a pushed
        secret fetch); otherwise we probe: ``caps.describe`` aimed at a peer
        we heard announce, the hub node first (it announces every ~30 s). An
        error reply proves the round trip just as well. No proof within the
        window: roll back now instead of waiting for crash boots, since a
        build that can't reach the band can't be fixed over it. If nobody
        announced at all but the band was heard, there is no one to ask: the
        alive check decides instead."""
        w = self._worker
        marked = False

        def tick(elapsed: float) -> None:
            nonlocal marked
            if not marked and elapsed >= _HEALTH_SECS:
                marked = True
                self._mark_survived()

        began = _now()
        try:
            proven, tries = await self._await_round_trip(_gate_secs(), tick)
        except asyncio.CancelledError:
            return
        if proven:
            log.info("build %s proved band contact after %.0fs", target, _now() - began)
            self._confirm(target, "band")
            return
        heard = getattr(w, "band_rx_at", None) is not None
        if heard and tries == 0:
            log.warning("build %s: band heard but no peer announced to probe; keeping it "
                        "on the alive check", target)
            self._confirm(target, "alive (no probe target)")
            return
        reason = ("no band contact: " + (
            f"heard the band but got no reply to {tries} probe(s) in {int(_gate_secs())}s"
            if heard else f"heard nothing from the band in {int(_gate_secs())}s"))
        st = self._read_state()
        if not st or st.get("target_build") != target:
            return      # resolved elsewhere meanwhile
        log.error("build %s: %s; rolling back", target, reason)
        await self._rollback(st, reason, recheck=True)

    async def _recheck_band(self, bad_build) -> None:
        """Run by the build a band-gate rollback restored: can *it* reach the
        band? If so the new build was at fault and the long retry wait stands.
        If not, the band was the problem (hub outage, network, sleep): record
        the result as inconclusive, which only holds the build back an hour."""
        if self._worker is None or not callable(getattr(self._worker, "request", None)):
            return
        try:
            proven, tries = await self._await_round_trip(_gate_secs())
        except asyncio.CancelledError:
            return
        r = self._last_result() or {}
        if r.get("result") != "rolled_back" or r.get("from_build") != bad_build:
            return
        if proven:
            log.info("restored build reached the band; build %s stays held back", bad_build)
            return
        why = f"{r.get('reason', 'no band contact')}; the previous build could not reach " \
              "the band either (inconclusive)"
        log.warning("build %s rollback inconclusive: %s", bad_build, why)
        self._write_result({**r, "result": "inconclusive", "reason": why})

    async def _rollback(self, st: dict, reason: str = "crash loop", *,
                        recheck: bool = False) -> None:
        if not _PREV.exists():
            log.error("rollback requested (%s) but no .prev bundle; clearing state", reason)
            self._write_result({"result": "unverified", "build": st.get("target_build"),
                                "reason": f"{reason}; no previous bundle to roll back to"})
            self._clear_state()
            return
        try:
            import shutil
            shutil.copy2(_PREV, _PYZ)
            os.chmod(_PYZ, 0o755)
        except Exception as e:
            log.exception("rollback copy failed")
            self._write_result({"result": "rollback_failed", "build": st.get("target_build"),
                                "reason": f"{reason}; restoring the previous bundle "
                                          f"failed: {type(e).__name__}: {e}"})
            self._clear_state()
            return
        self._write_state({"stage": "rolledback", "target_build": st.get("prev_build"),
                           "prev_build": st.get("target_build"), "attempts": 0,
                           "reason": reason, "recheck": recheck, "at": int(time.time())})
        self._write_result({"result": "rolled_back", "reason": reason,
                            "from_build": st.get("target_build"),
                            "to_build": st.get("prev_build")})
        log.warning("rolled back build %s to %s (%s); restarting",
                    st.get("target_build"), st.get("prev_build"), reason)
        # Last word from this build, best effort: with only one direction of
        # the band broken the hub may still hear it before we restart.
        announce = getattr(self._worker, "announce", None)
        if callable(announce):
            try:
                await asyncio.wait_for(announce(), 2.0)
            except Exception:
                pass
        self._schedule_restart(self._current_argv())

    # -- internals -----------------------------------------------------------

    def _current_argv(self, hub: str | None = None, psk: str | None = None,
                      name: str | None = None) -> list[str]:
        """Reconstruct the full launch command (python + pyz + args), applying
        any hub/psk/name overrides. Preserves every other existing flag."""
        argv = [sys.executable, *sys.argv]  # sys.argv[0] is the pyz path
        # Started as ``python -m rook.worker``: sys.argv[0] is the package's
        # __main__.py, which cannot run as a plain script (relative imports).
        spec = getattr(sys.modules.get("__main__"), "__spec__", None)
        mod = getattr(spec, "name", "") or ""
        if mod.endswith(".__main__") and sys.argv and sys.argv[0].endswith("__main__.py"):
            argv = [sys.executable, "-m", mod[:-len(".__main__")], *sys.argv[1:]]

        def setflag(flag: str, val: str | None) -> None:
            if val is None:
                return
            if flag in argv:
                argv[argv.index(flag) + 1] = val
            else:
                argv.extend([flag, val])

        setflag("--hub", hub)
        setflag("--psk", psk)
        setflag("--name", name)
        return argv

    def _persist(self, argv: list[str]) -> bool:
        """Write the new launch command into the systemd --user unit (or the
        Windows Scheduled Task) so it survives restarts/reboots/logons.
        No-op (returns False) when neither applies."""
        if is_windows():
            return self._persist_windows(argv)
        if not _UNIT.exists():
            return False
        try:
            text = _UNIT.read_text(encoding="utf-8")
            execstart = "ExecStart=" + " ".join(argv)
            out, replaced = [], False
            for line in text.splitlines():
                if line.startswith("ExecStart="):
                    out.append(execstart)
                    replaced = True
                else:
                    out.append(line)
            if not replaced:
                return False
            # A torn unit file would leave systemd unable to start the worker.
            unit = _UNIT.resolve()  # replace a symlinked unit's target, not the link
            atomic_write_text(unit, "\n".join(out) + "\n", unit.stat().st_mode & 0o777)
            return True
        except Exception:
            log.exception("failed to persist unit")
            return False

    def _persist_windows(self, argv: list[str]) -> bool:
        """Rewrite the RookBandWorker Scheduled Task's action so a hub/psk/name
        change from worker.reconfigure survives the next logon — the Task
        Scheduler equivalent of rewriting the systemd unit's ExecStart. Live
        restart still goes through os.execv (see _do_restart); this only
        affects what runs on the NEXT AtLogon trigger."""
        import shutil
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            return False
        exe, *rest = argv

        def esc(s: str) -> str:
            return s.replace("'", "''")

        arg_str = " ".join(f'"{esc(a)}"' for a in rest)
        script = (
            f"$a = New-ScheduledTaskAction -Execute '{esc(exe)}' -Argument '{arg_str}'; "
            f"Set-ScheduledTask -TaskName '{_WIN_TASK}' -Action $a"
        )
        try:
            r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:
            log.exception("failed to persist scheduled task")
            return False

    def _schedule_restart(self, argv: list[str], delay: float = 1.0) -> None:
        """Fire the restart shortly *after* this call returns, so the reply is
        delivered to the caller before the worker is replaced."""
        loop = asyncio.get_event_loop()
        loop.call_later(delay, lambda: asyncio.ensure_future(self._do_restart(argv)))

    async def _do_restart(self, argv: list[str]) -> None:
        sup = _supervisor()
        try:
            if sup == "systemd-user":
                # systemd owns the swap: it stops this process (and the
                # systemctl client) and starts a fresh instance from the unit.
                # The job is queued before the kill, so it completes regardless.
                await self._run("systemctl", "--user", "daemon-reload")
                await self._run("systemctl", "--user", "restart", "--no-block", _SVC)
                return
            if sup == "runit":
                await self._run("sv", "restart", _SVC)
                return
            # No service manager: replace this process image in place.
            log.info("re-exec: %s", " ".join(argv))
            os.execv(argv[0], argv)
        except Exception:
            log.exception("restart failed; attempting in-place re-exec")
            try:
                os.execv(argv[0], argv)
            except Exception:
                log.exception("re-exec failed")

    async def _run(self, *cmd: str) -> None:
        try:
            await run(cmd, 10)
        except RunTimeout:
            pass   # killed; the restart below re-execs us either way


PLUGIN = SelfUpdatePlugin
