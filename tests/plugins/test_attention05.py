"""The attention plugin (rook/plugins/attention) on a real 0.5 hub with the
jobs plugin: its ready-made job is off on a fresh hub (Bake, 2026-11-09:
"a fresh install defaults to OFF"), installed from the ready-made list it
calls the house agent with the attention skill (castling-attention: "an aggregation job on the hub
run by the house agent"), and what the house agent curates is attention
records (attention.list), private to the owners. The agents package is a
stand-in (test_jobs.HOUSE) that writes what the house agent was sent."""

import json
import sys
from dataclasses import replace

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import PLUGINS  # noqa: E402
from rook.kit.manifest import check  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.kit.ctx import Caller as KitCaller  # noqa: E402
from test_jobs import HOUSE, HOUSE_PY, Door, Rig, _owner, cfg, plugin, run, until  # noqa: E402,F401

ATTENTION = PLUGINS / "attention"


def test_manifest_is_clean():
    m, issues = check(ATTENTION / "plugin.yaml")
    assert m is not None and issues == []
    assert (ATTENTION / "skills" / "attention.md").is_file()


def test_the_house_agent_has_the_skill():
    m, _ = check(PLUGINS / "agents" / "plugin.yaml")
    assert "attention" in m["personas"]["house"]["skills"]


@pytest.fixture
def rig_cfg(cfg):
    (cfg.library / "attention").symlink_to(ATTENTION, target_is_directory=True)
    plugin(cfg.library, "agents", HOUSE, {"house.py": HOUSE_PY})
    return replace(cfg, agents={**cfg.agents, "house": {"grants": ["*"]}})


def _jobs_rt(rig):
    return next(m for m in rig.hub.plugins._modules["job"].values() if hasattr(m, "_RT"))._RT


def _attention_jobs(rig):
    return [j for archived in (False, True)
            for j in rig.hub.records.type("job.job").list(_owner(), archived=archived)
            if j["source"] == "attention/attention"]


def test_off_until_installed_then_a_house_agent_job(rig_cfg):
    async def body():
        rig = await Rig(rig_cfg).start()
        inbox = rig.hub.data / "plugins" / "agent" / "inbox.jsonl"
        try:
            await _jobs_rt(rig).seed()
            assert _attention_jobs(rig) == []
            offered = {t["template"]: t for t in await rig.bake.ok("job.templates")}
            assert offered["attention/attention"]["installed"] is False

            # guided setup's yes: the house agent installs it for the person
            house = Door(rig.hub, rig.hub.add_token(Caller(person="person:bake", agent="agent:house"), label="h"))
            job = await house.ok("job.install", template="attention/attention")
            assert (job["run_as"], job["persona"], job["enabled"]) == ("persona", "house", True)
            assert job["trigger"] == {"every": "15m"} and job["call"] == "agent.chat.send"
            assert job["args"]["agent"] == "house" and "attention skill" in job["args"]["text"]

            r = await rig.bake.ok("job.run", job=job["id"], wait=True)
            assert r["state"] == "ok" and r["as_who"] == "agent:house", r
            sent = [json.loads(x) for x in inbox.read_text().splitlines()]
            assert sent[-1]["agent"] == "house" and sent[-1]["from"] == "agent:house"
            assert sent[-1]["text"] == job["args"]["text"] and sent[-1]["wait"] is True
        finally:
            await rig.stop()
    run(body)


def test_what_it_curates_is_private_records(rig_cfg):
    async def body():
        rig = await Rig(rig_cfg).start()
        try:
            got = await rig.bake.ok("attention.create", summary="The car warranty ends Friday: call the dealer.",
                                    source="phone: Gmail, the dealer", whose="person:bake", due=1793900000.0)
            assert got["summary"].startswith("The car warranty")
            assert [i["id"] for i in await rig.bake.ok("attention.list")] == [got["id"]]
            assert [i["id"] for i in await rig.bake.ok("attention.search", query="warranty")] == [got["id"]]
            assert await rig.amy.ok("attention.list") == []          # someone else's: not theirs to see
            house = Door(rig.hub, rig.hub.add_token(Caller(agent="agent:house"), label="house"))
            mine = await house.ok("attention.create", summary="School trip form due Monday.", whose="person:bake")
            assert [i["id"] for i in await house.ok("attention.list")] == [mine["id"]]   # its own, to curate
            assert {i["id"] for i in await rig.bake.ok("attention.list")} == {got["id"], mine["id"]}
            done = await rig.bake.ok("attention.archive", id=got["id"], revision=got["revision"])
            assert done["archived"]
        finally:
            await rig.stop()
    run(body)


def test_an_attention_job_seeded_by_an_older_hub_keeps_running(rig_cfg):
    """Bake's hub had it installed active by the old manifest (enabled: true):
    after the upgrade seeding leaves it as it is, refreshed but not archived."""
    async def body():
        rig = await Rig(rig_cfg).start()
        try:
            rt = _jobs_rt(rig)
            m = next(m for m in await rt.manifests() if m["plugin"] == "attention")
            old = {**m, "version": "0.4.9", "jobs": {"attention": {**m["jobs"]["attention"], "enabled": True}}}
            jobs = sys.modules[type(rt).__module__]
            fields = {**jobs.template_fields(old, "attention", old["jobs"]["attention"]), "enabled": True}
            jobs._as(rt.ctx, KitCaller(plugin="attention", band=rt.band)).records.job.create(**fields)

            await rt.seed()
            got = _attention_jobs(rig)
            assert len(got) == 1 and not got[0]["archived"] and got[0]["enabled"] is True
            assert got[0]["creator"] == "plugin:attention" and got[0]["source_version"] == m["version"]
        finally:
            await rig.stop()
    run(body)
