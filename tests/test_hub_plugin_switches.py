"""Hub plugin switches persist (core.plugins.hub) and stay coherent with a
plugin's own ``enabled`` setting; presets choose exactly a set of groups."""

from __future__ import annotations

import pytest

from tests.plugins_fakes import band_env, make_node
from rook.hub import plugin_profiles as pp


# -- hub persistence -------------------------------------------------------------

@pytest.mark.asyncio
async def test_hub_plugin_switches_survive_a_restart(tmp_path, monkeypatch):
    band_env(tmp_path, monkeypatch)
    node = make_node(tmp_path)
    await node.start()
    assert node.host.loaded("notify") is not None and node.host.loaded("knowledge") is None
    off = await node.set_plugin_enabled("notify", False, "human:op")
    assert off["ok"] and off["persisted"] and not node.has("notify.send")
    on = await node.set_plugin_enabled("knowledge", True, "human:op")
    assert on["ok"] and node.host.loaded("knowledge") is not None
    store = node.settings_store
    assert store.get(pp.HUB_KEY, "hub", "")["value"] == {"notify": False, "knowledge": True}
    # Its own `enabled` switch went on with it, attributed.
    assert store.get("knowledge.enabled", "hub", "")["value"] is True
    assert store.history(key="knowledge.enabled")[0]["actor"] == "human:op"
    refused = await node.set_plugin_enabled("settings", False, "human:op")
    assert not refused["ok"] and node.host.loaded("settings") is not None
    await node.stop()

    again = make_node(tmp_path)          # a restart: a new node over the same store
    await again.start()
    assert again.host.loaded("notify") is None
    assert again.host.status["notify"]["state"] == "disabled"
    assert again.host.loaded("knowledge") is not None
    back = await again.set_plugin_enabled("notify", True, "human:op")
    assert back["ok"] and again.has("notify.send")
    await again.stop()


@pytest.mark.asyncio
async def test_hub_plugin_caps_persist_and_refuse_without_an_owner(tmp_path, monkeypatch):
    from rook.hub.authz import current_principal, principal_for_user
    band_env(tmp_path, monkeypatch)
    node = make_node(tmp_path)
    await node.start()
    denied = await node.dispatch("hub.plugin.disable", {"module": "notify"})
    assert not denied["ok"] and "denied" in denied["error"]
    tok = current_principal.set(principal_for_user(None, True))
    try:
        r = await node.dispatch("hub.plugin.disable", {"module": "notify"}, identity="human:owner")
    finally:
        current_principal.reset(tok)
    assert r["ok"] and r["result"]["persisted"]
    assert pp.hub_disabled(node.settings_store) == {"notify"}
    await node.stop()


@pytest.mark.asyncio
async def test_env_lock_on_an_enabled_setting_wins(tmp_path, monkeypatch):
    band_env(tmp_path, monkeypatch)
    monkeypatch.setenv("ROOK_KNOWLEDGE", "0")
    node = make_node(tmp_path)
    await node.start()
    r = await node.set_plugin_enabled("knowledge", True, "human:op")
    assert not r["ok"] and "ROOK_KNOWLEDGE" in r["error"]
    assert node.host.loaded("knowledge") is None
    await node.stop()


@pytest.mark.asyncio
async def test_stored_switches_from_another_process_are_synced(tmp_path, monkeypatch):
    enroll, band = band_env(tmp_path, monkeypatch)
    node = make_node(tmp_path)
    await node.start()
    # What the dashboard's /setup wizard writes, without the hub node at hand.
    defaults = {p["id"] for p in pp.PRESETS if p["default"]}
    pp.apply_presets_to_store(node.settings_store, (defaults - {"home"}) | {"knowledge"},
                              band["id"], actor="human:setup")
    assert node.host.loaded("knowledge") is None
    done = await node.sync_plugin_state()
    assert {d["module"] for d in done} >= {"knowledge"} and all(d["ok"] for d in done)
    assert node.host.loaded("knowledge") is not None
    assert node.host.loaded("home") is None            # its box was unticked
    assert await node.sync_plugin_state() == []      # nothing new
    await node.stop()


def test_the_default_selection_changes_nothing():
    defaults = [p["id"] for p in pp.PRESETS if p["default"]]
    assert pp.preset_changes(defaults) == ({}, {})            # nothing stored yet
    assert pp.preset_changes(defaults, {pid: None for pid in pp.PRESET_IDS}) == ({}, {})
    # Stored choices are what the boxes show; saving them as shown is a no-op.
    states = {"remote": True, "agents": False, "knowledge": True, "home": None,
              "alerts": None, "hardware": False}
    shown = pp.preset_selected(states)
    assert pp.preset_changes([k for k, v in shown.items() if v], states) == ({}, {})


def test_preset_changes_only_write_the_boxes_that_changed():
    defaults = {p["id"] for p in pp.PRESETS if p["default"]}
    # Untick Hardware, tick Knowledge; leave the rest as shown.
    hub, worker = pp.preset_changes((defaults - {"hardware"}) | {"knowledge"})
    assert worker == {"hid": False, "hid_a11y": False, "cec": False, "pikvm": False,
                      "dongle": False, "camera": False, "screenshot": False, "screen": False,
                      "memory": True}
    assert hub == {"knowledge": True, "tasks": True}
    # Unticking Knowledge & tasks never turns tasks off (KEEP_ON), nor info / notify.
    hub, worker = pp.preset_changes(defaults - {"remote", "alerts"},
                                    {"knowledge": True})
    assert hub == {"knowledge": False, "telegram": False, "discord": False}
    assert "tasks" not in hub and "notify" not in hub and "info" not in worker
    assert worker["shell"] is False and "persona" not in worker and "speak_android" not in worker
    assert not (set(worker) & pp.PROTECTED_WORKER)
    with pytest.raises(ValueError):
        pp.preset_changes(["nope"])


def test_preset_states_come_from_stored_choices():
    st = pp.preset_states({"knowledge": True}, {"shell": True, "file_ops": True, "proc": True,
                                               "hid": False, "sessions": True, "work": False})
    assert st["remote"] is True and st["knowledge"] is True
    assert st["agents"] is None             # mixed: switched one by one
    assert st["hardware"] is False and st["home"] is None
    assert pp.preset_states({}, {}, {"knowledge": True})["knowledge"] is True
    prof = pp.profile_from_defaults({"shell": True, "hid": False, "selfupdate": False, "x": 1})
    assert prof == {"enable": ["shell"], "disable": ["hid"]}
    assert pp.normalize_profile({"enable": ["a;rm"], "disable": []}) is None


def test_setup_wizard_default_ticks_store_nothing(tmp_path, monkeypatch):
    from rook.hub.settings_store import SettingsStore
    enroll, band = band_env(tmp_path, monkeypatch)
    store = SettingsStore(tmp_path / "s.db")
    defaults = [p["id"] for p in pp.PRESETS if p["default"]]
    r = pp.apply_presets_to_store(store, defaults, band["id"], actor="human:setup")
    assert r["hub"] == {} and r["worker_defaults"] == {} and store.history() == []
    # A hub whose knowledge was already turned on in Settings shows it ticked:
    # keeping the box ticked changes nothing either.
    store.set("knowledge.enabled", "hub", "", value=True, actor="human:op")
    r = pp.apply_presets_to_store(store, defaults + ["knowledge"], band["id"], actor="human:setup")
    assert r["hub"] == {} and r["worker_defaults"] == {}
