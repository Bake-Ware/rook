"""The attention plugin (rook/plugins/attention) on a real 0.5 hub with the
jobs plugin: its ready-made job is installed active and runs the house agent
with the attention skill (castling-attention: "an aggregation job on the hub
run by the house agent"), and what the house agent curates is attention
records (attention.list), private to the owners. The agents package is a
stand-in (test_jobs.HOUSE) that writes what the house agent was sent."""

import json
from dataclasses import replace

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import PLUGINS  # noqa: E402
from rook.kit.manifest import check  # noqa: E402
from test_jobs import HOUSE, HOUSE_PY, Rig, _owner, cfg, plugin, run, until  # noqa: E402,F401

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


def test_attention_pass_is_a_house_agent_job(rig_cfg):
    async def body():
        rig = await Rig(rig_cfg).start()
        inbox = rig.hub.data / "plugins" / "agents" / "inbox.jsonl"
        try:
            job = (await until(lambda: [j for j in rig.hub.records.type("jobs.jobs").list(_owner())
                                        if j["source"] == "attention/attention"], what="the seeded job"))[0]
            assert (job["creator"], job["run_as"], job["persona"], job["enabled"]) == (
                "plugin:attention", "persona", "house", True)
            assert job["trigger"] == {"every": "15m"} and "attention skill" in job["agent"]["prompt"]

            r = await rig.bake.ok("jobs.start", job=job["id"], wait=True)
            assert r["state"] == "ok" and r["as_who"] == "agent:house", r
            sent = [json.loads(x) for x in inbox.read_text().splitlines()]
            assert sent[-1]["agent"] == "house" and sent[-1]["from"] == "agent:house"
            assert sent[-1]["text"] == job["agent"]["prompt"] and sent[-1]["wait"] is True
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
            done = await rig.bake.ok("attention.archive", id=got["id"], revision=got["revision"])
            assert done["archived"]
        finally:
            await rig.stop()
    run(body)
