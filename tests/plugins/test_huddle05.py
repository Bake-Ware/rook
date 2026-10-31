"""Huddles on 0.5 (rook/plugins/huddle): plain group threads on a durable
stream, with role personas. A post naming a role reaches that agent, as the
poster. No chair, pins or votes (castling-workbench); work lands through
github.merge (tests/plugins/test_github_merge.py). Real hub, hard timeouts."""

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from hub_rig import FakeGitHub, config, on_rig  # noqa: E402
from port_helpers import PLUGINS  # noqa: E402
from rook.kit.manifest import check  # noqa: E402


@pytest.fixture
def gh():
    fake = FakeGitHub()
    fake.start()
    yield fake
    fake.stop()


def test_manifest_is_clean():
    raw, issues = check(PLUGINS / "huddle")
    assert raw is not None and not issues, [str(i) for i in issues]
    assert set(raw["caps"]) == {"start", "post", "read", "close"}       # no pin, table, vote


def test_a_huddle_is_a_thread_with_roles(tmp_path, gh):
    async def body(rig):
        h = await rig.bake.ok("huddle.start", title="Release notes for 0.5", roles=["writer", "qa", "ghost"])
        hid = h["huddle"]
        out = await rig.coder.ok("huddle.post", huddle=hid, text="Draft is up; @qa can you check the install steps?")
        assert out["told"] == ["qa"]
        sent = rig.told()
        assert sent[0]["agent"] == "qa" and "install steps" in sent[0]["text"]
        assert sent[0]["from"] == ["person:bake", "agent:coder"]          # as the poster
        # @all: every role but the poster; one that can't be reached says so
        out = await rig.qa.ok("huddle.post", huddle=hid, text="@all looks fine to me")
        assert out["told"] == ["writer"] and "ghost" in out["not_told"]
        # a name that isn't one of its roles reaches nobody
        assert (await rig.amy.ok("huddle.post", huddle=hid, text="ask @pm later"))["told"] == []
        thread = await rig.amy.ok("huddle.read", huddle=hid)
        posts = [(e["by"], e["data"]["text"]) for e in thread["events"] if e["type"] == "text"]
        assert posts[0] == ("agent:coder", "Draft is up; @qa can you check the install steps?")
        assert thread["roles"] == ["writer", "qa", "ghost"] and len(posts) == 3
        assert [x["id"] for x in await rig.amy.ok("huddle.search", query="release")] == [hid]
        # closing: the creator or an owner; the thread stays readable
        assert not (await rig.amy.call("huddle.close", huddle=hid))["ok"]
        assert (await rig.bake.ok("huddle.close", huddle=hid))["state"] == "closed"
        assert not (await rig.coder.call("huddle.post", huddle=hid, text="late"))["ok"]
        assert (await rig.amy.ok("huddle.read", huddle=hid, after=thread["next"]))["closed"]
    on_rig(config(tmp_path, gh), body)


def test_the_thread_survives_a_restart(tmp_path, gh):
    cfg = config(tmp_path, gh)
    got = {}

    async def first(rig):
        h = await rig.bake.ok("huddle.start", title="Keep me")
        await rig.bake.ok("huddle.post", huddle=h["huddle"], text="still here?")
        got["h"] = h["huddle"]

    async def second(rig):
        thread = await rig.amy.ok("huddle.read", huddle=got["h"])
        assert [e["data"]["text"] for e in thread["events"] if e["type"] == "text"] == ["still here?"]
    on_rig(cfg, first)
    on_rig(cfg, second)
