"""Hub key hierarchy (``docs/design/permissions.md`` 4.1, 4.5).

* **Root key**: the existing OTA signing key (``rook.remote.update_keys``).
  Its public half is baked into every worker bundle. It signs OTA manifests,
  deauth orders and, here, the ``is_hub`` grant for the operational key.
* **Operational key** (``hub-op-key`` beside the root key, mode 0600): signs
  call tickets and hub announces. Rotated every 30 days; the previous key is
  kept (and its grant announced) for a 24 h overlap so in-flight workers keep
  verifying.

Grants are issued in memory by whichever hub process needs them (dashboard or
MCP bridge; both can read the root key), cover every band that process has
seen, live 7 days and are renewed daily. Nothing here raises into the call
path: a missing root key simply disables signing (calls go out without
tickets and the hub alerts).
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..core import authz

log = logging.getLogger("rook.hub.keys")

OP_KEY_NAME = "hub-op-key"
OP_KEY_ROTATE_SECS = 30 * 86400
OP_KEY_OVERLAP_SECS = 86400
GRANT_RENEW_SECS = 86400


def _root_key():
    try:
        from ..remote.update_keys import load_signing_key
        return load_signing_key()
    except Exception:
        log.exception("could not load the root (update signing) key")
        return None


def _key_dir() -> Path:
    from ..remote.update_keys import key_path
    return key_path().parent


def _read_key(path: Path):
    from nacl.signing import SigningKey
    return SigningKey(base64.b64decode(path.read_text().strip()))


def _write_key(path: Path):
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(bytes(sk)).decode("ascii") + "\n")
    return sk


class HubSigner:
    """Op key + root-signed ``is_hub`` grants + tickets for one hub process."""

    RETRY_SECS = 60.0          # look for a root key created after start-up
    RELOAD_SECS = 86400.0      # re-check op-key rotation in long-running processes

    def __init__(self, root_sk=None, op_dir: str | Path | None = None, *,
                 now: float | None = None) -> None:
        self._lock = threading.Lock()
        self._explicit_root = root_sk
        self._op_dir = Path(op_dir) if op_dir else None
        self.root = None
        self.enabled = False
        self.anchor = None
        self.op = None
        self.prev = None
        self.kid = None
        self._grants: dict[str, dict] = {}   # kid -> current grant
        self._bands: set[str] = set()
        self._seq = 0
        self._attempt = 0.0
        self._setup(now)

    def _setup(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._attempt = now
        root = self._explicit_root if self._explicit_root is not None else _root_key()
        if root is None:
            if self.root is None:
                log.warning("ROOK AUTHZ ALERT: no root signing key on this hub; calls go "
                            "out without tickets and no is_hub grant is announced")
            return
        try:
            self._load_op(self._op_dir or _key_dir(), now)
        except Exception:
            log.exception("ROOK AUTHZ ALERT: hub op key unavailable; signing disabled")
            self.enabled = False
            return
        self.root, self.anchor = root, authz.pub_b64(root)
        self.enabled = True

    def ready(self, now: float | None = None) -> bool:
        """Whether signing works now. Retries a missing root key (the
        dashboard creates it on first start, possibly after this process
        started) and re-checks op-key rotation once a day."""
        now = now if now is not None else time.time()
        wait = self.RELOAD_SECS if self.enabled else self.RETRY_SECS
        if now - self._attempt > wait:
            with self._lock:
                if now - self._attempt > wait:
                    old = self.kid
                    self._setup(now)
                    if self.kid != old:
                        self._grants.clear()
        return self.enabled

    # -- op key ---------------------------------------------------------------
    def _load_op(self, d: Path, now: float | None) -> None:
        now = now if now is not None else time.time()
        path, prev = d / OP_KEY_NAME, d / (OP_KEY_NAME + ".prev")
        if path.exists() and now - path.stat().st_mtime > OP_KEY_ROTATE_SECS:
            try:
                os.replace(path, prev)
                os.utime(prev, (now, now))  # the overlap window starts at rotation
                log.info("hub op key rotated")
            except OSError:
                log.exception("hub op key rotation failed; keeping the old key")
        if not path.exists():
            try:
                _write_key(path)
            except FileExistsError:
                pass  # another hub process created it first
        self.op = _read_key(path)
        if prev.exists() and now - prev.stat().st_mtime <= OP_KEY_OVERLAP_SECS:
            try:
                self.prev = _read_key(prev)
            except Exception:
                self.prev = None
        self.kid = authz.key_id(authz.pub_b64(self.op))

    # -- grants ---------------------------------------------------------------
    def note_band(self, band: str) -> None:
        if band:
            self._bands.add(band)

    def _grant_for(self, sk, band: str, now: float) -> dict:
        kid = authz.key_id(authz.pub_b64(sk))
        g = self._grants.get(kid)
        fresh = (g is not None and band in g["scope"]["bands"]
                 and now - g["iat"] < GRANT_RENEW_SECS and g["exp"] > now)
        if not fresh:
            self._bands.add(band)
            g = authz.make_grant(self.root, authz.pub_b64(sk), "is_hub", self._bands,
                                 name=authz.RESERVED_HUB_NAME, now=now)
            self._grants[kid] = g
        return g

    def grant(self, band: str, now: float | None = None) -> dict | None:
        if not self.ready(now):
            return None
        with self._lock:
            return self._grant_for(self.op, band, now if now is not None else time.time())

    def grants(self, band: str, now: float | None = None) -> list[dict]:
        """Grants to announce on ``band``: the current op key's, plus the
        previous key's during the rotation overlap."""
        if not self.ready(now):
            return []
        now = now if now is not None else time.time()
        with self._lock:
            out = [self._grant_for(self.op, band, now)]
            if self.prev is not None:
                out.append(self._grant_for(self.prev, band, now))
            return out

    # -- announces ------------------------------------------------------------
    def decorate_announce(self, msg: dict, band: str, now: float | None = None) -> dict:
        """Add the ``is_hub`` grant(s) and an op-key signature (proof of
        possession, 4.4) to a hub announce."""
        if not self.ready(now):
            return msg
        now = now if now is not None else time.time()
        grants = self.grants(band, now)
        with self._lock:
            self._seq = max(self._seq + 1, int(now * 1000))
            seq = self._seq
        msg = {**msg, "grants": grants}
        return authz.sign_announce(self.op, msg, seq=seq, now=now)

    # -- tickets --------------------------------------------------------------
    def ticket(self, *, band: str, principal: str, via: list[str], cap: str, target: str,
               msg_id: str, args: Any, tier: str, rev: int, inline_grant: bool = False,
               now: float | None = None) -> dict | None:
        if not self.ready(now):
            return None
        now = now if now is not None else time.time()
        grant = self.grant(band, now) if inline_grant else None
        return authz.make_ticket(self.op, self.kid, principal=principal, via=via, cap=cap,
                                 target=target, msg_id=msg_id, args=args, tier=tier,
                                 rev=rev, now=now, grant=grant)



def deauth_payload(root_sk, worker_id: str, name: str, reason: str,
                   now: float | None = None) -> dict:
    """A deauth order both generations of worker accept: the v2 order (own
    signature domain, required worker_id/issued_at) nested under ``v2``,
    wrapped in a legacy v1 body signed the old way for workers that predate
    v2. New workers verify only the v2 order (permissions 6.3)."""
    from ..remote.update_keys import _canonical_payload
    v2 = authz.make_deauth(root_sk, worker_id, name, reason, now=now)
    body = {"worker_id": worker_id, "name": name, "issued_at": v2["issued_at"],
            "reason": v2["reason"], "v2": v2}
    sig = root_sk.sign(_canonical_payload(body)).signature
    return {**body, "sig": base64.b64encode(sig).decode("ascii")}
