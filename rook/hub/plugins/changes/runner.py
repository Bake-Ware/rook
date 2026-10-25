"""Running a change request: checks, scripts, tests, keep or roll back.

Every command is ``shell.exec`` on the machine its step names, journaled as
``change:<name>``. Steps run as the hub, not as their author: the author's
right to run commands there was checked when the CR came in, and what a
standard change may do (no sacred machine, no hard stop) was checked by
:func:`.model.checklist`; anything else ran only after the person's yes.

A step with ``detach`` runs in its own session on the worker
(``~/.rook-changes/<cr>/<step>.log`` and ``.rc``) and is polled, so it
outlives a hub restart: a hub deploy restarts the hub that runs it, and
:meth:`Runner.resume` picks the CR up where it was.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import time
from typing import Any, Callable

log = logging.getLogger("rook.hub.plugins.changes.runner")

DIR = ".rook-changes"
POLL_S = 10.0
#: The phases in order; a failure in ``scripts`` or ``post`` rolls back.
PHASES = ("point", "dry_run", "pre", "scripts", "post")


def _tail(text: Any, n: int = 600) -> str:
    t = str(text or "")
    return t if len(t) <= n else "…" + t[-n:]


class Runner:
    def __init__(self, store: Any, node: Callable[[], Any], told: Callable[..., Any],
                 poll_s: float = POLL_S, clock=time.time) -> None:
        self.store = store
        self._node = node
        self._told = told          # told(cr, outcome, line): the log line, FYI, alert
        self.poll_s = poll_s
        self._clock = clock
        self._tasks: dict[str, asyncio.Task] = {}

    # -- one command ---------------------------------------------------------------
    def _target(self, worker: str) -> str:
        from ..jobs.kinds import resolve_worker
        node = self._node()
        client = getattr(node, "client", None)
        found = resolve_worker(dict(getattr(client, "workers", {}) or {}), worker, "shell.exec",
                               getattr(node, "worker_id", ""))
        if not found:
            raise ConnectionError(f"{worker} is not on the band")
        return found[0]

    async def _exec(self, cr: dict, worker: str, cmd: str, timeout: float) -> dict:
        from ...authz import system
        node = self._node()
        try:
            wid = self._target(worker)
            reply = await node.client.call(
                cap="shell.exec", args={"cmd": cmd, "timeout": timeout}, target=wid,
                timeout=timeout + 15, identity=f"change:{cr['id']}",
                principal=system(f"change-{cr['id']}"))
        except (asyncio.TimeoutError, ConnectionError, ValueError) as e:
            return {"ok": False, "rc": None, "out": "", "error": f"{type(e).__name__}: {e}"}
        res = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        if not reply.get("ok") and not res:
            return {"ok": False, "rc": None, "out": "", "error": str(reply.get("error"))[:300]}
        rc = res.get("code")
        return {"ok": rc == 0, "rc": rc, "out": _tail(f"{res.get('stdout') or ''}"
                                                       f"{res.get('stderr') or ''}"),
                "error": "" if rc is not None else str(res.get("error") or "")[:300]}

    async def _detached(self, cr: dict, name: str, step: dict, cmd: str,
                        resumed: bool) -> dict:
        base = f"~/{DIR}/{cr['id']}/{name}"
        if resumed:
            # Cut off by a restart: start it only if it never started.
            seen = await self._exec(cr, step["worker"], f"test -f {base}.log", 30)
            resumed = seen["rc"] == 0
        if not resumed:
            inner = f"bash -c {shlex.quote(cmd)} > {base}.log 2>&1; echo $? > {base}.rc.tmp " \
                    f"&& mv {base}.rc.tmp {base}.rc"
            start = (f"mkdir -p ~/{DIR}/{cr['id']} && rm -f {base}.rc && "
                     f"setsid -f bash -c {shlex.quote(inner)} >/dev/null 2>&1 </dev/null")
            got = await self._exec(cr, step["worker"], start, 30)
            if not got["ok"]:
                return {**got, "error": got["error"] or "could not start it"}
        deadline = self._clock() + step["timeout"]
        poll = f"test -f {base}.rc && {{ cat {base}.rc; echo ---; tail -c 1500 {base}.log; }}"
        while True:
            got = await self._exec(cr, step["worker"], poll, 30)
            if got["rc"] == 0:
                head, _, rest = got["out"].lstrip("…").partition("---")
                try:
                    rc = int(head.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    rc = None
                return {"ok": rc == 0, "rc": rc, "out": _tail(rest.strip()), "error": ""}
            if self._clock() >= deadline:
                return {"ok": False, "rc": None, "out": "", "error": "timed out"}
            await asyncio.sleep(self.poll_s)

    async def step(self, cr: dict, phase: str, i: int, step: dict, cmd: str | None = None,
                   resumed: bool = False) -> dict:
        cmd = cmd or step["run"]
        if step.get("detach"):
            got = await self._detached(cr, f"{phase}-{i}", step, cmd, resumed)
        else:
            got = await self._exec(cr, step["worker"], cmd, step["timeout"])
        if phase in ("pre", "post", "after") and got["rc"] is not None:
            want = step.get("expect")
            got["ok"] = got["rc"] == step.get("expect_rc", 0) and (not want or want in got["out"])
        self.store.note(cr["id"], {"phase": phase, "step": i, "worker": step["worker"],
                                   "ok": got["ok"], "rc": got["rc"], "out": got["out"],
                                   **({"error": got["error"]} if got["error"] else {})})
        return got

    # -- the whole change --------------------------------------------------------------
    def start(self, ident: str, resumed: bool = False) -> None:
        if ident in self._tasks and not self._tasks[ident].done():
            return
        self._tasks[ident] = asyncio.get_running_loop().create_task(self._guarded(ident, resumed))

    async def _guarded(self, ident: str, resumed: bool) -> None:
        try:
            await self.run(ident, resumed)
        except Exception as e:  # noqa: BLE001 - a broken run says so, never hangs open
            log.exception("changes: %s failed", ident)
            row = self.store.set(ident, state="failed")
            self.store.note(ident, {"phase": "error", "error": f"{type(e).__name__}: {e}"[:300]})
            self._told(row, "failed", f"the run broke ({type(e).__name__})")

    async def run(self, ident: str, resumed: bool = False) -> dict:
        row = self.store.get(ident)
        cr = {"id": ident, **row["record"]}
        rb = cr.get("rollback") or {}
        start_phase = row["phase"] if resumed and row["phase"] in PHASES + ("rollback",) else ""
        if start_phase == "rollback":
            return await self._rollback(cr, "resumed after a restart", resumed=True)
        phases = PHASES[PHASES.index(start_phase):] if start_phase else PHASES
        self.store.set(ident, state="running")
        for phase in phases:
            steps = {"point": [dict(rb, run=rb.get("point"))] if rb.get("point") else [],
                     "dry_run": [dict(rb, run=rb.get("dry_run"), detach=False)]
                     if rb.get("dry_run") else [],
                     "pre": cr["tests"], "scripts": cr["scripts"], "post": cr["tests"]}[phase]
            first = row["step"] if phase == start_phase else 0
            for i, s in enumerate(steps):
                if i < first:
                    continue
                where = f"{phase} step {i + 1} on {s['worker']}"
                cut = resumed and phase == start_phase and i == first
                if cut and phase == "scripts" and not s.get("detach"):
                    # It may have run halfway: nothing re-runs it, the rollback goes.
                    return await self._rollback(cr, f"{where} was cut off by a hub restart")
                self.store.set(ident, phase=phase, step=i)
                got = await self.step(cr, phase, i, s, resumed=cut and bool(s.get("detach")))
                if got["ok"] or phase == "pre" and got["rc"] is not None:
                    continue
                if phase in ("point", "dry_run", "pre"):
                    row = self.store.set(ident, state="refused")
                    self._told(row, "refused", f"nothing changed: {where} failed")
                    return row
                return await self._rollback(cr, f"{where} failed (rc {got['rc']})")
        row = self.store.set(ident, state="kept", phase="done")
        self._told(row, "kept", "tests passed")
        return row

    async def _rollback(self, cr: dict, why: str, resumed: bool = False) -> dict:
        rb = cr.get("rollback") or {}
        if not rb.get("run"):
            row = self.store.set(cr["id"], state="failed", phase="done")
            self._told(row, "failed", f"{why}; it has no rollback")
            return row
        self.store.set(cr["id"], phase="rollback", step=0)
        got = await self.step(cr, "rollback", 0, rb, resumed=resumed and bool(rb.get("detach")))
        for i, s in enumerate(cr["tests"]):
            await self.step(cr, "after", i, s)
        if not got["ok"]:
            row = self.store.set(cr["id"], state="failed", phase="done")
            self._told(row, "failed", f"{why}; the rollback failed too (rc {got['rc']})")
            return row
        row = self.store.set(cr["id"], state="rolled_back", phase="done")
        self._told(row, "rolled_back", why)
        return row

    async def revert(self, ident: str) -> dict:
        """Roll a kept change back later (the person's "revert that")."""
        row = self.store.get(ident)
        cr = {"id": ident, **row["record"]}
        rb = cr.get("rollback") or {}
        got = await self.step(cr, "revert", 0, rb) if rb.get("run") else \
            {"ok": False, "rc": None}
        row = self.store.set(ident, state="reverted" if got["ok"] else row["state"])
        self._told(row, "reverted" if got["ok"] else "failed",
                   "rolled back on request" if got["ok"] else f"revert failed (rc {got['rc']})")
        return row

    def resume(self) -> list[str]:
        """CRs a hub restart cut off: carried on where they were."""
        out = []
        for row in self.store.list(("running",), limit=100):
            self.start(row["id"], resumed=True)
            out.append(row["id"])
        return out

    async def stop(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
