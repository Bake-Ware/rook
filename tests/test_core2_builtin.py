"""Core's builtin manifest (rook/core2/builtin/core.yaml) validates and agrees
with the spec's presets and the kit's contract registry."""

import pytest

pytest.importorskip("jsonschema")

from rook.core2.builtin import CORE
from rook.core2.rights import PRESETS
from rook.kit.contracts import Contracts
from rook.kit.ctx import Caller, KitError
from rook.kit.manifest import load
from rook.kit.manifest.validate import CORE_PRESETS, contract_problems

SERVICES = {"notify.send", "notify.channel.send", "notify.channel.status", "llm.complete", "agent.backend",
            "voice.tts", "voice.tts.voices", "voice.stt", "voice.turn", "voice.speaker"}
DEVICES = {"hid.click", "hid.key", "hid.drag", "hid.type", "app.launch", "notification.post",
           "power.off", "screen.capture", "screen.on", "sound.set", "media.play", "media.pause",
           "media.stop", "media.next", "media.previous", "media.seek", "ui.screen",
           # the rest of castling-caps C1, first provided by the phone plugins
           "ui.click", "ui.scroll", "ui.type", "ui.wait", "hid.status", "screen.keep_on", "screen.status",
           "battery.status", "app.list", "app.open_url", "app.share", "app.settings", "clipboard.get",
           "clipboard.set", "sound.status", "sound.dnd", "media.status", "notification.list",
           "notification.get", "notification.update", "notification.dismiss", "notification.act",
           "voice.speak", "voice.ask", "voice.replies", "voice.stop", "voice.voices"}


@pytest.fixture(scope="module")
def core():
    return load(CORE)


def test_validates_clean(core):
    m = core.data
    assert (m["plugin"], m["package"], m["builtin"]) == ("core", "core", True)
    # TODO(castling:manifest-v11): drop once `route` is in the contract schema.
    other = [str(w) for w in core.warnings
             if not (w.code == "unknown-key" and w.path.startswith("/contracts/")
                     and w.path.endswith("/route"))]
    assert other == []


def test_contracts_and_routing(core):
    contracts = core.data["contracts"]
    assert set(contracts) == SERVICES | DEVICES
    for name, c in contracts.items():
        assert c["route"] == ("provider" if name in SERVICES else "target"), name
        assert c.get("doc"), name


def test_a_cap_shaped_like_the_contract_fits_it(core):
    for name, c in core.data["contracts"].items():
        cap = {"args": c.get("args", {}), **({"result": c["result"]} if "result" in c else {})}
        assert contract_problems(c, cap) == [], name


def test_presets_match_the_spec(core):
    assert core.data["presets"] == PRESETS
    for name, p in core.data["presets"].items():
        assert CORE_PRESETS[name]["max_risk"] == p.get("max_risk", "admin")
        assert CORE_PRESETS[name]["effects"] == p.get("effects")


def test_events(core):
    emits = core.data["emits"]
    assert {"worker.online", "worker.offline", "plugin.installed", "hold.created",
            "session.started", "gate.blocked"} <= set(emits)
    for name, ev in emits.items():
        assert ev.get("doc") and ev["payload"], name
        assert not ev["taints"], name


@pytest.mark.asyncio
async def test_kit_registry_routes_core_contracts(core):
    reg = Contracts()
    reg.load(core.data)
    reg.load({"plugin": "telegram", "provides": {"notify.send": "telegram.send"},
              "caps": {"send": {"args": {"text": {"type": "str", "required": True},
                                         "to": {"type": "str"}, "title": {"type": "str"},
                                         "silent": {"type": "bool"}}}}})
    with pytest.raises(KitError, match="no arg title"):
        reg.load({"plugin": "discord", "provides": {"notify.send": "discord.post"},
                  "caps": {"post": {"args": {"text": {"type": "str", "required": True},
                                             "to": {"type": "str"}}}}})
    calls = []
    await reg.route("notify.send", {"text": "hi"}, Caller(person="bake"),
                    lambda cap, args, who: calls.append(cap))
    assert calls == ["telegram.send"]
    # A device contract goes to the provider on the named target, not a chosen one.
    reg.load({"plugin": "kvm", "provides": {"hid.type": "kvm.hid.type"},
              "caps": {"hid.type": {"args": {"text": {"type": "str", "required": True},
                                             "enter": {"type": "bool"},
                                             "slow": {"type": "bool"}}}}})
    assert reg.providers("hid.type") == ["kvm.hid.type"]
    with pytest.raises(KitError, match="routes by target"):
        reg.choose("hid.type")
