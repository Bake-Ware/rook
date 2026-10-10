"""Shared fakes for the jobs tests: a band roster with scripted caps, a
movable clock, a real vault in tmp, and helpers to build stores, executors
and schedulers without a hub."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rook.band_mcp.vault import Vault
from rook.hub.plugins.jobs.executor import Executor
from rook.hub.plugins.jobs.identity import resolve_identity
from rook.hub.plugins.jobs.model import check
from rook.hub.plugins.jobs.runtime import Runtime
from rook.hub.plugins.jobs.scheduler import Scheduler
from rook.hub.plugins.jobs.store import JobStore

HUB = "hubnode"
OWNER = {"id": "token:agent_t", "kind": "token", "role": "agent", "groups": [], "label": "test"}
# 2026-10-10 12:00:00 UTC
T0 = 1791633600.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class Journal:
    def __init__(self) -> None:
        self.rows = []

    def record(self, **kw):
        self.rows.append(kw)
        return f"c{len(self.rows)}"


class FakeRuntime(Runtime):
    """Workers are ``{id: entry}``; ``caps[cap]`` is ``fn(args, worker_id)``
    returning a reply dict (sync or async), or raising."""

    def __init__(self, tmp_path=None, *, clock=None, workers=None, caps=None, tools=None) -> None:
        vault = Vault(str(tmp_path / "vault.db")) if tmp_path is not None else None
        node = SimpleNamespace(_vault=vault, journal=Journal(), worker_id=HUB)
        super().__init__(node, clock=clock or Clock(), sleep=self._sleep)
        self.workers = workers if workers is not None else {
            "w1": {"name": "alpha", "caps": ["shell.exec", "notify.post", "voice.speak"],
                   "facts": {"os": "linux"}, "tags": ["lab"]},
            "w2": {"name": "beta", "caps": ["shell.exec"], "facts": {"os": "windows"}},
        }
        self.caps = caps or {}
        self.tools = tools or {}
        self.calls = []
        self.slept = []

    async def _sleep(self, s: float) -> None:
        self.slept.append(s)
        await asyncio.sleep(0)

    @property
    def vault(self):
        return self.node._vault

    def roster(self) -> dict:
        return {**self.workers, HUB: {"name": "rook", "caps": ["notify.send", "hub.info"]}}

    async def call(self, cap, args, target, timeout, identity):
        self.calls.append((cap, args, target, identity.display))
        fn = self.caps.get(cap)
        if fn is None:
            return {"ok": False, "error": f"no cap {cap}"}
        out = fn(args, target)
        if asyncio.iscoroutine(out):
            out = await asyncio.wait_for(out, timeout)
        return out

    async def tool(self, name, args, identity):
        return await self.tools[name](**args)


def ok(result=None):
    return {"ok": True, "result": result}


def shell(exit_code=0, stdout=""):
    return lambda args, wid: ok({"exit_code": exit_code, "stdout": stdout})


def job_doc(steps, entry=None, **kw):
    return {"name": kw.pop("name", "j"), "entry": entry or next(iter(steps), "a"), "steps": steps, **kw}


def make(tmp_path, steps=None, entry=None, *, runtime=None, clock=None, owner="hubA", **kw):
    """(store, scheduler, runtime, job row) on a fresh database."""
    clock = clock or Clock()
    tmp_path.mkdir(parents=True, exist_ok=True)
    rt = runtime or FakeRuntime(tmp_path, clock=clock)
    store = JobStore(tmp_path / "jobs.db")
    sched = Scheduler(store, Executor(rt), owner=owner, clock=clock, settings=lambda k: None)
    row = None
    if steps is not None:
        job, _ = check(job_doc(steps, entry, **kw), now=clock())
        row = store.create_job(job, OWNER, clock(), "America/Toronto")
    return store, sched, rt, row


async def execute(rt, steps, entry=None, settings=None, **kw):
    """Run a graph straight through the executor; returns the result."""
    job, _ = check(job_doc(steps, entry, **kw))
    job["id"] = "j_test"
    ex = Executor(rt, settings=settings or (lambda k: None))
    return await ex.execute(job, {"id": "r_test", "started": rt.clock(), "missed": False, "vars": {}},
                            resolve_identity(job, OWNER))


async def drain(sched):
    """Wait for every run the scheduler started."""
    while sched.active:
        await asyncio.gather(*list(sched.active.values()), return_exceptions=True)
