"""rook.kit.contracts: contracts, providers, routing to the chosen one."""

import pytest

from rook.kit.ctx import Caller, KitError
from rook.kit.contracts import Contracts

CORE = {"plugin": "core", "contracts": {
    "notify.send": {"doc": "Get a message to a person.",
                    "args": {"to": {"type": "str"}, "text": {"type": "str", "required": True}}}}}
TELEGRAM = {"plugin": "telegram", "provides": {"notify.send": "telegram.send"},
            "caps": {"send": {"args": {"to": {}, "text": {"required": True}, "silent": {"type": "bool"}}}}}
DISCORD = {"plugin": "discord", "provides": {"notify.send": "post"},
           "caps": {"post": {"args": {"to": {}, "text": {}}}}}
BAKE = Caller(person="bake", agent="claude", why="tell Bake")


def test_default_is_first_installed_and_choice_wins():
    c = Contracts()
    c.load(CORE)
    c.load(TELEGRAM)
    c.load(DISCORD)
    assert c.providers("notify.send") == ["telegram.send", "discord.post"]
    assert c.choose("notify.send") == "telegram.send"
    c.set_choice("notify.send", "discord.post")
    assert c.choose("notify.send") == "discord.post"
    c.set_choice("notify.send", None)
    assert c.choose("notify.send") == "telegram.send"
    with pytest.raises(KitError, match="doesn't provide"):
        c.set_choice("notify.send", "shell.exec")
    assert c.contract("notify.send")["doc"] == "Get a message to a person."
    assert c.contracts() == ["notify.send"]


def test_unload_falls_back_and_choice_returns():
    choices = {"notify.send": "discord.post"}          # the hub setting
    c = Contracts(choices)
    for m in (CORE, TELEGRAM, DISCORD):
        c.load(m)
    c.unload("discord")
    assert c.choose("notify.send") == "telegram.send"
    c.load(DISCORD)
    assert c.choose("notify.send") == "discord.post"   # the pick survives a reinstall
    c.unload("telegram")
    c.unload("discord")
    with pytest.raises(KitError, match="nothing provides"):
        c.choose("notify.send")


@pytest.mark.asyncio
async def test_route_as_the_caller():
    c = Contracts()
    c.load(CORE)
    c.load(TELEGRAM)
    calls = []

    async def dispatch(cap, args, caller):
        calls.append((cap, args, caller))
        return {"sent": True}

    assert await c.route("notify.send", {"text": "hi"}, BAKE, dispatch) == {"sent": True}
    assert calls == [("telegram.send", {"text": "hi"}, BAKE)]
    assert await c.route("notify.send", {"text": "hi"}, BAKE, lambda cap, a, who: cap) == "telegram.send"


def test_compatibility_and_ownership():
    c = Contracts()
    c.load(CORE)
    with pytest.raises(KitError, match="doesn't take text"):
        c.load({"plugin": "sms", "provides": {"notify.send": "send"}, "caps": {"send": {"args": {"to": {}}}}})
    with pytest.raises(KitError, match="requires number"):
        c.load({"plugin": "sms", "provides": {"notify.send": "send"},
                "caps": {"send": {"args": {"to": {}, "text": {}, "number": {"required": True}}}}})
    with pytest.raises(KitError, match="own cap"):
        c.provide("notify.send", "telegram.send", "sms")
    with pytest.raises(KitError, match="already defined"):
        c.load({"plugin": "other", "contracts": {"notify.send": {}}})
    # A provider may arrive before its contract (load order); it's checked when the contract does.
    c.load({"plugin": "tts", "provides": {"voice.tts": "speak"}, "caps": {"speak": {}}})
    assert c.choose("voice.tts") == "tts.speak"
    with pytest.raises(KitError, match="doesn't take text"):
        c.define("voice.tts", {"args": {"text": {"required": True}}}, "voice")
