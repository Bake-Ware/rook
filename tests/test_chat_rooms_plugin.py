"""The chat rooms hub plugin (rook/hub/plugins/rooms.py): the band side of the
hub's persistent chat rooms (docs/spec/core-v1.md, "Chat rooms")."""

from __future__ import annotations

import pytest

from rook.band_mcp.chat_rooms import ChatStore
from rook.hub.node import HubNode


@pytest.fixture
def node(tmp_path):
    return HubNode(str(tmp_path), entry_points=False, band_max_risk="write",
                   chat=ChatStore(str(tmp_path / "chat.db")))


def test_chat_caps_are_on_the_hub(node):
    assert {"chat.read", "chat.write", "chat.delete", "chat.presence"} <= set(node.caps())
    tiers = node.announce_msg()["tiers"]
    assert tiers["chat.read"] == "r" and tiers["chat.write"] == "w"
    assert tiers["chat.delete"] == "w" and tiers["chat.presence"] == "r"


@pytest.mark.asyncio
async def test_band_callers_are_labelled_band(node):
    started = await node.dispatch("chat.write", {"action": "start", "title": "t",
                                                 "invite": ["agent:other"]},
                                  "agent:port", source="band")
    assert started["ok"], started
    room = started["result"]["room"]
    assert started["result"]["participants"] == ["band:agent:port", "agent:other"]

    sent = await node.dispatch("chat.write", {"action": "send", "room": room, "text": "hi"},
                               "human:operator", source="band")
    assert sent["ok"], sent
    # A band peer cannot post as a person: its self-stamped identity is prefixed.
    read = await node.dispatch("chat.read", {"action": "read", "room": room}, "agent:port",
                               source="band")
    assert [m["sender"] for m in read["result"]["messages"]] == ["band:human:operator"]
    assert read["result"]["last_seq"] == read["result"]["messages"][-1]["seq"]

    rooms = await node.dispatch("chat.read", {"action": "rooms"}, "agent:port", source="band")
    assert [r["room"] for r in rooms["result"]["rooms"]] == [room]


@pytest.mark.asyncio
async def test_local_callers_keep_their_identity(node):
    started = await node.dispatch("chat.write", {"action": "start", "title": "t"}, "agent:claude")
    room = started["result"]["room"]
    await node.dispatch("chat.write", {"action": "send", "room": room, "text": "x",
                                       "mentions": "agent:b"}, "agent:claude")
    read = await node.dispatch("chat.read", {"action": "read", "room": room}, "agent:claude")
    msg = read["result"]["messages"][0]
    assert msg["sender"] == "agent:claude" and msg["mentions"] == ["agent:b"]
    assert "agent:b" in read["result"]["participants"]


@pytest.mark.asyncio
async def test_errors_and_delete(node):
    bad = await node.dispatch("chat.write", {"action": "send", "room": "nope", "text": "x"}, "a")
    assert not bad["ok"] and "no such room" in bad["error"]
    bad = await node.dispatch("chat.read", {"action": "bogus"}, "a")
    assert not bad["ok"] and "action must be" in bad["error"]
    room = (await node.dispatch("chat.write", {"action": "start"}, "agent:a"))["result"]["room"]
    denied = await node.dispatch("chat.delete", {"room": room}, "agent:b")
    assert not denied["ok"] and "participant" in denied["error"]
    gone = await node.dispatch("chat.delete", {"room": room}, "agent:a")
    assert gone["ok"] and gone["result"]["room"] == room
    pres = await node.dispatch("chat.presence", {}, "agent:a")
    assert "agent:a" in {a["identity"] for a in pres["result"]["agents"]}


@pytest.mark.asyncio
async def test_band_write_needs_the_ceiling_raised(tmp_path):
    node = HubNode(str(tmp_path), entry_points=False)  # default ceiling: read
    body = await node.dispatch("chat.write", {"action": "start"}, "agent:x", source="band")
    assert not body["ok"] and "not callable over the band" in body["error"]
    ok = await node.dispatch("chat.read", {"action": "rooms"}, "agent:x", source="band")
    assert ok["ok"]  # the plugin opened chat.db in the state dir by itself
    await node.stop()
