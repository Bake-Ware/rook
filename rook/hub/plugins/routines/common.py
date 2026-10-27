"""Shared plumbing for the canned routines: their saved state, band calls as
the caller, and quiet delivery to the person's feed.

**State.** Each routine keeps one small JSON document per ``state`` key
(``<plugin data dir>/state/<key>.json``): what it saw last time, so the next
run reports only what changed. Written atomically; an unreadable file is a
fresh start (the next run is a baseline again).

**Band calls.** A routine calls worker caps (a phone's ``attention.feed``, a
camera's ``camera.snap``) as the principal that called the routine: a job
run's identity (its creator, rechecked each run) or the agent that called it
directly. The hub's authorizer checks each call for that principal, as for
any ``rook_call``. Each call is journaled under that identity; a sensitive
cap's result is never journaled (only that it answered).

**Delivery** (:func:`deliver`): one line in the 30-day log
(``attention.log``, outcome ``tell``, or ``ask_now`` for an urgent finding)
and one alert on the person's phones through the attention plugin, on the
quiet ``digest`` channel (no pop-up, no sound), or the ``urgent`` channel
when a routine found something clearly bad. Never a decision card, never a
question. A hub with no phone set up (``attention.devices`` empty) keeps the
log line and the routine's own result (the job run's output); the reply
says why nothing reached a phone.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("rook.hub.plugins.routines")

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CALL_TIMEOUT = 30.0


def state_key(key: str, default: str) -> str:
    key = str(key or default).strip().lower()
    if not KEY_RE.match(key):
        raise ValueError("state: 1-64 lowercase letters, digits, - or _")
    return key


class StateFile:
    """One routine's saved state (a JSON object)."""

    def __init__(self, root: Path, key: str) -> None:
        self.path = Path(root) / "state" / f"{key}.json"

    damaged = False

    def load(self) -> dict:
        """The saved state; ``{}`` when there is none. A file that exists but
        can't be read sets ``damaged`` (the routine says so)."""
        self.damaged = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            self.damaged = True
            return {}
        if not isinstance(data, dict):
            self.damaged = True
            return {}
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"), default=str)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def sidecar(self, name: str) -> Path:
        """A file beside the state (a camera's last frame)."""
        return self.path.parent / f"{self.path.stem}.{name}"


def caller_principal():
    from ...authz import current_principal
    return current_principal.get()


def caller_id() -> str:
    p = caller_principal()
    return getattr(p, "id", "") or "system:routines"


def roster(node: Any) -> dict[str, dict]:
    client = getattr(node, "client", None)
    try:
        return dict(getattr(client, "workers", {}) or {})
    except Exception:  # noqa: BLE001
        return {}


def find_worker(node: Any, ref: str) -> str | None:
    """A live worker's id by id or (unique, any case) name."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    workers = roster(node)
    if ref in workers:
        return ref
    hits = [wid for wid, w in workers.items() if str(w.get("name") or "").lower() == ref.lower()]
    return hits[0] if len(hits) == 1 else None


def worker_name(node: Any, wid: str) -> str:
    return str((roster(node).get(wid) or {}).get("name") or wid)


async def band_call(node: Any, cap: str, args: dict, target: str, *,
                    timeout: float = CALL_TIMEOUT, sensitive: bool = False,
                    why: str = "") -> dict:
    """One worker cap call as the routine's caller; the cap's result dict, or
    raises ``RuntimeError`` with the reason (denied, offline, failed)."""
    client = getattr(node, "client", None)
    if client is None:
        raise RuntimeError("the hub has no band client")
    p = caller_principal()
    ident = getattr(p, "id", None) or "system:routines"
    try:
        reply = await client.call(cap=cap, args=args, target=target, timeout=timeout,
                                  identity=ident, principal=p)
    except (TimeoutError, OSError, ConnectionError) as e:
        reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    if not isinstance(reply, dict):
        reply = {"ok": False, "error": "no reply"}
    journal = getattr(node, "journal", None)
    if journal is not None:
        shown = {"ok": True, "result": "[sensitive: not journaled]"} if (sensitive and reply.get("ok")) else reply
        with contextlib.suppress(Exception):
            journal.record(cap=cap, worker=worker_name(node, target), identity=ident, args=None,
                           reply=shown, why=why or None)
    if not reply.get("ok"):
        raise RuntimeError(str(reply.get("error") or f"{cap} failed")[:300])
    res = reply.get("result")
    if isinstance(res, dict) and res.get("ok") is False:
        raise RuntimeError(str(res.get("error") or res.get("stderr") or f"{cap} refused")[:300])
    return res if isinstance(res, dict) else {"value": res}


def attention(node: Any) -> Any:
    getter = getattr(node, "plugin", None)
    if not callable(getter):
        return None
    with contextlib.suppress(Exception):
        return getter("attention")
    return None


async def deliver(node: Any, *, source: str, title: str, text: str, line: str, why: str = "",
                  urgent: bool = False, explain: str = "") -> dict:
    """Tell the person quietly: one log line and one alert on their phones
    (digest channel; urgent only for a clearly bad finding). Never raises."""
    att = attention(node)
    out: dict = {"logged": False, "phone": False, "urgent": bool(urgent)}
    if att is None:
        out["reason"] = "the attention plugin is not running on this hub"
        return out
    try:
        await att.service.send(text, title=title, urgency="urgent" if urgent else "normal",
                               channel="urgent" if urgent else "digest", explain=explain,
                               source=source)
        out["phone"] = True
        how = "urgent alert on the phone" if urgent else "quietly in the phone's digest"
    except ValueError as e:
        out["reason"] = str(e)
        how = "logged only: no phone set up"
    except Exception as e:  # noqa: BLE001 - the log line still counts
        log.exception("routines: sending %s failed", source)
        out["reason"] = f"sending failed: {type(e).__name__}"
        how = "logged only: sending failed"
    with contextlib.suppress(Exception):
        att.log(source, "ask_now" if urgent else "tell", line, why, how)
        out["logged"] = True
    return out


def clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"
