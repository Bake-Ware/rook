"""Staged worker rollout: canaries first, then the rest, stop on a failed gate.

The hub pushes one signed manifest (a build kept in the worker feed) with
``worker.apply``. Each worker verifies the signature and hash, smoke-tests
the bundle, swaps it in and restarts; its own band-round-trip gate rolls it
back if the new build cannot reach the band (rook/worker/plugins/selfupdate).
The hub-side gate here waits for the worker to come back announcing the new
build, then proves the hub can reach it (``worker.status`` answers with that
build) and that it is still on it a little later. A worker that rolls back,
fails to answer, or misses the window fails the gate, and the rollout stops
before touching anyone else.

The engine is band-agnostic: it is given a ``client`` with ``workers`` (the
live roster) and ``call(cap=, args=, target=, timeout=, identity=)``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

log = logging.getLogger("rook.hub.plugins.updates.rollout")

ONLINE_SECS = 90.0
APPLY_TIMEOUT = 60.0


def online(w: dict, now: float | None = None) -> bool:
    return ((now or time.time()) - float(w.get("last_seen") or 0)) < ONLINE_SECS


def new_rollout(build: int, targets: list[dict], canaries: list[str], *, by: str,
                release_after: bool, gate_secs: float, settle_secs: float,
                batch: int, note: str = "") -> dict:
    rows = []
    for t in targets:
        rows.append({"worker_id": t["worker_id"], "name": t.get("name") or t["worker_id"][:8],
                     "band": t.get("band"), "from_build": t.get("build"),
                     "phase": "canary" if t["worker_id"] in canaries else "main",
                     "status": "pending", "detail": "", "started": 0, "finished": 0})
    rows.sort(key=lambda r: (r["phase"] != "canary", r["name"].lower()))
    return {"id": time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6], "kind": "workers",
            "build": int(build), "targets": rows, "status": "running", "reason": "",
            "by": by, "note": note, "release_after": bool(release_after),
            "gate_secs": float(gate_secs), "settle_secs": float(settle_secs),
            "batch": max(1, int(batch)), "started_at": time.time(), "finished_at": 0}


class RolloutEngine:
    POLL = 2.0     # seconds between roster checks while a worker restarts

    def __init__(self, client: Any, manifest: dict, state: dict,
                 save: Callable[[], None], identity: str = "system:rook-hub-updates",
                 sleep: Callable[[float], Awaitable] = asyncio.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.manifest = manifest
        self.state = state          # the rollout dict, mutated in place
        self.save = save
        self.identity = identity
        self._sleep = sleep
        self._clock = clock
        self.cancelled = False

    def _row(self, t: dict, status: str, detail: str = "") -> None:
        t["status"], t["detail"] = status, detail
        if status in ("pushing",) and not t["started"]:
            t["started"] = self._clock()
        if status in ("done", "failed", "skipped", "held", "cancelled"):
            t["finished"] = self._clock()
        self.save()

    async def run(self) -> dict:
        st = self.state
        try:
            for phase in ("canary", "main"):
                rows = [t for t in st["targets"] if t["phase"] == phase and t["status"] == "pending"]
                size = 1 if phase == "canary" else st["batch"]
                for i in range(0, len(rows), size):
                    if self.cancelled:
                        return self._finish("cancelled", "stopped by the operator")
                    chunk = rows[i:i + size]
                    await asyncio.gather(*(self.one(t) for t in chunk))
                    failed = [t for t in chunk if t["status"] == "failed"]
                    if failed:
                        names = ", ".join(t["name"] for t in failed)
                        return self._finish("stopped", f"{names} failed the health check: "
                                                        f"{failed[0]['detail']}")
                if phase == "canary":
                    canaries = [t for t in st["targets"] if t["phase"] == "canary"]
                    if canaries and not any(t["status"] == "done" for t in canaries):
                        return self._finish("stopped", "no canary could be updated, so nobody "
                                                       "else was touched")
            skipped = [t for t in st["targets"] if t["status"] in ("skipped", "held")]
            return self._finish("done", f"{len(skipped)} skipped" if skipped else "")
        except asyncio.CancelledError:
            self._finish("cancelled", "the hub stopped during the rollout")
            raise
        except Exception as e:  # never leave a rollout "running" forever
            log.exception("rollout %s crashed", st.get("id"))
            return self._finish("stopped", f"internal error: {e}")

    def _finish(self, status: str, reason: str) -> dict:
        st = self.state
        for t in st["targets"]:
            if t["status"] == "pending":
                t["status"] = "cancelled" if status != "done" else "skipped"
                t["detail"] = "not started" if status != "done" else t["detail"]
        st["status"], st["reason"], st["finished_at"] = status, reason, self._clock()
        self.save()
        return st

    async def one(self, t: dict) -> None:
        build = self.state["build"]
        wid = t["worker_id"]
        w = self.client.workers.get(wid)
        if w is None or not online(w, self._clock()):
            return self._row(t, "skipped", "offline: it updates when it comes back "
                                           "and the build is released to everyone")
        have = w.get("build")
        if isinstance(have, int) and have >= build:
            return self._row(t, "done", f"already on build {have}")
        if "worker.apply" not in (w.get("caps") or []):
            return self._row(t, "skipped", "this worker cannot take updates over the band; "
                                           "reinstall it with the install command")
        self._row(t, "pushing", f"sending build {build}")
        try:
            reply = await self.client.call(cap="worker.apply", args={"manifest": self.manifest},
                                           target=wid, timeout=APPLY_TIMEOUT,
                                           identity=self.identity)
        except Exception as e:
            return self._row(t, "failed", f"could not send the update: {e}")
        if not reply.get("ok", False):
            return self._row(t, "failed", str(reply.get("error") or "the worker refused the update"))
        res = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        action = res.get("action")
        if res.get("ok") is False:
            return self._row(t, "failed", str(res.get("error") or "the worker refused the update"))
        if action == "up-to-date":
            return self._row(t, "done", "already up to date")
        if action == "held":
            return self._row(t, "held", "this worker is pinned (worker.hold); not updated")
        if action == "skipped":
            return self._row(t, "failed", str(res.get("reason") or "the worker skipped it"))
        self._row(t, "waiting", "installing and restarting")
        await self._gate(t)

    async def _gate(self, t: dict) -> None:
        build, wid = self.state["build"], t["worker_id"]
        deadline = self._clock() + self.state["gate_secs"]
        while self._clock() < deadline:
            if self.cancelled:
                return self._row(t, "failed", "rollout cancelled while it was restarting")
            w = self.client.workers.get(wid) or {}
            upd = ((w.get("hb") or {}).get("worker") or {})
            if isinstance(upd, dict) and upd.get("bad_build") == build:
                return self._row(t, "failed", f"it rolled itself back: {upd.get('update', '')}")
            if w.get("build") == build and online(w, self._clock()):
                break
            await self._sleep(self.POLL)
        else:
            return self._row(t, "failed", f"it did not come back on build {build} within "
                                          f"{int(self.state['gate_secs'])}s")
        self._row(t, "waiting", "back on the new build; checking the hub can reach it")
        try:
            reply = await self.client.call(cap="worker.status", args={}, target=wid,
                                           timeout=15.0, identity=self.identity)
        except Exception as e:
            reply = {"ok": False, "error": str(e)}
        res = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        if not reply.get("ok", False) or res.get("build") not in (None, build):
            return self._row(t, "failed", "it came back but did not answer the hub: "
                                          + str(reply.get("error") or res.get("build")))
        settle_end = self._clock() + self.state["settle_secs"]
        while self._clock() < settle_end:
            await self._sleep(min(self.POLL * 2.5, max(0.0, settle_end - self._clock())))
            w = self.client.workers.get(wid) or {}
            if w.get("build") not in (None, build):
                return self._row(t, "failed", f"it went back to build {w.get('build')}")
        self._row(t, "done", f"on build {build} and answering")
