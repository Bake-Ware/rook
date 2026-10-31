"""Worker-side enforcement for core v2 (docs/spec/core-v2.md sections 5.2,
5.4, 6.3, 7 and 9).

Everything a worker does to decide whether to run a call, with no callback
to the hub except elevation:

- :class:`ReplayStore`: the nonce replay cache, file-backed and bounded.
- :class:`TrustStore`: the anchor (pinned home hub, a read-only file) and
  the home-hub-signed memberships with their per-hub ceilings.
- :class:`WorkerGate`: hub operational keys, hub contact, version per band,
  the ticket check (``wire.verify_ticket``), elevation leases and the
  in-call elevation flow.
- :func:`launch`: the one way plugins start processes, as the normal user
  unless the call holds a lease.

No networking: the host hands in requests, hub announces and an ``ask``
coroutine that carries ``core.elevate`` to the hub.

TODO(castling:worker-host): the host routes every request through
``WorkerGate.check`` and ``WorkerGate.execute``, answers ``core.lease.drop``
with ``drop_leases`` and calls ``tick`` from its heartbeat loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import CORE_VERSION, rights, wire

HUB_LOST = 90            # seconds without hub contact before the hub is lost (5.4)
ANNOUNCE_SKEW = 90       # |now - ts| allowed on a hub announce (5.4)
ELEVATE_TIMEOUT = 10     # seconds to wait for core.elevate (7.2)
COLD_START = 150         # refuse every ticket this long when the replay cache was lost (6.3)
REPLAY_MAX = 200_000     # nonces held at most (~18 MB); see ReplayStore
REPLAY_FLOOR = 600       # a nonce is kept at least this long, full or not
BUCKET = 10              # seconds of expiry per replay-cache set
ELEVATIONS = ("never", "may", "upfront")

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HUB_ID = re.compile(r"[0-9a-f]{16}")


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# -- replay cache (6.3) ------------------------------------------------------

class ReplayStore:
    """Accepted nonces, each kept until about ``max(exp + SKEW, now +
    REPLAY_KEEP)``: in sets per BUCKET seconds of that time (a nonce is a
    128-bit int, ~90 bytes held), appended to ``path`` as ``<nonce> <until>``
    lines, compacted when the file holds mostly dead lines. ``path=None``
    keeps memory only. Same ``seen``/``add`` interface as
    ``wire.ReplayCache``.

    Bounded by ``max_entries``, not by load: when full, whole buckets whose
    nonces are all at least REPLAY_FLOOR (10 minutes) old go, oldest first.
    The 15 minutes are kept while there is room, 10 always (castling-protocol:
    "remembers used ticket nonces for 10-20 minutes"), well past spec 6.3's
    MUST of ``exp + 30``. Refused only beyond ``max_entries`` nonces younger
    than 10 minutes: ~330 calls/s sustained for 10 minutes at 200,000.

    The store refuses (``seen`` is True) for COLD_START seconds after it
    starts when it can't vouch for earlier nonces: memory only, or a file
    that couldn't be read whole (a torn last line counts). ``add`` raises
    when it can't record, and the caller refuses the call.
    """

    def __init__(self, path: str | Path | None, now: int | None = None, max_entries: int = REPLAY_MAX):
        now = _now(now)
        self.path = Path(path) if path is not None else None
        self.max = max_entries
        self.buckets: dict[int, set[int]] = {}      # until // BUCKET -> nonces
        self.size = 0
        self.ready_at = now + COLD_START
        self._lines = 0
        if self.path is None:
            return
        try:
            text = self.path.read_text(encoding="ascii") if self.path.exists() else ""
            whole = True
        except (OSError, UnicodeDecodeError):
            text, whole = "", False
        for line in text.splitlines():
            nonce, _, until = line.partition(" ")
            if not _HEX32.fullmatch(nonce) or not until.isdigit():
                whole = False
                continue
            if int(until) > now and not self._has(int(nonce, 16)):
                self._put(int(nonce, 16), int(until))
        if whole:
            self.ready_at = now
        try:
            self._compact()
        except OSError:
            pass                 # add() fails too, so every call is refused

    def _has(self, n: int) -> bool:
        return any(n in s for s in self.buckets.values())

    def _put(self, n: int, until: int) -> None:
        self.buckets.setdefault(until // BUCKET, set()).add(n)
        self.size += 1

    def _drop(self, b: int) -> None:
        self.size -= len(self.buckets.pop(b))

    def _prune(self, now: int) -> None:
        for b in [b for b in self.buckets if (b + 1) * BUCKET <= now]:
            self._drop(b)

    def seen(self, nonce: str, now: int) -> bool:
        if now < self.ready_at:
            return True
        self._prune(now)
        return self._has(int(nonce, 16))

    def add(self, nonce: str, exp: int, now: int) -> None:
        self._prune(now)
        while self.size >= self.max:
            # Every nonce in bucket b was accepted at or before its end - KEEP.
            oldest = min(self.buckets, default=None)
            if oldest is None or (oldest + 1) * BUCKET - wire.REPLAY_KEEP + REPLAY_FLOOR > now:
                raise OverflowError("replay cache full")
            self._drop(oldest)
        until = max(exp + wire.SKEW, now + wire.REPLAY_KEEP)
        if self.path is not None:
            with open(self.path, "a", encoding="ascii") as f:
                f.write(f"{nonce} {until}\n")
                f.flush()
                os.fsync(f.fileno())     # a fast reboot must not reopen the window
            self._lines += 1
        self._put(int(nonce, 16), until)
        if self._lines > 2 * self.size + 1024:
            self._compact()

    def _compact(self) -> None:
        if self.path is None:
            return
        _write_atomic(self.path, "".join(f"{n:032x} {(b + 1) * BUCKET}\n"
                                         for b, ns in self.buckets.items() for n in ns))
        self._lines = self.size


# -- trust store (9.2, 9.3) --------------------------------------------------

@dataclass(frozen=True)
class Member:
    """One trusted (hub, band) pair and what it may do on this worker."""

    hub: str
    root: str
    band: str
    role: str
    ceiling: dict


class TrustStore:
    """The anchor plus memberships. The worker holds no signing key, so it
    can neither create nor widen an entry.

    ``anchor_path`` holds ``{"hub", "root", "band"}``, written at enrollment
    by an administrator; it is only ever read here. ``anchor_protected`` says
    whether this process can't write it (report it; a user install can't do
    better). ``memberships_path`` holds the accepted membership documents,
    each with the ``rook-hubkey`` grant that certified its signing key when
    it wasn't the root; every document is verified again on load, so nobody
    can forge or widen one. The worker writes that file itself, so its own
    account could delete entries (a narrowed home ceiling, a removal); that
    account can already do whatever its calls can, and only the anchor
    needs protecting from it (spec 15).
    """

    def __init__(self, anchor_path: str | Path, memberships_path: str | Path | None, worker_id: str):
        self.worker_id = worker_id
        self.anchor_path = Path(anchor_path)
        anchor = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        hub, root, band = anchor.get("hub"), anchor.get("root"), anchor.get("band")
        if not (isinstance(hub, str) and isinstance(band, str) and _HEX32.fullmatch(band)):
            raise ValueError("anchor needs hub, root and a 32-hex band")
        try:
            if wire.key_id(root) != hub:
                raise ValueError("anchor hub is not key_id(root)")
        except (AttributeError, TypeError, ValueError, IndexError) as e:
            raise ValueError(f"anchor root key: {e}") from None
        self.home = Member(hub, root, band, "home", {"grants": ["*"]})
        self.anchor_protected = not os.access(self.anchor_path, os.W_OK)
        self.path = Path(memberships_path) if memberships_path is not None else None
        self.docs: dict[tuple[str, str], dict] = {}   # (hub, band) -> {"doc", "hubkey"}
        self.skipped: list[str] = []                  # why stored documents didn't load
        if self.path is not None and self.path.exists():
            for rec in json.loads(self.path.read_text(encoding="utf-8")).get("memberships", []):
                if not isinstance(rec, dict):
                    self.skipped.append("malformed")
                    continue
                ok, reason = self._verify(rec.get("doc"), rec.get("hubkey"), None)
                if ok:
                    self.docs[(rec["doc"]["hub"], rec["doc"]["band"])] = rec
                else:
                    self.skipped.append(reason)

    def _home_keys(self, hubkey: Any, at: int) -> dict:
        keys = {self.home.hub: self.home.root}
        op = wire.verify_hubkey(hubkey, self.home.root, self.home.band, at) if hubkey else None
        if op:
            keys[wire.key_id(op)] = op
        return keys

    def _verify(self, doc: Any, hubkey: Any, op_keys: dict | None) -> tuple[bool, str]:
        """Check a membership. ``op_keys`` are the home hub's currently
        verified operational keys; None re-checks a stored document against
        the grant that certified its key when it was issued."""
        if not isinstance(doc, dict):
            return False, "malformed"
        if op_keys is None:
            iat = doc.get("iat")
            keys = self._home_keys(hubkey, iat) if isinstance(iat, int) else {self.home.hub: self.home.root}
        else:
            keys = {self.home.hub: self.home.root, **op_keys}
        ok, reason = wire.verify_membership(doc, self.home.hub, keys, self.worker_id)
        if ok and not (isinstance(doc.get("band"), str) and _HEX32.fullmatch(doc["band"])
                       and isinstance(doc.get("iat"), int) and not isinstance(doc["iat"], bool)):
            return False, "malformed"
        return ok, reason

    def set_membership(self, doc: Any, op_keys: dict, hubkey: Any = None) -> tuple[bool, str]:
        """Accept a document from ``core.trust.set`` (the ticket check already
        made sure the home hub sent it). ``op_keys`` are the home hub's
        verified operational keys on the home band; ``hubkey`` is the grant
        for the signing key, kept so the document verifies after a restart.
        Refuses a document not newer than the one held for that pair."""
        ok, reason = self._verify(doc, hubkey, op_keys)
        if not ok:
            return False, reason
        pair = (doc["hub"], doc["band"])
        held = self.docs.get(pair)
        if held and doc["iat"] <= held["doc"]["iat"]:
            return False, "not_newer"
        signed_by_root = doc["kid"] == self.home.hub
        rec = {"doc": doc, "hubkey": None if signed_by_root else hubkey}
        if not signed_by_root and not self._verify(doc, hubkey, None)[0]:
            return False, "no_grant"     # couldn't be checked again after a restart
        self.docs[pair] = rec
        self._save()
        return True, "ok"

    def _save(self) -> None:
        if self.path is not None:
            recs = [self.docs[k] for k in sorted(self.docs)]
            _write_atomic(self.path, json.dumps({"memberships": recs}, indent=1))

    def member(self, hub: str, band: str) -> Member | None:
        """The trusted entry for (hub, band), or None (also for ``removed``)."""
        rec = self.docs.get((hub, band))
        if rec:
            d = rec["doc"]
            return None if d["role"] == "removed" else Member(d["hub"], d["root"], band, d["role"], d["ceiling"])
        return self.home if (hub, band) == (self.home.hub, self.home.band) else None

    def members(self, band: str) -> list[Member]:
        hubs = {h for h, b in self.docs if b == band}
        if band == self.home.band:
            hubs.add(self.home.hub)
        return [m for m in (self.member(h, band) for h in sorted(hubs)) if m]

    def describe(self) -> dict:
        """``core.trust.get``: the anchor and the memberships, no PSKs."""
        return {"anchor": {"hub": self.home.hub, "band": self.home.band, "protected": self.anchor_protected},
                "memberships": [{k: r["doc"][k] for k in ("hub", "band", "role", "ceiling", "by", "iat")
                                 if k in r["doc"]} for r in (self.docs[p] for p in sorted(self.docs))]}


# -- elevation errors --------------------------------------------------------

class NeedsElevation(Exception):
    """Raised by a cap (or the host on its behalf) that hit access denied.
    TODO(castling:wiring): re-export as the kit's elevation error."""


class ElevationError(Exception):
    """Elevation failed; the call replies ``elevation: <reason>``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# -- the gate ----------------------------------------------------------------

Ask = Callable[[str, dict], Awaitable[dict]]
"""``ask(band, args)`` sends ``core.elevate`` with ``args`` to the hub node
on ``band`` and returns its reply ``{ok, result | error}``; raises when the
hub can't be reached."""


@dataclass(frozen=True)
class Call:
    """A request that passed every worker check (6.3)."""

    request: dict
    ticket: dict
    band: str
    member: Member
    meta: dict          # the cap's manifest data: risk, effects, elevation

    def context(self, lease: str | None = None) -> dict:
        """The verified call context handed to the cap (6.6)."""
        t = self.ticket
        return {"person": t.get("person"), "agent": t.get("agent"), "session": t["session"],
                "band": self.band, "why": t["why"], "hub": t["hub"], "call": t["call"],
                "risk": "admin" if lease else self.meta.get("risk", "admin"), "lease": lease}


def reply(request_id: str, worker_id: str, *, result: Any = None, error: str | None = None,
          elevated: str | None = None) -> dict:
    out: dict = {"id": request_id, "from": worker_id, "ok": error is None}
    if error is None:
        out["result"] = result
    else:
        out["error"] = error
    if elevated:
        out["elevated"] = elevated
    return out


class WorkerGate:
    """Per-worker enforcement state: trust, hub keys and contact, version per
    band, the replay cache and leases. ``caps`` maps each cap the worker
    serves to its manifest data (``risk``, ``effects``, ``elevation``).
    Methods take ``now`` (Unix seconds) for tests; default is the clock."""

    def __init__(self, worker_id: str, name: str, trust: TrustStore, replay: ReplayStore,
                 caps: dict[str, dict], ask: Ask | None = None):
        self.worker_id, self.name = worker_id, name
        self.trust, self.replay, self.caps = trust, replay, caps
        self.ask = ask
        self.keys: dict[tuple[str, str], dict[str, tuple[str, int, dict]]] = {}  # (hub, band) -> kid -> (pub, until, grant)
        self.contact: dict[tuple[str, str], int] = {}
        self.seq: dict[tuple[str, str, str], int] = {}                      # (hub, band, kid) -> last seq
        self.versions: dict[str, str | None] = {}                           # band -> negotiated core
        self.leases: dict[str, dict] = {}
        self._lock = threading.Lock()

    # hub keys and contact (5.2, 5.4)

    def add_hubkey(self, band: str, grant: Any, now: int | None = None) -> bool:
        """Learn an operational key from a ``rook-hubkey`` grant that arrived
        on ``band``, if a hub trusted there certified it."""
        now = _now(now)
        hub = grant.get("hub") if isinstance(grant, dict) else None
        m = self.trust.member(hub, band) if isinstance(hub, str) else None
        pub = wire.verify_hubkey(grant, m.root, band, now) if m else None
        if not pub:
            return False
        ring = self.keys.setdefault((hub, band), {})
        kid = wire.key_id(pub)
        until = grant["exp"] + wire.GRANT_GRACE
        if until >= ring.get(kid, ("", 0, {}))[1]:
            ring[kid] = (pub, until, grant)
        return True

    def op_keys(self, hub: str, band: str, now: int | None = None) -> dict[str, str]:
        now = _now(now)
        ring = self.keys.get((hub, band), {})
        for kid in [k for k, v in ring.items() if v[1] < now]:
            del ring[kid]
        return {kid: v[0] for kid, v in ring.items()}

    def hub_announce(self, band: str, msg: Any, now: int | None = None) -> bool:
        """Accept a signed hub announce (5.4): hub contact, plus the hub's
        core version for this band. Returns whether it was accepted."""
        now = _now(now)
        with self._lock:
            self._prune(now)
            if not isinstance(msg, dict) or msg.get("typ") != "rook-announce" or msg.get("v") != 2:
                return False
            if msg.get("hubkey"):
                self.add_hubkey(band, msg["hubkey"], now)
            hub, kid, ts, seq = msg.get("hub"), msg.get("kid"), msg.get("ts"), msg.get("seq")
            if not (isinstance(hub, str) and isinstance(kid, str) and msg.get("band") == band
                    and isinstance(ts, int) and isinstance(seq, int)):
                return False
            pub = self.op_keys(hub, band, now).get(kid)
            if not pub or not wire.verify_sig(pub, msg) or abs(now - ts) > ANNOUNCE_SKEW:
                return False
            if seq <= self.seq.get((hub, band, kid), -1):
                return False
            self.seq[(hub, band, kid)] = seq
            self.contact[(hub, band)] = now
            self.versions[band] = wire.negotiate(CORE_VERSION, msg.get("core"))
            return True

    def lost(self, hub: str, band: str, now: int | None = None) -> bool:
        return _now(now) - self.contact.get((hub, band), -10**12) > HUB_LOST

    # the ticket check (6.3)

    def check(self, request: Any, band: str, now: int | None = None) -> Call | dict | None:
        """Decide on a request that arrived on ``band``. Returns a
        :class:`Call` to run, a refusal reply to send, or None when the
        worker must stay silent (not addressed to it)."""
        now = _now(now)
        if not isinstance(request, dict) or request.get("target") != self.worker_id:
            return None
        rid = request.get("id") if isinstance(request.get("id"), str) else ""
        with self._lock:
            self._prune(now)
            if band in self.versions and self.versions[band] is None:
                return reply(rid, self.worker_id, error="denied by worker: incompatible")
            ticket = request.get("ticket")
            if isinstance(ticket, dict) and ticket.get("hubkey"):
                self.add_hubkey(band, ticket["hubkey"], now)
            trust = {m.hub: {"band": band, "role": m.role, "ceiling": m.ceiling,
                             "keys": self.op_keys(m.hub, band, now)} for m in self.trust.members(band)}
            state = {"worker_id": self.worker_id, "name": self.name, "band": band,
                     "trust": trust, "caps": self.caps}
            try:
                ok, reason = wire.verify_ticket(ticket, request, state, now, self.replay)
            except (OSError, OverflowError):
                ok, reason = False, "replay"     # couldn't record the nonce: refuse (6.3)
            if not ok:
                err = (f"unknown capability: {request.get('cap')}" if reason == "unknown_cap"
                       else f"denied by worker: {reason}")
                return reply(rid, self.worker_id, error=err)
            self.contact[(ticket["hub"], band)] = now
            return Call(request, ticket, band, self.trust.member(ticket["hub"], band),
                        self.caps[ticket["cap"]])

    def trust_set(self, doc: Any, now: int | None = None) -> tuple[bool, str]:
        """``core.trust.set`` (only reaches here from the home hub): accept a
        membership and drop the leases of the hub it changes."""
        now = _now(now)
        home = self.trust.home
        with self._lock:
            keys = self.op_keys(home.hub, home.band, now)
            kid = doc.get("kid") if isinstance(doc, dict) else None
            grant = self.keys.get((home.hub, home.band), {}).get(kid, (None, 0, None))[2]
            ok, reason = self.trust.set_membership(doc, keys, grant)
            if ok:
                for lid in [lid for lid, l in self.leases.items()
                            if (l["hub"], l["band"]) == (doc["hub"], doc["band"])]:
                    del self.leases[lid]
            return ok, reason

    # leases (7.3, 7.4)

    def _prune(self, now: int) -> None:
        for lid, lease in list(self.leases.items()):
            if lease["exp"] < now or self.lost(lease["hub"], lease["band"], now):
                del self.leases[lid]

    def tick(self, now: int | None = None) -> None:
        """Drop expired leases and every lease of a hub this worker lost."""
        with self._lock:
            self._prune(_now(now))

    def lease_for(self, ticket: dict, now: int | None = None) -> dict | None:
        """A held lease with exactly this ticket's binding, if any."""
        now = _now(now)
        with self._lock:
            self._prune(now)
            for lease in self.leases.values():
                if all(lease.get(k) == ticket.get(k)
                       for k in ("hub", "band", "worker", "person", "agent", "session")):
                    return lease
        return None

    def drop_leases(self, hub: str, band: str, args: dict) -> dict:
        """``core.lease.drop`` from ``hub`` on ``band``: by ``lease`` id, or
        every lease of that hub matching all given ``person``/``agent``/
        ``session``. With none of those, drops all of that hub's leases."""
        keys = [k for k in ("person", "agent", "session") if k in args]
        with self._lock:
            gone = [lid for lid, lease in self.leases.items()
                    if lease["hub"] == hub and lease["band"] == band
                    and (lid == args["lease"] if "lease" in args
                         else all(lease.get(k) == args[k] for k in keys))]
            for lid in gone:
                del self.leases[lid]
        return {"dropped": len(gone)}

    # elevation (7.2)

    def needs_upfront(self, call: Call) -> bool:
        el = call.meta.get("elevation", "never")
        return el == "upfront" or (call.ticket.get("elevate") is True and el != "never")

    async def elevate(self, call: Call, reason: str, now: int | None = None) -> dict:
        """Return a lease for ``call``: a held one, or a new one from the hub.
        Raises :class:`ElevationError` (fail closed)."""
        held = self.lease_for(call.ticket, now)
        if held:
            return held
        probe = {"cap": "core.elevate", "risk": "admin", "effects": [],
                 "target": call.request.get("appliance") or self.name}
        if not rights.permits(call.member.ceiling, probe):
            raise ElevationError("outside ceiling")
        if self.ask is None:
            raise ElevationError("hub unreachable")
        try:
            answer = await asyncio.wait_for(self.ask(call.band, {"ticket": call.ticket, "reason": reason}),
                                            ELEVATE_TIMEOUT)
        except Exception:
            raise ElevationError("hub unreachable") from None
        if not isinstance(answer, dict) or not answer.get("ok"):
            err = answer.get("error") if isinstance(answer, dict) else None
            raise ElevationError("outside ceiling" if err == "elevation: outside ceiling" else "denied")
        lease = (answer.get("result") or {}).get("lease") if isinstance(answer.get("result"), dict) else None
        now = _now(now)
        with self._lock:
            ok, why = wire.verify_lease(lease, call.ticket, self.op_keys(call.ticket["hub"], call.band, now), now)
            if not ok or not _HEX32.fullmatch(str(lease.get("id"))):
                raise ElevationError(f"invalid lease ({why if not ok else 'id'})")
            self.contact[(lease["hub"], call.band)] = now
            self.leases[lease["id"]] = lease
        return lease

    async def execute(self, call: Call, action: Callable[[dict | None], Awaitable[Any]],
                      now: int | None = None) -> dict:
        """Run a checked call. ``action(lease)`` runs the cap, elevated when
        ``lease`` is a lease; it raises :class:`NeedsElevation` or
        ``PermissionError`` on access denied. Up-front caps get their lease
        first; ``may`` caps re-run once with one. Returns the reply; other
        exceptions from ``action`` are the host's to turn into replies."""
        rid = call.request["id"]
        lease = None
        try:
            if self.needs_upfront(call):
                lease = await self.elevate(call, "upfront", now)
            try:
                result = await action(lease)
            except (NeedsElevation, PermissionError):
                if lease is not None or call.meta.get("elevation", "never") != "may":
                    raise
                lease = await self.elevate(call, "access_denied", now)
                result = await action(lease)
        except ElevationError as e:
            return reply(rid, self.worker_id, error=f"elevation: {e.reason}")
        return reply(rid, self.worker_id, result=result, elevated=lease["id"] if lease else None)


# -- process launch (7.1) ----------------------------------------------------

class LaunchUnsupported(Exception):
    """No elevated launch on this platform yet."""


class LaunchRefused(Exception):
    """The worker runs as an administrator and has no normal account to drop
    to. Not a PermissionError on purpose: it must never trigger elevation."""


DENIED_MARKERS = ("permission denied", "operation not permitted", "access is denied",
                  "must be run as root", "are you root", "requires root", "eacces", "eperm",
                  "requires elevation", "administrator privileges")


def looks_denied(code: int | None, stderr: str | bytes = "") -> bool:
    """Best-effort access-denied detection for a finished process (7.2):
    exit 126 (couldn't execute) or a well-known message on stderr. Only for
    ``elevation: may`` caps, where a false positive costs one re-run."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    text = stderr.lower()
    return code == 126 or (code not in (0, None) and any(m in text for m in DENIED_MARKERS))


def _is_admin(platform: str) -> bool:
    if platform == "win32":
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def launch_plan(argv: list[str], lease: dict | None, *, platform: str = sys.platform,
                admin: bool | None = None, run_as: str | None = None) -> tuple[list[str], dict]:
    """The argv and ``subprocess`` keywords for one process launch.

    Linux: a normal call runs as the worker's own account; a worker running
    as root drops to ``run_as`` (required: refusing beats running a normal
    call as root). An elevated call runs through ``sudo -n`` (no prompt:
    the account needs a sudoers rule, which ``rook`` install sets up), or
    directly when the worker already is root.

    Windows (design, not built): the worker service runs as LocalSystem and
    starts normal calls with the signed-in user's filtered (non-admin) token
    via CreateProcessAsUser; elevated calls use the linked full token. A
    per-user worker without a service can't elevate without a UAC prompt,
    so it reports ``elevation: unsupported``. macOS would match Linux
    (``sudo -n``) but is untested, so it is stubbed too. Until then a
    non-Linux worker running as an administrator refuses normal launches.
    """
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    admin = _is_admin(platform) if admin is None else admin
    if not platform.startswith("linux"):
        if lease is not None:
            raise LaunchUnsupported(f"elevated launch is not built for {platform} yet")
        if admin:
            raise LaunchRefused(f"worker runs as an administrator; dropping rights isn't built for {platform}")
        return list(argv), {}
    if admin:
        if lease is not None:
            return list(argv), {}
        if not run_as:
            raise LaunchRefused("worker runs as root but no normal account (run_as) is set")
        import pwd
        pw = pwd.getpwnam(run_as)
        return list(argv), {"user": pw.pw_uid, "group": pw.pw_gid,
                            "extra_groups": os.getgrouplist(run_as, pw.pw_gid)}
    if lease is not None:
        return ["sudo", "-n", "--", *argv], {}
    return list(argv), {}


async def launch(argv: list[str], lease: dict | None = None, *, run_as: str | None = None,
                 **kwargs: Any) -> asyncio.subprocess.Process:
    """Start a process the way every plugin must (7.1): as the normal user,
    or elevated when ``lease`` is the call's lease. ``kwargs`` go to
    ``asyncio.create_subprocess_exec`` (cwd, env, stdin, stdout, ...)."""
    try:
        args, extra = launch_plan(argv, lease, run_as=run_as)
    except LaunchUnsupported:
        raise ElevationError("unsupported") from None
    return await asyncio.create_subprocess_exec(*args, **extra, **kwargs)
