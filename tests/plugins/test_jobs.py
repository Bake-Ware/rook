"""The jobs plugin (rook/plugins/jobs) on a real 0.5 hub, in-process with a
loopback band, driven through the HTTP door: schedules, events, webhooks,
scripts and their workflow library, identities, retries, asks, recovery
after a restart, and templates from other plugins' manifests.

A ``probe`` hub plugin gives the jobs something to call, and ``tell``
provides ``notify.send`` (it writes what it was asked to send to a file).
Every test body runs under a hard timeout; HTTP requests run in a thread
with their own socket timeout."""

import asyncio
import hashlib
import hmac
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
    args: { text: { type: str } }
  flaky:
    doc: Fail until the nth call.
    risk: write
    scope: self
    on: hub
    run: scripts/probe.py:flaky
    args: { key: { type: str, required: true }, fails: { type: int, default: 0 } }
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
COUNTS = {}

def echo(ctx, text=None):
    return {"text": text, "person": ctx.person, "agent": ctx.agent}

def flaky(ctx, key, fails=0):
    COUNTS[key] = COUNTS.get(key, 0) + 1
    if COUNTS[key] <= fails:
        raise ctx.Error(f"not yet ({COUNTS[key]})", retry=True)
    return {"tries": COUNTS[key]}

def happen(ctx, what):
    ctx.emit("probe.happened", {"what": what})
    return {"ok": True}

def me(ctx, request):
    return {"person": ctx.person, "query": request["query"]}
"""

TELL = """\
manifest: 1
plugin: tell
version: 1.0.0
doc: A notify.send provider that writes to a file.
runs_on: hub
caps:
  send:
    doc: Write the message down.
    risk: write
    scope: self
    on: hub
    run: scripts/tell.py:send
    args:
      text: { type: str, required: true }
      to: { type: str }
      title: { type: str }
provides:
  notify.send: tell.send
"""

TELL_PY = """\
import json

def send(ctx, text, to=None, title=None):
    with open(ctx.data / "sent.jsonl", "a") as f:
        f.write(json.dumps({"text": text, "to": to, "title": title}) + "\\n")
    return {"sent": True}
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
    plugin(lib, "tell", TELL, {"tell.py": TELL_PY})
    (lib / "jobs").symlink_to(PLUGINS / "jobs", target_is_directory=True)
    return Config(data=tmp_path / "data", band=BAND, library=lib,
                  users={"bake": {"preset": "owner"}, "amy": {"preset": "adult"}, "pat": {"preset": "adult"}},
                  agents={"helper": {"grants": ["probe.echo", "notify.send"]}},
                  settings={"jobs": {"tick": "50ms", "retry_after": "50ms", "timeout": "20s"}},
                  contracts={"notify.send": "tell.send"}, doors={"http": {"bind": "127.0.0.1:0"}})


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

    def sent(self) -> list[dict]:
        f = self.hub.data / "plugins" / "tell" / "sent.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def runs(self, job: str) -> list[dict]:
        return self.hub.records.type("jobs.run").list(_owner(), where={"job": job}, order="created", limit=500)

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
    return lambda: [r for r in rig.runs(job) if r["state"] not in ("running", "waiting")][n - 1:] and \
        [r for r in rig.runs(job) if r["state"] not in ("running", "waiting")]


# -- cron and checks (no hub) ---------------------------------------------------------

def test_manifest_is_clean():
    raw, issues = check(PLUGINS / "jobs")
    assert raw is not None and [i.code for i in issues] == ["route-auth-none"], [str(i) for i in issues]


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
                     ({**base, "trigger": {"webhook": "my-secret"}}, "webhook: true"),
                     ({**base, "trigger": {"every": "5m", "filter": {"a": 1}}}, "goes with on"),
                     ({**base, "script": "def main(job): pass"}, "exactly one of call"),
                     ({"name": "j", "trigger": {"manual": True}, "agent": {"persona": "pm"}}, "prompt"),
                     ({**base, "call": None, "agent": {"prompt": "hi"}, "worker": "nas"}, "worker goes with"),
                     ({**base, "run_as": "persona"}, "names its persona")):
        with pytest.raises(KitError, match=msg):
            check_job(job, owner=True)
    for job, msg in (({**base, "run_as": "persona", "persona": "pm"}, "owner"),
                     ({**base, "call": None, "script": "x"}, "owner"),
                     ({**base, "trigger": {"on": "worker.online"}}, "owner")):
        with pytest.raises(KitError, match=msg):
            check_job(job, owner=False)
        check_job(job, owner=True)


# -- on the hub ------------------------------------------------------------------------

def test_every_job_runs_as_its_creator_on_the_named_worker(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            job = await rig.amy.ok("jobs.create", name="tick", trigger={"every": "1s"}, call="probe.echo",
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
            assert [j["name"] for j in await rig.amy.ok("jobs.list")] == ["tick"]
            assert await rig.pat.ok("jobs.list") == [] and await rig.pat.ok("jobs.run.list") == []
            assert len(await rig.bake.ok("jobs.run.list", where={"job": job["id"]})) >= 2
            assert not (await rig.pat.call("jobs.start", job="tick"))["ok"]
            # a name is the band's once
            out = await rig.pat.call("jobs.create", name="tick", trigger={"manual": True}, call="probe.echo")
            assert not out["ok"] and "already called tick" in out["error"]
            # off: no more runs
            got = await rig.amy.ok("jobs.update", id="tick", revision=job["revision"], enabled=False)
            n = len(rig.runs(job["id"]))
            await asyncio.sleep(1.5)
            assert len(rig.runs(job["id"])) <= n + 1 and got["revision"] == job["revision"] + 1
        finally:
            await rig.stop()
    run(body)


def test_script_jobs_and_the_workflow_library(cfg):
    script = """
import time

def main(job):
    print("starting", job.trigger, job.vars)
    a, b = job.parallel(lambda: job.call("probe.echo", {"text": "a"}, why="first"),
                        lambda: job.call("probe.echo", {"text": "b"}, on="rook"))
    tries = job.retry(lambda: job.call("probe.flaky", {"key": "k", "fails": 1}), times=3, wait="10ms")
    try:
        job.call("probe.nope")
    except job.Error as e:
        job.log("refused:", e.message)
    t = time.monotonic(); job.wait("50ms")
    job.notify("all done", title="Script")
    return {"a": a["text"], "b": b["text"], "tries": tries["tries"], "waited": time.monotonic() - t >= 0.05,
            "vars": job.vars, "last": job.last}
"""

    async def body():
        rig = await Rig(cfg).start()
        try:
            out = await rig.amy.call("jobs.create", name="s", trigger={"manual": True}, script=script)
            assert not out["ok"] and "owner" in out["error"]              # scripts run on the hub
            job = await rig.bake.ok("jobs.create", name="s", trigger={"manual": True}, script=script,
                                    vars={"x": 1})
            r = await rig.bake.ok("jobs.start", job="s", vars={"y": 2}, wait=True)
            assert r["state"] == "ok", r
            assert r["result"] == {"a": "a", "b": "b", "tries": 2, "waited": True, "vars": {"x": 1, "y": 2},
                                   "last": None}
            assert r["log"][0] == "starting manual {'x': 1, 'y': 2}"
            assert any(line.startswith("refused: ") and "probe.nope" in line for line in r["log"])
            assert rig.sent()[-1] == {"text": "all done", "to": "bake", "title": "Script"}
            whys = {e["why"] for e in rig.calls("probe.echo")}
            assert {"job s: first", "job s: probe.echo"} <= whys
            assert all(e["person"] == "person:bake" for e in rig.calls("probe.echo"))
            r2 = await rig.bake.ok("jobs.start", job=job["id"], wait=True)
            assert r2["result"]["last"] == r["started"]                  # the last good run

            # a script that raises, one that never says done, one that prints garbage
            for name, src, err in (("boom", "def main(job):\n    raise ValueError('nope')\n", "ValueError: nope"),
                                   ("gone", "import os\ndef main(job):\n    os._exit(3)\n", "exit 3"),
                                   ("nomain", "x = 1\n", "no function main")):
                await rig.bake.ok("jobs.create", name=name, trigger={"manual": True}, script=src, notify="never")
                r = await rig.bake.ok("jobs.start", job=name, wait=True)
                assert r["state"] == "failed" and err in r["error"], r
        finally:
            await rig.stop()
    run(body)


def test_ask_answer_cancel_and_timeout(cfg):
    asker = """
def main(job):
    a = job.ask("Ship it?", ["yes", "no"], timeout="20s")
    b = job.ask("Anyone?", timeout="100ms")
    return [a, b]
"""

    async def body():
        rig = await Rig(cfg).start()
        try:
            await rig.bake.ok("jobs.create", name="asker", trigger={"manual": True}, script=asker)
            rid = (await rig.bake.ok("jobs.start", job="asker"))["run"]
            await until(lambda: rig.hub.records.type("jobs.run").get(_owner(), rid)["state"] == "waiting",
                        what="the ask")
            rec = rig.hub.records.type("jobs.run").get(_owner(), rid)
            assert rec["asking"] == {"question": "Ship it?", "options": ["yes", "no"]}
            # The one ask (castling-caps C4): it waits on Approvals for the job's person.
            asks = [h for h in (await rig.bake.ok("approval.list"))["holds"] if h["kind"] == "ask"]
            assert [(a["question"], a["options"], a["person"]) for a in asks] == [
                ("Ship it?", ["yes", "no"], "person:bake")]
            hold = asks[0]["hold"]
            assert not (await rig.pat.call("approval.decide", hold=hold, answer="yes"))["ok"]     # not pat's
            out = await rig.bake.call("approval.decide", hold=hold, answer="maybe")
            assert not out["ok"] and "one of: yes, no" in out["error"]
            assert (await rig.bake.ok("approval.decide", hold=hold, answer="yes"))["answered"]
            assert rig.sent() == []                       # no notice of its own: the ask is the card
            rec = await until(lambda: (r := rig.hub.records.type("jobs.run").get(_owner(), rid))["state"] == "ok"
                              and r, what="the run")
            assert rec["result"] == ["yes", None] and rec["asking"] is None

            await rig.bake.ok("jobs.create", name="sleeper", trigger={"manual": True}, notify="never",
                              script="import time\ndef main(job):\n    time.sleep(30)\n")
            rid = (await rig.bake.ok("jobs.start", job="sleeper"))["run"]
            out = await rig.bake.call("jobs.start", job="sleeper")
            assert not out["ok"] and "still running" in out["error"]            # one run at a time
            await asyncio.sleep(0.3)
            assert (await rig.bake.ok("jobs.cancel", run=rid))["state"] == "cancelled"
            assert rig.hub.records.type("jobs.run").get(_owner(), rid)["state"] == "cancelled"

            await rig.bake.ok("jobs.create", name="slow", trigger={"manual": True}, notify="never",
                              timeout="300ms", script="import time\ndef main(job):\n    time.sleep(30)\n")
            r = await rig.bake.ok("jobs.start", job="slow", wait=True)
            assert r["state"] == "failed" and r["error"] == "timed out after 300ms"
        finally:
            await rig.stop()
    run(body)


def test_retries_then_notice(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            job = await rig.amy.ok("jobs.create", name="flaky", trigger={"manual": True}, call="probe.flaky",
                                   args={"key": "f1", "fails": 2}, retries=2)
            await rig.amy.ok("jobs.start", job="flaky")
            runs = await until(lambda: (r := rig.runs(job["id"])) and r[-1]["state"] == "ok" and r, what="ok")
            assert [(r["attempt"], r["trigger"], r["state"]) for r in runs] == [
                (1, "manual", "failed"), (2, "retry", "failed"), (3, "retry", "ok")]
            assert rig.sent() == []                                  # it came good: nobody told

            job = await rig.amy.ok("jobs.create", name="broken", trigger={"manual": True}, call="probe.flaky",
                                   args={"key": "f2", "fails": 5}, retries=1)
            await rig.amy.ok("jobs.start", job="broken")
            await until(lambda: rig.sent(), what="the notice")
            assert rig.sent() == [{"text": "Job broken failed: not yet (2)", "to": "amy", "title": "Job broken"}]
            assert [r["state"] for r in rig.runs(job["id"])] == ["failed", "failed"]
        finally:
            await rig.stop()
    run(body)


def test_event_jobs_with_filters(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            out = await rig.amy.call("jobs.create", name="ev", trigger={"on": "probe.*"}, call="probe.echo")
            assert not out["ok"] and "owner" in out["error"]
            job = await rig.bake.ok("jobs.create", name="ev", trigger={"on": "probe.happened",
                                                                       "filter": {"what": ["door", "window"]}},
                                    script="def main(job):\n    return job.payload\n")
            await rig.bake.ok("probe.happen", what="cat")
            await rig.bake.ok("probe.happen", what="door")
            runs = await until(done(rig, job["id"]), what="the event run")
            assert runs[0]["trigger"] == "event" and runs[0]["result"] == {"what": "door"}
            assert runs[0]["event"] == {"name": "probe.happened", "payload": {"what": "door"}}
            await asyncio.sleep(0.3)
            assert len(rig.runs(job["id"])) == 1                     # cat didn't pass the filter
        finally:
            await rig.stop()
    run(body)


def test_webhooks_are_signed_per_job(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            job = await rig.amy.ok("jobs.create", name="hook", trigger={"webhook": True}, call="probe.echo",
                                   args={"text": "pushed"})
            hook = job["webhook"]
            assert hook == await rig.amy.ok("jobs.webhook", id="hook")
            assert not (await rig.pat.call("jobs.webhook", id=job["id"]))["ok"]
            body_ = json.dumps({"ref": "main"}).encode()
            sig = "sha256=" + hmac.new(hook["key"].encode(), body_, hashlib.sha256).hexdigest()
            anon = Door(rig.hub, "")
            status, out = await anon.request("POST", hook["path"], body_, auth=False)
            assert status == 401 and out["error"] == "unknown job or bad signature"
            status, out = await anon.request("POST", hook["path"].replace(job["id"], "r_0000000000000000"), body_,
                                             {"X-Rook-Signature": sig}, auth=False)
            assert status == 401 and out["error"] == "unknown job or bad signature"
            status, out = await anon.request("POST", hook["path"], body_, {"X-Hub-Signature-256": sig}, auth=False)
            assert status == 200 and out["ok"], out
            runs = await until(done(rig, job["id"]), what="the webhook run")
            assert runs[0]["trigger"] == "webhook" and runs[0]["event"]["payload"] == {"ref": "main"}
            assert runs[0]["result"]["person"] == "person:amy"
            assert (await anon.request("GET", hook["path"], auth=False))[0] == 405
            assert (await anon.request("POST", "/p/jobs/nope", b"{}", auth=False))[0] == 404
            # auth: user routes take the bearer token
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
            sweep = await until(lambda: [j for j in rig.hub.records.type("jobs.jobs").list(_owner())
                                         if j["source"] == "probe/sweep"], what="the seeded job")
            assert sweep[0]["creator"] == "plugin:probe" and sweep[0]["run_as"] == "persona"
            runs = await until(done(rig, sweep[0]["id"]), what="a sweep run")
            assert runs[0]["result"] == {"text": "seeded", "person": None, "agent": "agent:helper"}
            assert runs[0]["as_who"] == "agent:helper"

            out = await rig.amy.call("jobs.create", name="p", trigger={"manual": True}, call="probe.echo",
                                     **{"as": "persona", "persona": "helper"})
            assert not out["ok"] and "owner" in out["error"]
            await rig.bake.ok("jobs.create", name="p", trigger={"manual": True}, call="probe.secret",
                              notify="never", **{"as": "persona", "persona": "helper"})
            r = await rig.bake.ok("jobs.start", job="p", wait=True)
            assert r["state"] == "failed" and r["error"].startswith("denied: agent"), r

            seen = {m["plugin"]: m for m in (await rig.amy.ok("hub.manifests", section="jobs"))["manifests"]}
            assert set(seen) == {"backup", "jobs", "probe"} and "folder" not in seen["probe"]     # paths: owners only
            assert seen["probe"]["jobs"]["sweep"]["as"] == "persona"
            seen = (await rig.bake.ok("hub.manifests", section="jobs"))["manifests"]
            assert all(m["folder"] for m in seen)
            listed = {t["template"]: t for t in await rig.amy.ok("jobs.templates")}
            assert {"probe/sweep", "probe/offered", "jobs/security-scan"} <= set(listed)
            assert listed["probe/offered"]["installed"] is False and listed["probe/offered"]["does"] == "call"
            mine = await rig.amy.ok("jobs.install", template="probe/offered", name="mine")
            assert (mine["source"], mine["person"], mine["run_as"]) == ("probe/offered", "person:amy", "creator")
            r = await rig.amy.ok("jobs.start", job="mine", wait=True)
            assert r["result"]["person"] == "person:amy"
            assert {t["template"]: t for t in await rig.amy.ok("jobs.templates")}["probe/offered"]["installed"]
            out = await rig.amy.call("jobs.install", template="probe/sweep")
            assert not out["ok"] and "owner" in out["error"]
            assert not (await rig.amy.call("jobs.install", template="probe/nope"))["ok"]

            # an edited seeded job is left alone by plugin updates (edited)
            got = await rig.bake.ok("jobs.update", id=sweep[0]["id"], revision=sweep[0]["revision"],
                                    args={"text": "mine now"})
            assert got["edited"] is True
        finally:
            await rig.stop()
    run(body)


def test_security_scan_template(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            for _ in range(3):        # refused: pat may not read amy's job
                await rig.pat.call("jobs.cancel", run="r_nope")
                await rig.bake.call("probe.nope")
            job = await rig.bake.ok("jobs.install", template="jobs/security-scan", vars={"threshold": 2})
            assert job["trigger"] == {"every": "5m"} and job["plugin"] == "jobs"
            r = await rig.bake.ok("jobs.start", job="security-scan", wait=True)
            assert r["state"] == "ok", r
            assert r["result"]["denied"] >= 3 and r["result"]["told"] is True
            assert rig.sent()[-1]["title"] == "Security scan" and "refused" in rig.sent()[-1]["text"]
        finally:
            await rig.stop()
    run(body)


def test_restart_fails_open_runs_and_catches_up(cfg):
    async def body():
        rig = await Rig(cfg).start()
        await rig.bake.ok("jobs.create", name="long", trigger={"manual": True}, retries=0,
                          script="import time\ndef main(job):\n    time.sleep(30)\n")
        rid = (await rig.bake.ok("jobs.start", job="long"))["run"]
        catch = await rig.bake.ok("jobs.create", name="catch", trigger={"every": "1s"}, call="probe.echo",
                                  catch_up=True, notify="never")
        skip = await rig.bake.ok("jobs.create", name="skip", trigger={"every": "1s"}, call="probe.echo",
                                 notify="never")
        await until(done(rig, catch["id"]), what="a catch run")
        await until(done(rig, skip["id"]), what="a skip run")
        await asyncio.sleep(0.3)
        await rig.stop()                                     # the long run is cut off
        store = RecordStore(str(cfg.data / "records.db"))
        store.register(load(PLUGINS / "jobs").data)
        assert store.type("jobs.run").get(_owner(), rid)["state"] == "running"     # for the next start
        store.close()
        await asyncio.sleep(2.2)                             # slots missed while down

        rig = await Rig(cfg).start()
        try:
            catches = await until(lambda: [r for r in rig.runs(catch["id"]) if r["trigger"] == "catch_up"],
                                  what="the catch-up run")
            assert len(catches) == 1
            await asyncio.sleep(0.5)
            assert not [r for r in rig.runs(skip["id"]) if r["trigger"] == "catch_up"]
            rec = rig.hub.records.type("jobs.run").get(_owner(), rid)
            assert (rec["state"], rec["error"]) == ("failed", "interrupted: the hub restarted before it finished")
            await until(lambda: rig.sent(), what="the notice")
            assert rig.sent() == [{"text": "Job long failed: interrupted: the hub restarted before it finished",
                                   "to": "bake", "title": "Job long"}]
        finally:
            await rig.stop()
    run(body)
