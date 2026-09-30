"""Commit-confirmed worker config push (DESIGN-band-services.md §1).

Shared by ``rook_config_apply`` and the Settings area's "Apply to worker".
The worker stages the config, restarts under it and must be reconfirmed
within ``confirm_within`` seconds or it reverts on its own. Replies are
masked: the pushed ``psk`` and ``env`` values never come back (P19).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Iterable

from ..core.settings import mask_worker_config


def masked_reply(reply: Any, public_env: Iterable[str] = ()) -> Any:
    """A ``worker.config_get`` / ``config_apply`` reply with its config masked."""
    if not isinstance(reply, dict):
        return reply
    out = dict(reply)
    res = out.get("result")
    if isinstance(res, dict):
        res = dict(res)
        if isinstance(res.get("config"), dict):
            res["config"] = mask_worker_config(res["config"], public_env)
        out["result"] = res
    if isinstance(out.get("config"), dict):
        out["config"] = mask_worker_config(out["config"], public_env)
    return out


async def apply_confirmed(client: Any, target: str, settings: dict, *, identity: str,
                          confirm_within: float = 120.0, public_env: Iterable[str] = (),
                          sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                          poll: float = 4.0) -> dict:
    public_env = set(public_env)
    epoch = int(time.time())
    try:
        applied = await client.call(
            cap="worker.config_apply", target=target, timeout=15.0, identity=identity,
            args={"settings": settings, "epoch": epoch, "confirm_within": confirm_within})
    except asyncio.TimeoutError:
        return {"ok": False, "stage": "apply", "error": "no reply on config_apply"}
    if not (applied.get("result") or {}).get("ok", applied.get("ok")):
        return {"ok": False, "stage": "apply", "reply": masked_reply(applied, public_env)}

    deadline = time.time() + min(confirm_within, 110.0)
    await sleep(poll)  # let it go down and restart
    last_err = "worker did not return"
    while time.time() < deadline:
        try:
            got = await client.call(cap="worker.config_get", target=target, timeout=8.0,
                                    identity=identity)
            res = got.get("result") or {}
            if res.get("epoch") == epoch or (res.get("config") or {}).get("epoch") == epoch:
                conf = await client.call(cap="worker.config_confirm", target=target,
                                         timeout=10.0, identity=identity, args={"epoch": epoch})
                return {"ok": True, "epoch": epoch, "confirmed": conf.get("result", conf),
                        "config": mask_worker_config(res.get("config"), public_env)}
            last_err = f"worker back but at epoch {res.get('epoch')}, not {epoch}"
        except asyncio.TimeoutError:
            last_err = "worker still down (restarting)"
        await sleep(poll)
    return {"ok": False, "stage": "confirm", "epoch": epoch,
            "error": f"{last_err}; worker will auto-revert to its prior config at its deadline"}
