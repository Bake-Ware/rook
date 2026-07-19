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
    worker.update(url?, hub?, psk?, name?) — fetch a new bundle, then restart
    worker.deauth(payload)                — ed25519-signed remove/ban from band
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from ..plugin import Plugin, capability

log = logging.getLogger("rook.worker.plugins.selfupdate")

_HOME = Path(os.path.expanduser("~"))
_WORKER_DIR = _HOME / ".rook-band-worker"
_PYZ = _WORKER_DIR / "band-worker.pyz"
_PREV = _WORKER_DIR / "band-worker.pyz.prev"     # previous bundle, for rollback
_STATE = _WORKER_DIR / "update_state.json"       # in-flight update tracking
_HOLD = _WORKER_DIR / "hold"                      # presence pins this node (no auto-update)
_BANNED = _WORKER_DIR / "banned"                  # presence = deauthed; refuse to rejoin
_WORKER_ID_FILE = _WORKER_DIR / "worker_id"       # stable identity (see core.stable_worker_id)
_UNIT = _HOME / ".config" / "systemd" / "user" / "rook-band-worker.service"
_SVC = "rook-band-worker"

# OTA convergence tuning.
_POLL_SECS = float(os.environ.get("ROOK_UPDATE_POLL", "300"))  # manifest check interval
_HEALTH_SECS = 60.0        # stay alive this long post-swap before declaring success
_MAX_BOOT_ATTEMPTS = 3     # boots into a new build without health before rolling back
_UA = "rook-worker"        # Cloudflare blocks the default urllib User-Agent


def _is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "") or bool(os.environ.get("TERMUX_VERSION"))


def _supervisor() -> str:
    if _UNIT.exists():
        return "systemd-user"
    if _is_termux():
        return "runit"
    return "exec"


def _my_worker_id() -> str | None:
    """This node's stable worker_id (written by core.stable_worker_id)."""
    try:
        return _WORKER_ID_FILE.read_text().strip() or None
    except Exception:
        return None


def is_banned() -> bool:
    """True if this node has been deauthed (worker.deauth). Checked at boot by
    the CLI so a banned worker parks itself off-band instead of rejoining."""
    return _BANNED.exists()


def ban_info() -> dict:
    """Details of the standing ban, or ``{}`` if not banned."""
    try:
        return json.loads(_BANNED.read_text())
    except Exception:
        return {"at": 0} if _BANNED.exists() else {}


class SelfUpdatePlugin(Plugin):
    NAMESPACE = "worker"

    def __init__(self) -> None:
        super().__init__()
        self._stopping = False
        self._converge_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        # In-band OTA (telesthete Drop) state, wired by bind_worker.
        self._worker = None
        self._ota_rx = None          # active OtaDropReceiver, if a push is running
        self._ota_manifest: dict = {}

    # -- in-band OTA over telesthete Drop ------------------------------------

    def bind_worker(self, worker) -> None:
        """Grab a handle to the Worker so we can receive in-band bundle pushes
        (raw Drop packets) and send REQUEST/DONE back onto the band. Called once
        at worker start."""
        self._worker = worker
        worker.register_binary_handler(self._ota_binary)

    def _ota_binary(self, payload: bytes, peer_id: tuple) -> bool:
        """Feed inbound telesthete DROP packets to the active receiver. Returns
        True (consumed) for any DROP-channel packet on our band."""
        if self._worker is None or self._ota_rx is None:
            return False
        from ..ota_drop import is_drop_packet
        if not is_drop_packet(payload, self._worker.transport.band_id):
            return False
        self._ota_rx.feed(payload)
        return True

    @capability("ota_begin")
    async def _ota_begin(self, manifest: dict, drop_id: int = 0) -> dict:
        """Arm this worker to receive a bundle pushed **in band** over telesthete
        Drop (§8), for the given ``drop_id``. The ed25519-signed manifest is
        verified up front — being on the band is not enough — and its build must
        be newer. The controller then offers the bytes; we pull, verify sha256
        against the manifest, smoke-test, swap, and restart. The delivery channel
        is untrusted; only the signature + hash grant the swap."""
        from .._build_info import BUILD
        from .._update_verify import verify_manifest
        from ..ota_drop import OtaDropReceiver

        if self._worker is None:
            return {"ok": False, "error": "worker not bound (no transport)"}
        if not isinstance(manifest, dict):
            return {"ok": False, "error": "manifest must be an object"}
        if not verify_manifest(manifest):
            return {"ok": False, "error": "manifest signature invalid (fail-closed)"}
        target = int(manifest.get("build", 0))
        if target <= BUILD and not _HOLD.exists():
            # Nothing to do — tell the controller so it doesn't bother offering.
            return {"ok": True, "action": "up-to-date", "build": BUILD}
        if self._ota_rx is not None:
            return {"ok": False, "error": "an in-band update is already in progress"}

        self._ota_manifest = dict(manifest)
        rx = OtaDropReceiver(
            band_id=self._worker.transport.band_id,
            drop_id=int(drop_id),
            send_coro=self._worker.send_raw,
        )
        rx.on_complete(self._on_ota_complete)
        rx.start()
        self._ota_rx = rx
        log.info("in-band OTA armed: drop_id=%d target_build=%s", drop_id, target)
        return {"ok": True, "action": "receiving", "drop_id": int(drop_id),
                "from_build": BUILD, "to_build": target}

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
        res = await self._swap_and_restart(tmp, target, BUILD)
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
        for t in (self._converge_task, self._health_task):
            if t is not None:
                t.cancel()

    @capability("check")
    async def _check(self, force: bool = False) -> dict:
        """Check the signed manifest now and update if a newer build is
        published (bypasses the poll interval). ``force=true`` overrides a hold
        pin for this explicit call. Used to drive canary rollouts."""
        return await self._check_and_update(force=force)

    @capability("apply")
    async def _apply(self, manifest: dict) -> dict:
        """Apply a signed update manifest pushed in-band by the controller.
        The ed25519 signature is verified before anything happens, so being on
        the band is not enough to trigger it — only a validly signed manifest is."""
        if not isinstance(manifest, dict):
            return {"ok": False, "error": "manifest must be an object"}
        return await self._apply_manifest(manifest)

    @capability("hold")
    def _hold(self, enable: bool = True) -> dict:
        """Pin this node so it won't auto-update (survives restarts). Use to
        freeze critical hosts / the dongle box. ``worker.check(force=true)``
        still updates it on explicit demand."""
        _WORKER_DIR.mkdir(parents=True, exist_ok=True)
        if enable:
            _HOLD.write_text(f"held at {int(time.time())}\n")
        else:
            _HOLD.unlink(missing_ok=True)
        return {"ok": True, "held": enable}

    @capability("deauth")
    async def _deauth(self, payload: dict) -> dict:
        """Remove/ban this worker from the band — but ONLY on an ed25519-signed
        order from the controller. Being on the band (knowing the shared PSK) is
        not enough: ``payload`` is verified against the same signing key as OTA
        manifests, and is bound to THIS node's stable worker_id so a signed order
        for one worker can't be replayed against another.

        payload = {"worker_id": "<target>", "name": "<label>", "issued_at":
                   <unix>, "reason": "...", "sig": "<base64 ed25519>"}

        On success the node persists a ``banned`` flag and restarts into the CLI
        boot gate, which parks it OFF the band (no transport, no announce). The
        flag survives reboots; clear ``~/.rook-band-worker/banned`` and restart
        to rejoin.

        LIMITATION: this stops a COOPERATIVE worker (one running this code). A
        compromised node running modified code can ignore it and keep using the
        shared PSK — the controller still hides/ignores it and denies it commands
        and updates, but truly evicting a hostile node needs per-worker identity
        or a PSK rotation."""
        from .._update_verify import verify_manifest
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be an object"}
        if not verify_manifest(payload):
            # Fail closed: unsigned or wrong key → refuse.
            return {"ok": False, "error": "invalid or missing signature"}
        mine = _my_worker_id()
        target = payload.get("worker_id")
        if target and mine and target != mine:
            return {"ok": False, "error": "worker_id mismatch (not this worker)"}
        issued = payload.get("issued_at")
        if isinstance(issued, (int, float)) and abs(time.time() - issued) > 86400:
            # Reject ancient captured orders; window is generous for clock skew.
            return {"ok": False, "error": "signed order too old"}
        _WORKER_DIR.mkdir(parents=True, exist_ok=True)
        _BANNED.write_text(json.dumps({
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

    @capability("status")
    def _status(self) -> dict:
        from .._build_info import as_dict as _build
        return {
            "pid": os.getpid(),
            "python": sys.executable,
            "argv": sys.argv,
            "pyz": str(_PYZ),
            "pyz_exists": _PYZ.exists(),
            "supervisor": _supervisor(),
            **_build(),
        }

    # -- lifecycle -----------------------------------------------------------

    @capability("restart")
    async def _restart(self) -> dict:
        """Restart this worker with its current config, via the service manager
        (or re-exec). Kill-safe."""
        self._schedule_restart(self._current_argv())
        return {"ok": True, "supervisor": _supervisor(), "restarting": True}

    @capability("reconfigure")
    async def _reconfigure(self, hub: str | None = None, psk: str | None = None,
                           name: str | None = None, restart: bool = True) -> dict:
        """Repoint this worker at a new band (hub/psk) and/or rename it, then
        restart. The new config is persisted to the systemd unit so it survives
        reboots. Used to migrate bands without a curl|bash reinstall.
        """
        if hub is None and psk is None and name is None:
            return {"ok": False, "error": "nothing to change (pass hub/psk/name)"}
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

    @capability("update")
    async def _update(self, url: str | None = None, hub: str | None = None,
                      psk: str | None = None, name: str | None = None) -> dict:
        """Fetch a fresh worker bundle from ``url`` (if given), optionally
        repoint at a new band, then restart. Kill-safe."""
        notes = []
        if url:
            try:
                await self._download(url, _PYZ)
                notes.append(f"bundle updated from {url}")
            except Exception as e:
                return {"ok": False, "error": f"download failed: {type(e).__name__}: {e}"}
        new_argv = self._current_argv(hub=hub, psk=psk, name=name)
        persisted = self._persist(new_argv)
        self._schedule_restart(new_argv)
        return {"ok": True, "supervisor": _supervisor(), "notes": notes,
                "persisted_unit": persisted, "restarting": True}

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
        the delivery channel is untrusted; only the signature grants the swap."""
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
        _WORKER_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _WORKER_DIR / "band-worker.pyz.dl"
        try:
            tmp.write_bytes(await self._http_get(pyz_url, timeout=120))
        except Exception as e:
            return {"ok": False, "error": f"bundle download failed: {type(e).__name__}: {e}"}
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
            os.replace(tmp, _PYZ)
            os.chmod(_PYZ, 0o755)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": f"swap failed: {type(e).__name__}: {e}"}

        self._write_state({"stage": "swapped", "target_build": target,
                           "prev_build": from_build, "attempts": 0, "at": int(time.time())})
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
            return json.loads(_STATE.read_text())
        except Exception:
            return None

    def _write_state(self, d: dict) -> None:
        _WORKER_DIR.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(d))

    def _clear_state(self) -> None:
        _STATE.unlink(missing_ok=True)

    async def _boot_check(self) -> None:
        """On startup, resolve any in-flight update: confirm the new build is
        healthy (and drop .prev), or roll back after too many failed boots."""
        from .._build_info import BUILD
        st = self._read_state()
        if not st:
            return
        if st.get("stage") not in ("swapped", "verifying"):
            self._clear_state()
            return
        target = st.get("target_build")
        if BUILD == target:
            st["attempts"] = int(st.get("attempts", 0)) + 1
            st["stage"] = "verifying"
            if st["attempts"] > _MAX_BOOT_ATTEMPTS:
                log.error("build %s failed to stay healthy after %d boots; rolling back",
                          target, st["attempts"] - 1)
                await self._rollback(st)
                return
            self._write_state(st)
            self._health_task = asyncio.create_task(self._finalize_health(target))
        else:
            # Running some other build than intended (manual reconfigure, etc.).
            log.info("update state target=%s but running build=%s; clearing", target, BUILD)
            self._clear_state()

    async def _finalize_health(self, target: int) -> None:
        try:
            await asyncio.sleep(_HEALTH_SECS)
        except asyncio.CancelledError:
            return
        st = self._read_state()
        if st and st.get("target_build") == target:
            self._clear_state()
            try:
                _PREV.unlink(missing_ok=True)
            except Exception:
                pass
            log.info("update to build %s verified healthy", target)

    async def _rollback(self, st: dict) -> None:
        if not _PREV.exists():
            log.error("rollback requested but no .prev bundle; clearing state")
            self._clear_state()
            return
        try:
            import shutil
            shutil.copy2(_PREV, _PYZ)
            os.chmod(_PYZ, 0o755)
        except Exception:
            log.exception("rollback copy failed")
            self._clear_state()
            return
        self._write_state({"stage": "rolledback", "target_build": st.get("prev_build"),
                           "prev_build": st.get("target_build"), "attempts": 0,
                           "at": int(time.time())})
        log.warning("rolled back to previous bundle; restarting")
        self._schedule_restart(self._current_argv())

    # -- internals -----------------------------------------------------------

    def _current_argv(self, hub: str | None = None, psk: str | None = None,
                      name: str | None = None) -> list[str]:
        """Reconstruct the full launch command (python + pyz + args), applying
        any hub/psk/name overrides. Preserves every other existing flag."""
        argv = [sys.executable, *sys.argv]  # sys.argv[0] is the pyz path

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
        """Write the new launch command into the systemd --user unit so it
        survives restarts/reboots. No-op (returns False) when not systemd."""
        if not _UNIT.exists():
            return False
        try:
            text = _UNIT.read_text()
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
            _UNIT.write_text("\n".join(out) + "\n")
            return True
        except Exception:
            log.exception("failed to persist unit")
            return False

    async def _download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".pyz.new")

        def _fetch() -> None:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            os.chmod(dest, 0o755)

        await asyncio.get_event_loop().run_in_executor(None, _fetch)

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
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass


PLUGIN = SelfUpdatePlugin
