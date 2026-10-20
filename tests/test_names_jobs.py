"""Speakable names for jobs and job runs (rook/core/names.py): assigned on
create, backfilled on open, accepted wherever a job or run id is (exact for
writes, one clear near miss for reads), carried in replies as ``spoken``, and
matched by the ``list`` search. Covers the store, the rook_jobs tool (and so
job.read / job.write), the dashboard's account API and the TUI client."""
import sqlite3
import urllib.error
from unittest.mock import Mock

import httpx
import pytest
from starlette.applications import Starlette

from rook.cli import band_tui as tui
from rook.core import names
from rook.hub.plugins.jobs import web as jobs_web
from rook.hub.plugins.jobs.store import JobStore

from tests.jobs_fakes import OWNER, T0
from tests.test_jobs_plugin import clean_env, node, tool_for  # noqa: F401 - fixture
from tests.test_jobs_web import ACCOUNTS, ADMIN, TOKEN

JOB = {"name": "nightly", "entry": "a", "triggers": [{"kind": "cron", "expr": "0 3 * * *"}],
       "steps": {"a": {"kind": "noop"}}}


def _rename(store: JobStore, kind: str, rid: str, name: str) -> None:
    with store._lock:
        store._db.execute("UPDATE record_names SET name=? WHERE kind=? AND id=?", (name, kind, rid))


def _named(path) -> dict:
    db = sqlite3.connect(path)
    try:
        return {(k, i): n for k, i, n in db.execute("SELECT kind, id, name FROM record_names")}
    finally:
        db.close()


# -- the store --------------------------------------------------------------------

def test_create_and_enqueue_assign_names(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    job = s.create_job(dict(JOB), OWNER, T0, "UTC")
    run = s.enqueue(job, "manual", T0)
    jn, rn = s.name_of("job", job["id"]), s.name_of("run", run["id"])
    assert names.is_name(jn) and names.is_name(rn)
    assert s.resolve("job", jn.replace("-", " ").upper(), write=True).id == job["id"]
    assert s.resolve("run", rn.replace("-", "_"), write=True).id == run["id"]
    assert s.names_of("run", [run["id"]]) == {run["id"]: rn}


def test_backfill_on_open_is_idempotent(tmp_path):
    path = tmp_path / "jobs.db"
    s = JobStore(path)
    jobs = [s.create_job({**JOB, "name": f"j{i}"}, OWNER, T0, "UTC") for i in range(3)]
    runs = [s.enqueue(j, "manual", T0) for j in jobs for _ in range(2)]
    with s._lock:
        names.drop(s._db)          # as a release before names left it
    s.close()
    s = JobStore(path)
    first = _named(path)
    assert {(k, i) for k, i in first} == {("job", j["id"]) for j in jobs} | {("run", r["id"]) for r in runs}
    assert all(names.is_name(n) for n in first.values())
    s.close()
    JobStore(path).close()
    assert _named(path) == first


def test_names_stay_reserved_after_delete_and_prune(tmp_path):
    # A deleted job's or pruned run's name is never handed to a new record,
    # so an old mention of it can't reach a different job.
    s = JobStore(tmp_path / "jobs.db")
    keep = s.create_job({**JOB, "name": "keep"}, OWNER, T0, "UTC")
    gone = s.create_job({**JOB, "name": "gone"}, OWNER, T0, "UTC")
    r1 = s.enqueue(gone, "manual", T0)
    old = s.name_of("job", gone["id"])
    s.delete_job(gone["id"])
    assert s.name_of("job", gone["id"]) == old and s.name_of("run", r1["id"])
    assert s.get_job(s.resolve("job", old).id) is None
    r2 = s.enqueue(keep, "manual", T0, state="dropped")
    s.prune(T0 + 400 * 86400, 30)
    assert s.name_of("run", r2["id"]) and s.name_of("job", keep["id"])


# -- the rook_jobs tool (job.read / job.write) ---------------------------------------

async def _setup(tmp_path):
    n = node(tmp_path)
    jobs = tool_for(n)
    store = n.plugin("job").store
    a = (await jobs(action="create", data={**JOB, "name": "alpha"}))["result"]
    b = (await jobs(action="create", data={**JOB, "name": "beta"}))["result"]
    _rename(store, "job", a["id"], "brave-otter-lamp")
    _rename(store, "job", b["id"], "quiet-heron-camp")
    return n, jobs, store, a, b


@pytest.mark.asyncio
async def test_job_refs_by_id_name_and_spoken(tmp_path):
    n, jobs, store, a, b = await _setup(tmp_path)
    assert a["spoken"] and a["spoken"] != b["spoken"]       # assigned on create, in the reply
    for ref in (a["id"], "alpha", "brave-otter-lamp", "Brave Otter lamp", "brave_otter_LAMP"):
        got = await jobs(action="get", id=ref)
        assert got["ok"] and got["result"]["id"] == a["id"], (ref, got)
        assert got["result"]["spoken"] == "brave-otter-lamp" and "resolved" not in got["result"]
    upd = await jobs(action="update", id="Brave Otter Lamp", data={"description": "d"})
    assert upd["ok"] and upd["result"]["description"] == "d"
    assert (await jobs(action="disable", id="brave otter lamp"))["result"]["enabled"] is False
    assert (await jobs(action="enable", id="brave_otter_lamp"))["result"]["enabled"] is True
    nxt = (await jobs(action="next", id="quiet heron camp"))["result"]
    assert nxt["id"] == b["id"] and nxt["spoken"] == "quiet-heron-camp"


@pytest.mark.asyncio
async def test_fuzzy_reads_resolve_writes_refuse(tmp_path):
    n, jobs, store, a, b = await _setup(tmp_path)
    got = await jobs(action="get", id="brave otter lamps")
    assert got["ok"] and got["result"]["id"] == a["id"]
    assert got["result"]["resolved"] == {"from": "brave otter lamps", "via": "fuzzy"}
    for action in ("update", "delete", "disable", "run", "cancel"):
        out = await jobs(action=action, id="brave otter lamps", data={"description": "x"})
        assert not out["ok"] and out["code"] == "NameError_", (action, out)
        assert out["candidates"] == [{"id": a["id"], "name": "brave-otter-lamp"}]
        assert "exact name" in out["error"]
    assert (await jobs(action="get", id=a["id"]))["result"]["description"] == ""
    nothing = await jobs(action="get", id="purple zebra kite")
    assert not nothing["ok"] and nothing["code"] == "NameError_" and "candidates" not in nothing


@pytest.mark.asyncio
async def test_ambiguous_read_returns_candidates(tmp_path):
    n, jobs, store, a, b = await _setup(tmp_path)
    _rename(store, "job", b["id"], "brave-otter-camp")
    out = await jobs(action="get", id="brave otter damp")
    assert not out["ok"] and out["ambiguous"] is True
    assert {c["id"] for c in out["candidates"]} == {a["id"], b["id"]}


@pytest.mark.asyncio
async def test_runs_by_spoken_name(tmp_path):
    n, jobs, store, a, b = await _setup(tmp_path)
    run = (await jobs(action="run", id="brave otter lamp"))["result"]
    assert run["job_id"] == a["id"] and names.is_name(run["spoken"])
    _rename(store, "run", run["id"], "amber-falcon-reed")
    got = (await jobs(action="run_get", id="Amber Falcon Reed"))["result"]
    assert got["id"] == run["id"] and got["spoken"] == "amber-falcon-reed"
    fuzzy = (await jobs(action="run_get", id="amber falcon reeds"))["result"]
    assert fuzzy["id"] == run["id"] and fuzzy["resolved"]["via"] == "fuzzy"
    listed = (await jobs(action="runs", id="brave-otter-lamp"))["result"]["runs"]
    assert [(r["id"], r["spoken"]) for r in listed] == [(run["id"], "amber-falcon-reed")]
    view = (await jobs(action="get", id=a["id"]))["result"]
    assert view["last_run"]["spoken"] == "amber-falcon-reed"
    refused = await jobs(action="cancel", id="amber falcon reeds")
    assert not refused["ok"] and refused["code"] == "NameError_"
    done = (await jobs(action="cancel", id="amber_falcon_reed"))["result"]["cancelled"]
    assert done == [{"id": run["id"], "spoken": "amber-falcon-reed", "state": "cancelled",
                     "cancel_requested": False}]
    again = (await jobs(action="run", id=b["id"]))["result"]
    by_job = (await jobs(action="cancel", id="quiet heron camp"))["result"]["cancelled"]
    assert [c["id"] for c in by_job] == [again["id"]]


@pytest.mark.asyncio
async def test_list_carries_and_searches_spoken(tmp_path):
    n, jobs, store, a, b = await _setup(tmp_path)
    listed = (await jobs())["result"]["jobs"]
    assert {j["id"]: j["spoken"] for j in listed} == {a["id"]: "brave-otter-lamp", b["id"]: "quiet-heron-camp"}
    for q in ("otter lamp", "Brave_Otter", "brave-otter-lamp"):
        found = (await jobs(query=q))["result"]["jobs"]
        assert [j["id"] for j in found] == [a["id"]], q
    assert [j["name"] for j in (await jobs(query="beta"))["result"]["jobs"]] == ["beta"]
    up = (await jobs(action="next"))["result"]["upcoming"]
    assert {u["job_id"]: u["spoken"] for u in up}[a["id"]] == "brave-otter-lamp"


@pytest.mark.asyncio
async def test_delete_by_exact_spoken_name(tmp_path):
    n, jobs, store, a, b = await _setup(tmp_path)
    assert (await jobs(action="delete", id="Quiet Heron Camp"))["result"]["deleted"] == b["id"]
    gone = await jobs(action="get", id="quiet heron camp")
    assert not gone.get("ok", True) or "error" in gone, gone   # the name stays reserved, the job is gone


# -- the dashboard API and the terminal panel ------------------------------------------

@pytest.mark.asyncio
async def test_account_api_takes_names_and_returns_candidates(tmp_path):
    n, jobs, store, a, b = await _setup(tmp_path)
    app = Starlette(routes=jobs_web.routes(lambda: n, ACCOUNTS, TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "get", "id": "Brave Otter Lamp"})
        assert r.status_code == 200 and r.json()["result"]["id"] == a["id"]
        r = await c.post(jobs_web.PATH, headers=ADMIN,
                         json={"csrf": "k", "action": "delete", "id": "brave otter lamps"})
        assert r.status_code == 404
        assert r.json()["candidates"] == [{"id": a["id"], "name": "brave-otter-lamp"}]


def test_tui_shows_run_names_and_passes_candidates(monkeypatch):
    assert tui._run_label({"id": "r_1", "spoken": "amber-falcon-reed"}) == "amber-falcon-reed (r_1)"
    assert tui._run_label({"id": "r_1"}) == "r_1"
    band = tui.BandHTTP("https://example.com", "test", "test")

    class Err(urllib.error.HTTPError):
        def read(self):
            return b'{"ok": false, "error": "no job is named \'a-b-c\'", "candidates": [{"id": "j_1", "name": "a-b-d"}]}'
    monkeypatch.setattr(band, "_req", Mock(side_effect=Err("u", 404, "Not Found", {}, None)))
    out = band.jobs("delete", "a b c")
    assert not out["ok"] and out["candidates"] == [{"id": "j_1", "name": "a-b-d"}]
