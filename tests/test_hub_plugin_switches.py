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
    pp.apply_presets_to_store(node.settings_store, ["knowledge", "remote"], band["id"],
                              actor="human:setup")
    assert node.host.loaded("knowledge") is None
    done = await node.sync_plugin_state()
    assert {d["module"] for d in done} >= {"knowledge"} and all(d["ok"] for d in done)
    assert node.host.loaded("knowledge") is not None
    assert node.host.loaded("home") is None            # its preset was left out
    assert await node.sync_plugin_state() == []      # nothing new
    await node.stop()


def test_preset_changes_choose_exactly_the_selection():
    hub, worker = pp.preset_changes(["remote"])
    assert worker["shell"] and worker["file_ops"] and worker["proc"]
    assert worker["hid"] is False and worker["sessions"] is False and worker["memory"] is False
    assert "info" in worker and worker["info"] is True        # listed, kept on
    assert hub["knowledge"] is False and "notify" not in hub   # notify is never switched off
    assert not (set(worker) & pp.PROTECTED_WORKER)
    hub, worker = pp.preset_changes(pp.PRESET_IDS)
    assert all(worker.values()) and all(hub.values())
    with pytest.raises(ValueError):
        pp.preset_changes(["nope"])
    prof = pp.profile_from_defaults({"shell": True, "hid": False, "selfupdate": False, "x": 1})
    assert prof == {"enable": ["shell"], "disable": ["hid"]}
    assert pp.normalize_profile({"enable": ["a;rm"], "disable": []}) is None
