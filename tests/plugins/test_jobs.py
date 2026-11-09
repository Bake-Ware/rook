"""The jobs plugin (rook/plugins/jobs) on a real 0.5 hub, in-process with a
loopback band, driven through the HTTP door: schedules, events (the call
gets the event), identities, timeouts, recovery after a restart, and
templates from other plugins' manifests.

A ``probe`` hub plugin gives the jobs something to call. Every test body
runs under a hard timeout; HTTP requests run in a thread with their own
socket timeout."""

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import PLUGINS  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from rook.kit.ctx import KitError, load_run  # noqa: E402
from rook.kit.ctx import Caller as KitCaller  # noqa: E402
from rook.kit.manifest import check, load  # noqa: E402
from rook.kit.records import RecordStore  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the jobs plugin."
jobs_mod = sys.modules[load_run(PLUGINS / "jobs", "scripts/jobs.py:Cron").__module__]

PROBE = """\
manifest: 1
plugin: probe
version: 1.0.0
doc: Caps the jobs tests call.
runs_on: hub
uses: ["probe.*"]
emits:
  probe.happened: { doc: Something happened., payload: { what: { type: str } } }
caps:
  echo:
    doc: Say who called.
    risk: read
    scope: self
    on: hub
    run: scripts/probe.py:echo
    args: { text: { type: str }, event: { type: map } }
  sleep:
    doc: Sleep a while.
    risk: read
    scope: self
    on: hub
    run: scripts/probe.py:sleep
  happen:
    doc: Emit probe.happened.
    risk: write
    scope: self
    on: hub
    run: scripts/probe.py:happen
    args: { what: { type: str, required: true } }
  secret:
    doc: Something the helper persona may not call.
    risk: read
    scope: self
    on: hub
    run: scripts/probe.py:echo
routes:
  me: { path: /me, methods: [GET], run: scripts/probe.py:me }
jobs:
  sweep:
    doc: Installed active, as the helper persona.
    trigger: { every: 1s }
    call: probe.echo
    args: { text: seeded }
    as: persona
    persona: helper
    enabled: true
  offered:
    doc: Offered for a person to install.
    trigger: { manual: true }
    call: probe.echo
    args: { text: installed }
    enabled: true
"""

PROBE_PY = """\
import asyncio

def echo(ctx, text=None, event=None):
    return {"text": text, "person": ctx.person, "agent": ctx.agent, **({"event": event} if event else {})}

async def sleep(ctx):
    await asyncio.sleep(30)

def happen(ctx, what):
    ctx.emit("probe.happened", {"what": what})
    return {"ok": True}

def me(ctx, request):
    return {"person": ctx.person, "query": request["query"]}
"""

def plugin(lib: Path, name: str, text: str, scripts: dict) -> None:
    (lib / name / "scripts").mkdir(parents=True)
    (lib / name / "plugin.yaml").write_text(text)
    for f, body in scripts.items():
        (lib / name / "scripts" / f).write_text(body)


@pytest.fixture
def cfg(tmp_path):
    lib = tmp_path / "lib"
    plugin(lib, "probe", PROBE, {"probe.py": PROBE_PY})
    (lib / "jobs").symlink_to(PLUGINS / "jobs", target_is_directory=True)
    return Config(data=tmp_path / "data", band=BAND, library=lib,
                  users={"bake": {"preset": "owner"}, "amy": {"preset": "adult"}, "pat": {"preset": "adult"}},
                  agents={"helper": {"grants": ["probe.echo"]}},
                  settings={"job": {"tick": "50ms", "timeout": "20s"}},
                  doors={"http": {"bind": "127.0.0.1:0"}})


class Door:
    def __init__(self, hub: Hub, token: str):
        self.hub, self.token = hub, token

    async def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                      auth: bool = True):
        def go():
            req = urllib.request.Request(f"http://127.0.0.1:{self.hub.doors[0].port}{path}", data=body,
                                         method=method)
            if auth:
                req.add_header("Authorization", f"Bearer {self.token}")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        return await asyncio.to_thread(go)

    async def call(self, cap: str, **args):
        _, out = await self.request("POST", "/call", json.dumps(
            {"worker": "rook", "cap": cap, "args": args, "why": WHY}).encode())
        return out

    async def ok(self, cap: str, **args):
        out = await self.call(cap, **args)
        assert out["ok"], out
        return out["result"]


class Rig:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def start(self) -> "Rig":
        self.hub = Hub(self.cfg, transport=Loopback())
        await self.hub.start()
        self.bake = Door(self.hub, (self.hub.data / "owner.token").read_text().strip())
        self.amy = Door(self.hub, self.hub.add_token(Caller(person="person:amy"), label="amy"))
        self.pat = Door(self.hub, self.hub.add_token(Caller(person="person:pat"), label="pat"))
        return self

    async def stop(self) -> None:
        await self.hub.stop()

    def runs(self, job: str) -> list[dict]:
        return self.hub.records.type("job.run").list(_owner(), where={"job": job}, order="created", limit=500)

    def calls(self, cap: str) -> list[dict]:
        return list(reversed(self.hub.journal.query(cap=cap, limit=500)))


def _owner():
    return KitCaller(band=BAND, owner=True)


async def until(pred, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        await asyncio.sleep(0.03)
    raise AssertionError(f"{what} not met in {timeout}s")


def run(body, timeout=60):
    async def main():
        return await asyncio.wait_for(body(), timeout)
    return asyncio.run(main())


def done(rig, job, n=1):
    return lambda: [r for r in rig.runs(job) if r["state"] != "running"][n - 1:] and \
        [r for r in rig.runs(job) if r["state"] != "running"]


# -- cron and checks (no hub) ---------------------------------------------------------

def test_manifest_is_clean():
    raw, issues = check(PLUGINS / "jobs")
    assert raw is not None and issues == [], [str(i) for i in issues]


def test_cron():
    Cron, utc = jobs_mod.Cron, ZoneInfo("UTC")

    def nxt(expr, at, tz=utc):
        ts = datetime.fromisoformat(at).replace(tzinfo=tz).timestamp()
        return datetime.fromtimestamp(Cron(expr).next_after(ts, tz), tz).strftime("%Y-%m-%d %H:%M %a")

    assert nxt("*/15 * * * *", "2026-10-30 10:07") == "2026-10-30 10:15 Fri"
    assert nxt("0 8 * * *", "2026-10-30 08:00") == "2026-10-31 08:00 Sat"           # strictly after
    assert nxt("0 9 * * mon-fri", "2026-10-30 10:00") == "2026-11-02 09:00 Mon"
    assert nxt("0 0 1 jan *", "2026-10-30 10:00") == "2027-01-01 00:00 Fri"
    assert nxt("@weekly", "2026-10-30 10:00") == "2026-11-01 00:00 Sun"
    assert nxt("0 0 13 * 5", "2026-10-30 10:00") == "2026-11-06 00:00 Fri"         # 13th OR Friday
    assert nxt("30 2 * * 7", "2026-10-30 10:00") == "2026-11-01 02:30 Sun"         # 7 = Sunday
    chicago = ZoneInfo("America/Chicago")
    assert nxt("0 8 * * *", "2026-10-30 09:00", chicago) == "2026-10-31 08:00 Sat"
    assert Cron("0 0 30 2 *").next_after(0, utc) is None
    for bad in ("* * * *", "60 * * * *", "* * * * 8", "*/0 * * * *", "x * * * *"):
        with pytest.raises(ValueError):
            Cron(bad)


def test_who_may_make_what():
    check_job = jobs_mod.check_job
    base = {"name": "j", "trigger": {"every": "5m"}, "call": "probe.echo"}
    check_job(base, owner=False)
    for job, msg in (({**base, "trigger": {"every": "5m", "manual": True}}, "exactly one of cron"),
                     ({**base, "trigger": {"cron": "nope"}}, "cron"),
                     ({**base, "trigger": {"every": "0.5s"}}, "1s or more"),
                     ({**base, "trigger": {"webhook": True}}, "exactly one of cron"),
                     ({**base, "trigger": {"manual": "yes"}}, "manual: true"),
                     ({**base, "trigger": {"every": "5m", "filter": {"a": 1}}}, "goes with on"),
                     ({**base, "run_as": "persona"}, "names its persona")):
        with pytest.raises(KitError, match=msg):
            check_job(job, owner=True)
    for job, msg in (({**base, "run_as": "persona", "persona": "pm"}, "owner"),
                     ({**base, "trigger": {"on": "worker.online"}}, "owner")):
        with pytest.raises(KitError, match=msg):
            check_job(job, owner=False)
        check_job(job, owner=True)


# -- on the hub ------------------------------------------------------------------------

def test_every_job_runs_as_its_creator_on_the_named_worker(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            job = await rig.amy.ok("job.create", name="tick", trigger={"every": "1s"}, call="probe.echo",
                                   worker="rook", args={"text": "hi"})
            assert job["creator"] == job["person"] == "person:amy"
            runs = await until(done(rig, job["id"], 2), what="two runs")
            assert [r["state"] for r in runs[:2]] == ["ok", "ok"] and runs[0]["trigger"] == "every"
            assert runs[0]["result"] == {"text": "hi", "person": "person:amy", "agent": None}
            assert runs[1]["scheduled"] - runs[0]["scheduled"] == pytest.approx(1.0)
            entry = rig.calls("probe.echo")[0]
            assert (entry["person"], entry["agent"], entry["decision"]) == ("person:amy", None, "allow")
            assert entry["why"].startswith("job tick (run ")
            # private: amy and the owners see the job and its runs, pat doesn't
            assert [j["name"] for j in await rig.amy.ok("job.list")] == ["tick"]
            assert await rig.pat.ok("job.list") == [] and await rig.pat.ok("job.run.list") == []
            assert len(await rig.bake.ok("job.run.list", where={"job": job["id"]})) >= 2
            assert not (await rig.pat.call("job.run", job="tick"))["ok"]
            assert not (await rig.amy.call("job.create", name="nocall", trigger={"manual": True}))["ok"]
            # a name is the band's once
            out = await rig.pat.call("job.create", name="tick", trigger={"manual": True}, call="probe.echo")
            assert not out["ok"] and "already called tick" in out["error"]
            # off: no more runs
            got = await rig.amy.ok("job.update", id="tick", revision=job["revision"], enabled=False)
            n = len(rig.runs(job["id"]))
            await asyncio.sleep(1.5)
            assert len(rig.runs(job["id"])) <= n + 1 and got["revision"] == job["revision"] + 1
        finally:
            await rig.stop()
    run(body)


HOUSE = """\
manifest: 1
plugin: agent
version: 1.0.0
doc: A stand-in for the agents package (the house agent's inbox, written to a file).
runs_on: hub
caps:
  chat.send:
    doc: Write the message down.
    risk: write
    scope: multi-user
    on: hub
    run: scripts/house.py:send
    args:
      agent: { type: str, required: true }
      text: { type: str, required: true }
      wait: { type: bool, default: true }
"""

HOUSE_PY = """\
import json

def send(ctx, agent, text, wait=True):
    with open(ctx.data / "inbox.jsonl", "a") as f:
        f.write(json.dumps({"agent": agent, "text": text, "wait": wait, "from": ctx.person or ctx.agent}) + "\\n")
    return {"sent": True}
"""


def test_event_jobs_hand_the_event_to_their_call(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            out = await rig.amy.call("job.create", name="ev", trigger={"on": "probe.*"}, call="probe.echo")
            assert not out["ok"] and "owner" in out["error"]
            job = await rig.bake.ok("job.create", name="ev", trigger={"on": "probe.happened",
                                                                       "filter": {"what": ["door", "window"]}},
                                    call="probe.echo", args={"text": "heard"})
            await rig.bake.ok("probe.happen", what="cat")
            await rig.bake.ok("probe.happen", what="door")
            runs = await until(done(rig, job["id"]), what="the event run")
            assert runs[0]["trigger"] == "event" and runs[0]["state"] == "ok", runs
            assert runs[0]["event"] == {"name": "probe.happened", "payload": {"what": "door"}}
            assert runs[0]["result"]["text"] == "heard"
            assert runs[0]["result"]["event"] == {"name": "probe.happened", "payload": {"what": "door"}}
            await asyncio.sleep(0.3)
            assert len(rig.runs(job["id"])) == 1                     # cat didn't pass the filter

            # a cap without an event arg is called with its own args only
            plain = await rig.bake.ok("job.create", name="plain", trigger={"on": "probe.happened"},
                                      call="probe.secret")
            await rig.bake.ok("probe.happen", what="window")
            runs = await until(done(rig, plain["id"]), what="the plain run")
            assert runs[0]["state"] == "ok" and "event" not in runs[0]["result"], runs
        finally:
            await rig.stop()
    run(body)


def test_timeout_and_one_run_at_a_time(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            await rig.bake.ok("job.create", name="slow", trigger={"manual": True}, call="probe.sleep",
                              timeout="300ms")
            rid = (await rig.bake.ok("job.run", job="slow"))["run"]
            out = await rig.bake.call("job.run", job="slow")
            assert not out["ok"] and "still running" in out["error"]
            rec = await until(lambda: (r := rig.hub.records.type("job.run").get(_owner(), rid))["state"] != "running"
                              and r, what="the timeout")
            assert rec["state"] == "failed" and rec["error"] == "timed out after 300ms"
        finally:
            await rig.stop()
    run(body)


def test_user_routes_take_the_bearer_token(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            anon = Door(rig.hub, "")
            assert (await anon.request("GET", "/p/probe/me?x=1", auth=False))[0] == 401
            status, out = await rig.amy.request("GET", "/p/probe/me?x=1")
            assert status == 200 and out == {"person": "person:amy", "query": {"x": "1"}}
        finally:
            await rig.stop()
    run(body)


def test_persona_jobs_and_plugin_templates(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            # probe ships sweep (persona, enabled): installed active as the helper
            sweep = await until(lambda: [j for j in rig.hub.records.type("job.job").list(_owner())
                                         if j["source"] == "probe/sweep"], what="the seeded job")
            assert sweep[0]["creator"] == "plugin:probe" and sweep[0]["run_as"] == "persona"
            runs = await until(done(rig, sweep[0]["id"]), what="a sweep run")
            assert runs[0]["result"] == {"text": "seeded", "person": None, "agent": "agent:helper"}
            assert runs[0]["as_who"] == "agent:helper"

            out = await rig.amy.call("job.create", name="p", trigger={"manual": True}, call="probe.echo",
                                     **{"as": "persona", "persona": "helper"})
            assert not out["ok"] and "owner" in out["error"]
            await rig.bake.ok("job.create", name="p", trigger={"manual": True}, call="probe.secret",
                              **{"as": "persona", "persona": "helper"})
            r = await rig.bake.ok("job.run", job="p", wait=True)
            assert r["state"] == "failed" and r["error"].startswith("denied: agent"), r

            seen = {m["plugin"]: m for m in (await rig.amy.ok("hub.manifests", section="jobs"))["manifests"]}
            assert set(seen) == {"backup", "probe"} and "folder" not in seen["probe"]     # paths: owners only
            assert seen["probe"]["jobs"]["sweep"]["as"] == "persona"
            seen = (await rig.bake.ok("hub.manifests", section="jobs"))["manifests"]
            assert all(m["folder"] for m in seen)
            listed = {t["template"]: t for t in await rig.amy.ok("job.templates")}
            assert {"probe/sweep", "probe/offered"} <= set(listed)
            assert listed["probe/offered"]["installed"] is False and listed["probe/offered"]["call"] == "probe.echo"
            mine = await rig.amy.ok("job.install", template="probe/offered", name="mine")
            assert (mine["source"], mine["person"], mine["run_as"]) == ("probe/offered", "person:amy", "creator")
            r = await rig.amy.ok("job.run", job="mine", wait=True)
            assert r["result"]["person"] == "person:amy"
            assert {t["template"]: t for t in await rig.amy.ok("job.templates")}["probe/offered"]["installed"]
            out = await rig.amy.call("job.install", template="probe/sweep")
            assert not out["ok"] and "owner" in out["error"]
            assert not (await rig.amy.call("job.install", template="probe/nope"))["ok"]

            # an edited seeded job is left alone by plugin updates (edited)
            got = await rig.bake.ok("job.update", id=sweep[0]["id"], revision=sweep[0]["revision"],
                                    args={"text": "mine now"})
            assert got["edited"] is True
        finally:
            await rig.stop()
    run(body)


def test_restart_fails_open_runs_and_catches_up(cfg):
    async def body():
        rig = await Rig(cfg).start()
        await rig.bake.ok("job.create", name="long", trigger={"manual": True}, call="probe.sleep")
        rid = (await rig.bake.ok("job.run", job="long"))["run"]
        catch = await rig.bake.ok("job.create", name="catch", trigger={"every": "1s"}, call="probe.echo",
                                  catch_up=True)
        skip = await rig.bake.ok("job.create", name="skip", trigger={"every": "1s"}, call="probe.echo")
        await until(done(rig, catch["id"]), what="a catch run")
        await until(done(rig, skip["id"]), what="a skip run")
        await asyncio.sleep(0.3)
        await rig.stop()                                     # the long run is cut off
        store = RecordStore(str(cfg.data / "records.db"))
        store.register(load(PLUGINS / "jobs").data)
        assert store.type("job.run").get(_owner(), rid)["state"] == "running"     # for the next start
        store.close()
        await asyncio.sleep(2.2)                             # slots missed while down

        rig = await Rig(cfg).start()
        try:
            catches = await until(lambda: [r for r in rig.runs(catch["id"]) if r["trigger"] == "catch_up"],
                                  what="the catch-up run")
            assert len(catches) == 1
            await asyncio.sleep(0.5)
            assert not [r for r in rig.runs(skip["id"]) if r["trigger"] == "catch_up"]
            rec = rig.hub.records.type("job.run").get(_owner(), rid)
            assert (rec["state"], rec["error"]) == ("failed", "interrupted: the hub restarted before it finished")
        finally:
            await rig.stop()
    run(body)
