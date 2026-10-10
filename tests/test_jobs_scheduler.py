"""Jobs scheduler (rook/hub/plugins/jobs/scheduler.py, store.py): triggers,
overlap (queue without limit by default, max_queue, skip, parallel), missed
runs with grace, at/after triggers, atomic lease claims between two
schedulers on one database, interrupted-on-start, cancel, alerts and
retention. Time is a hand-moved clock."""
import asyncio

import pytest

from rook.hub.plugins.jobs.executor import Executor
from rook.hub.plugins.jobs.scheduler import Scheduler
from rook.hub.plugins.jobs.store import JobStore
from tests.jobs_fakes import OWNER, T0, Clock, FakeRuntime, drain, make, ok, shell

NOOP = {"a": {"kind": "noop"}}
CAP = {"a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec", "args": {"cmd": "x"}}}


def states(store, jid):
    return [r["state"] for r in reversed(store.runs(jid, limit=100))]


# -- overlap ------------------------------------------------------------------------------

def test_queue_has_no_limit_by_default(tmp_path):
    store, _, _, job = make(tmp_path, NOOP)
    for _ in range(6):
        store.enqueue(job, "manual", T0)
    assert states(store, job["id"]) == ["due"] + ["queued"] * 5


def test_queue_with_max_queue_drops_the_overflow(tmp_path):
    store, _, _, job = make(tmp_path, NOOP, overlap={"mode": "queue", "max_queue": 1})
    runs = [store.enqueue(job, "manual", T0) for _ in range(3)]
    assert [r["state"] for r in runs] == ["due", "queued", "dropped"]
    assert runs[2]["error"] == "queue full (max_queue 1)" and runs[2]["finished"] == T0


def test_skip_and_parallel(tmp_path):
    store, _, _, job = make(tmp_path, NOOP, overlap={"mode": "skip"})
    assert [store.enqueue(job, "manual", T0)["state"] for _ in range(2)] == ["due", "dropped"]
    store2, _, _, job2 = make(tmp_path / "p", NOOP, overlap={"mode": "parallel"}, name="par")
    assert [store2.enqueue(job2, "manual", T0)["state"] for _ in range(3)] == ["due"] * 3


@pytest.mark.asyncio
async def test_queued_runs_wait_for_the_active_one(tmp_path):
    gate = asyncio.Event()

    async def blocked(args, wid):
        await gate.wait()
        return ok({"exit_code": 0})
    clock = Clock()
    rt = FakeRuntime(tmp_path, clock=clock, caps={"shell.exec": blocked})
    store, sched, _, job = make(tmp_path, CAP, runtime=rt, clock=clock)
    for _ in range(3):
        store.enqueue(job, "manual", clock())
    assert len(await sched.tick()) == 1
    assert await sched.tick() == []            # nothing promoted while one runs
    assert states(store, job["id"]) == ["running", "queued", "queued"]
    gate.set()
    await drain(sched)
    assert len(await sched.tick()) == 1        # the next one, in order
    await drain(sched)
    await sched.tick()
    await drain(sched)
    assert states(store, job["id"]) == ["success"] * 3


@pytest.mark.asyncio
async def test_parallel_runs_together(tmp_path):
    clock = Clock()
    gate = asyncio.Event()

    async def blocked(args, wid):
        await gate.wait()
        return ok({"exit_code": 0})
    rt = FakeRuntime(tmp_path, clock=clock, caps={"shell.exec": blocked})
    store, sched, _, job = make(tmp_path, CAP, runtime=rt, clock=clock, overlap={"mode": "parallel"})
    for _ in range(3):
        store.enqueue(job, "manual", clock())
    assert len(await sched.tick()) == 3
    gate.set()
    await drain(sched)


# -- cron, missed runs ----------------------------------------------------------------------

def every5(**kw):
    return dict(triggers=[{"kind": "cron", "expr": "*/5 * * * *", "tz": "UTC"}], **kw)


@pytest.mark.asyncio
async def test_cron_fires_on_time(tmp_path):
    clock = Clock()
    store, sched, _, job = make(tmp_path, NOOP, clock=clock, **every5())
    assert store.triggers(job["id"])[0]["next_at"] == T0 + 300
    clock.advance(299)
    assert await sched.tick() == []
    clock.advance(11)
    started = await sched.tick()
    await drain(sched)
    run = store.get_run(started[0])
    assert run["state"] == "success" and not run["missed"] and run["scheduled"] == T0 + 300
    assert run["trigger"] == "cron */5 * * * *" and run["identity_used"] == OWNER["id"]
    assert store.triggers(job["id"])[0]["next_at"] == T0 + 600


def _down_an_hour(tmp_path, **kw):
    clock = Clock()
    store, sched, _, job = make(tmp_path, NOOP, clock=clock, **every5(**kw))
    clock.advance(3600 + 30)   # the hub was down; 13:00 is 30s late (on time), 12:55 is 5.5 min late
    return clock, store, sched, job


def _fired(store, jid):
    return sorted(((r["scheduled"] - T0) / 60, r["missed"], r["state"]) for r in store.runs(jid, limit=100))


def test_missed_run_once_within_grace(tmp_path):
    clock, store, sched, job = _down_an_hour(tmp_path)
    sched.fire_due(clock())
    # 12:55 is the latest missed fire within 10m grace; 13:00 is on time.
    assert _fired(store, job["id"]) == [(55.0, True, "due"), (60.0, False, "queued")]
    assert store.triggers(job["id"])[0]["next_at"] == T0 + 3900


def test_missed_all_within_grace(tmp_path):
    clock, store, sched, job = _down_an_hour(tmp_path, missed={"mode": "all", "grace": "1h"})
    sched.fire_due(clock())
    got = _fired(store, job["id"])
    assert [m for m, _, _ in got] == [float(m) for m in range(5, 65, 5)]
    assert sum(1 for _, missed, _ in got if missed) == 11
    assert sum(1 for _, _, s in got if s == "queued") == 11   # unbounded queue keeps them all


def test_missed_skip_records_one_dropped_run(tmp_path):
    clock, store, sched, job = _down_an_hour(tmp_path, missed={"mode": "skip", "grace": "1h"})
    sched.fire_due(clock())
    got = _fired(store, job["id"])
    assert (60.0, False, "due") in got
    dropped = [r for r in store.runs(job["id"]) if r["state"] == "dropped"]
    assert len(dropped) == 1 and dropped[0]["missed"] and "missed 11 fire(s)" in dropped[0]["error"]


def test_missed_beyond_grace_is_not_run(tmp_path):
    clock = Clock()
    store, sched, _, job = make(tmp_path, NOOP, clock=clock,
                                triggers=[{"kind": "cron", "expr": "0 9 * * *"}])  # 09:00 Toronto
    clock.advance(86400 * 2)   # down two days; the last 09:00 was 23h ago
    sched.fire_due(clock())
    assert store.runs(job["id"]) == []
    assert store.triggers(job["id"])[0]["next_at"] > clock()


@pytest.mark.asyncio
async def test_missed_is_a_graph_variable(tmp_path):
    clock = Clock()
    steps = {"a": {"kind": "noop", "on": {"success": ["j"]}},
             "j": {"kind": "join", "condition": "run.missed", "on": {"success": ["late"], "failure": ["fresh"]}},
             "late": {"kind": "noop"}, "fresh": {"kind": "noop"}}
    store, sched, _, job = make(tmp_path, steps, clock=clock, **every5())
    clock.advance(300 + 200)  # 12:05 fire, 200s late: missed
    await sched.tick()
    await drain(sched)
    run = store.runs(job["id"])[0]
    assert run["missed"] and "late" in run["steps"] and "fresh" not in run["steps"]


def test_disabled_jobs_do_not_fire_and_resume_from_now(tmp_path):
    clock = Clock()
    store, sched, _, job = make(tmp_path, NOOP, clock=clock, **every5())
    store.set_enabled(job["id"], False, "t", clock(), "UTC", reason="maintenance")
    assert store.get_job(job["id"])["paused_reason"] == "maintenance"
    clock.advance(3600)
    sched.fire_due(clock())
    assert store.runs(job["id"]) == []
    store.set_enabled(job["id"], True, "t", clock(), "UTC")
    sched.fire_due(clock())
    assert store.runs(job["id"]) == []   # nothing "missed" while it was off
    assert store.triggers(job["id"])[0]["next_at"] == T0 + 3900


# -- at and after ----------------------------------------------------------------------------

def test_at_fires_once(tmp_path):
    clock = Clock()
    store, sched, _, job = make(tmp_path, NOOP, clock=clock,
                                triggers=[{"kind": "at", "when": "2026-10-10T08:01:00-04:00"}])
    clock.advance(30)
    assert sched.fire_due(clock()) == []
    clock.advance(40)
    assert [r["trigger"] for r in sched.fire_due(clock())] == ["at"]
    clock.advance(3600)
    assert sched.fire_due(clock()) == []
    assert store.triggers(job["id"])[0]["done"] == 1


@pytest.mark.asyncio
async def test_after_retriggers_on_its_outcome(tmp_path):
    clock = Clock()
    rt = FakeRuntime(tmp_path, clock=clock, caps={"shell.exec": shell()})
    store, sched, _, job = make(tmp_path, CAP, runtime=rt, clock=clock,
                                triggers=[{"kind": "manual"}, {"kind": "after", "every": "15m", "on": "success"}])
    store.enqueue(job, "manual", clock())
    await sched.tick()
    await drain(sched)
    await sched.tick()   # arms the after trigger from the finished run
    trig = store.triggers(job["id"])[0]
    assert trig["next_at"] == pytest.approx(clock() + 900)
    clock.advance(901)
    started = await sched.tick()
    await drain(sched)
    assert store.get_run(started[0])["trigger"] == "after"
    # A failing run does not re-arm an on-success trigger.
    rt.caps["shell.exec"] = shell(exit_code=1)
    clock.advance(901)
    await sched.tick()
    await drain(sched)
    await sched.tick()
    assert store.triggers(job["id"])[0]["next_at"] is None


# -- leases, two schedulers, restarts ------------------------------------------------------

def test_two_schedulers_never_claim_the_same_run(tmp_path):
    clock = Clock()
    store_a, sched_a, _, job = make(tmp_path, NOOP, clock=clock, owner="hubA")
    store_b = JobStore(tmp_path / "jobs.db")   # a second connection, as a second process would have
    store_a.enqueue(job, "manual", clock())
    a = store_a.claim("hubA", clock(), 60)
    b = store_b.claim("hubB", clock(), 60)
    assert (a is None) != (b is None)
    first = a or b
    for _ in range(5):
        store_a.enqueue(job, "manual", clock())
    assert store_a.promote() == 0              # queue mode: one at a time per job
    assert store_a.finish(first["id"], first["lease_owner"], "success", clock())
    assert store_b.promote() == 1
    claimed = [store_a.claim("hubA", clock(), 60), store_b.claim("hubB", clock(), 60)]
    assert sum(1 for c in claimed if c) == 1


def test_two_schedulers_fire_a_trigger_once(tmp_path):
    clock = Clock()
    store_a, sched_a, rt, job = make(tmp_path, NOOP, clock=clock, owner="hubA", **every5())
    store_b = JobStore(tmp_path / "jobs.db")
    sched_b = Scheduler(store_b, Executor(rt), owner="hubB", clock=clock)
    clock.advance(310)
    due = store_a.due_triggers(clock())
    got_a = store_a.fire(due[0], T0 + 600, False, [(T0 + 300, False)], "cron", clock())
    got_b = store_b.fire(due[0], T0 + 600, False, [(T0 + 300, False)], "cron", clock())
    assert len(got_a) == 1 and got_b is None
    assert sched_b.fire_due(clock()) == [] and len(store_a.runs(job["id"])) == 1


@pytest.mark.asyncio
async def test_parallel_ticks_on_one_database(tmp_path):
    clock = Clock()
    store_a, sched_a, rt, job = make(tmp_path, NOOP, clock=clock, owner="hubA",
                                     overlap={"mode": "parallel"}, **every5())
    sched_b = Scheduler(JobStore(tmp_path / "jobs.db"), Executor(rt), owner="hubB", clock=clock)
    clock.advance(310)
    for _ in range(4):
        store_a.enqueue(job, "manual", clock())
    a, b = await asyncio.gather(sched_a.tick(), sched_b.tick())
    await drain(sched_a)
    await drain(sched_b)
    assert len(a) + len(b) == 5 and not set(a) & set(b)
    assert sorted(states(store_a, job["id"])) == ["success"] * 5


def test_expired_lease_is_interrupted_by_another_scheduler(tmp_path):
    clock = Clock()
    store, _, _, job = make(tmp_path, NOOP, clock=clock)
    store.enqueue(job, "manual", clock())
    run = store.claim("hubA", clock(), 60)
    other = JobStore(tmp_path / "jobs.db")
    assert other.interrupt(clock() + 30) == []
    assert other.interrupt(clock() + 61) == [run["id"]]
    assert other.get_run(run["id"])["state"] == "interrupted"


def test_runs_left_running_are_interrupted_on_start(tmp_path):
    clock = Clock()
    store, _, rt, job = make(tmp_path, NOOP, clock=clock, owner="hubA")
    store.enqueue(job, "manual", clock())
    run = store.claim("hubA", clock(), 3600)
    restarted = Scheduler(JobStore(tmp_path / "jobs.db"), Executor(rt), owner="hubA", clock=clock)
    assert restarted.startup() == [run["id"]]
    got = store.get_run(run["id"])
    assert got["state"] == "interrupted" and "hub stopped" in got["error"] and got["finished"] == clock()


# -- cancel, alerts, run records, retention ------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_queued_and_running(tmp_path):
    clock = Clock()

    async def slow(args, wid):
        await asyncio.sleep(10)
    rt = FakeRuntime(tmp_path, clock=clock, caps={"shell.exec": slow})
    store, sched, _, job = make(tmp_path, CAP, runtime=rt, clock=clock)
    sched.lease = 0.15
    first = store.enqueue(job, "manual", clock())
    second = store.enqueue(job, "manual", clock())
    assert store.cancel(second["id"], clock())["state"] == "cancelled"
    await sched.tick()
    assert store.cancel(first["id"], clock())["cancel"] == 1   # asked; the lease holder stops it
    await asyncio.wait_for(drain(sched), 5)
    got = store.get_run(first["id"])
    assert got["state"] == "cancelled" and got["steps"]["a"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_run_record_and_alerts(tmp_path):
    clock = Clock()
    rt = FakeRuntime(tmp_path, clock=clock, caps={"shell.exec": shell(exit_code=1, stdout="nope"),
                                                  "notify.post": lambda a, w: ok({})})
    store, sched, _, job = make(tmp_path, CAP, runtime=rt, clock=clock,
                                alerts={"on_failure": [{"text": "{{job.name}} failed ({{run.id}})"}]})
    from rook.hub.plugins.jobs.service import JobService
    svc = JobService(store, sched, settings=lambda k: None, clock=clock)
    sched.alerts = svc.alerts
    run = store.enqueue(job, "manual", clock())
    await sched.tick()
    await drain(sched)
    got = store.get_run(run["id"])
    assert got["state"] == "failure" and got["executions"] == 1
    assert got["steps"]["a"]["exit_code"] == 1 and got["steps"]["a"]["output"]["stdout"] == "nope"
    assert got["alerts"] == [{"via": "notify", "state": "success"}]
    assert rt.calls[-1][1]["text"] == f"j failed ({run['id']})"


def test_retention_prunes_old_runs(tmp_path):
    clock = Clock()
    store, sched, _, job = make(tmp_path, NOOP, clock=clock)
    _, _, _, keep = make(tmp_path, NOOP, clock=clock, retention_days=90, name="keeper")
    old = clock() - 40 * 86400
    for j in (job, keep):
        store.enqueue(j, "manual", old, state="dropped")
        store.enqueue(j, "manual", clock(), state="dropped")
    store.enqueue(job, "manual", old)   # still waiting: never pruned
    assert sched.prune(clock()) == 1
    assert len(store.runs(job["id"])) == 2 and len(store.runs(keep["id"])) == 2
    assert store.prune(clock() + 60 * 86400, 30) == 2


def test_delete_removes_job_triggers_and_history(tmp_path):
    clock = Clock()
    store, _, _, job = make(tmp_path, NOOP, clock=clock, **every5())
    store.enqueue(job, "manual", clock())
    out = store.delete_job(job["id"])
    assert out["runs_deleted"] == 1 and store.get_job(job["id"]) is None
    assert store.triggers(job["id"]) == []


@pytest.mark.asyncio
async def test_loop_runs_manual_runs_and_stop_interrupts(tmp_path):
    clock = Clock()

    async def slow(args, wid):
        await asyncio.sleep(10)
    rt = FakeRuntime(tmp_path, clock=clock, caps={"shell.exec": slow})
    store, sched, _, job = make(tmp_path, CAP, runtime=rt, clock=clock)
    loop = asyncio.ensure_future(sched.run_forever())
    await asyncio.sleep(0.01)
    run = store.enqueue(job, "manual", clock())
    sched.wake()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if store.get_run(run["id"])["state"] == "running":
            break
    await sched.stop()
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    got = store.get_run(run["id"])
    assert got["state"] == "interrupted" and "hub stopped" in got["error"]
