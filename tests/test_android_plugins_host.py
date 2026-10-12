"""The Android app's native plugins through the worker's plugin host, with the
Chaquopy-only modules stubbed (no device or Android SDK needed)."""

from __future__ import annotations

import sys

from tests.test_plugin_contract import ANDROID_PY, _android_stubs


# -- Android plugins go through the host ------------------------------------

def test_android_plugins_load_through_the_host_with_placement(monkeypatch):
    """worker_entry used to register native caps by hand (no status, no
    settings, a ValueError on a collision crashed the runtime loop) and kept a
    hard-coded override list; now placement picks one of each pair."""
    from rook.core.facts import NodeFacts
    from rook.core.host import PluginHost
    _android_stubs(monkeypatch)
    packages = ["rook.worker.plugins", "rook_android.plugins"]
    phone = PluginHost(facts=NodeFacts(node_id="p", hw={"os": "android", "app": "android"}))
    phone.load(PluginHost.discover(packages, entry_points=False),
               enabled=["screen", "hid", "hid_a11y", "battery", "battery_android",
                        "screenshot", "selfupdate", "ui_android", "info"])
    mods = {p._module for p in phone.plugins}
    assert {"screen", "hid_a11y", "ui_android", "info"} <= mods
    assert not mods & {"hid", "screenshot", "selfupdate"}
    for stock in ("hid", "screenshot", "selfupdate", "battery"):
        assert phone.status[stock]["state"] == "not_placed", stock
    assert phone.registry.meta("ui.text").risk == "read"

    # A Termux or desktop worker never loads the native ones.
    desk = PluginHost(facts=NodeFacts(node_id="d", hw={"os": "android"}))
    desk.load(PluginHost.discover(packages, entry_points=False),
              enabled=["screen", "hid_a11y", "ui_android", "info"])
    assert {p._module for p in desk.plugins} == {"info"}
    assert desk.status["ui_android"]["state"] == "not_placed"


def test_android_cap_collision_is_isolated_not_fatal(monkeypatch):
    from rook.core.facts import NodeFacts
    from rook.core.host import PluginHost
    _android_stubs(monkeypatch)
    host = PluginHost(facts=NodeFacts(node_id="p", hw={"app": "android"}))
    host.registry.register("ui.text", lambda: "taken")
    host.load(PluginHost.discover("rook_android.plugins", entry_points=False),
              enabled=["ui_android", "screen"])
    assert host.status["ui_android"]["state"] == "failed"
    assert host.status["screen"]["state"] == "loaded"


def test_android_worker_entry_builds_a_host_backed_worker(monkeypatch, tmp_path):
    import importlib.util
    from types import SimpleNamespace
    from rook.core import authz
    from rook.worker import admin, core
    _android_stubs(monkeypatch)
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    # worker_kwargs() adds the APK's package to the tier floor; undo it after.
    monkeypatch.setattr(authz, "FLOOR_PACKAGES", list(authz.FLOOR_PACKAGES))
    monkeypatch.setattr(authz, "_floor", authz._floor)
    spec = importlib.util.spec_from_file_location("worker_entry_t", ANDROID_PY / "worker_entry.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    w = core.Worker(SimpleNamespace(send=None), name="phone", **entry.worker_kwargs())
    assert w.facts.hw["app"] == "android"
    assert "ui_android" in {p._module for p in w.plugins}
    assert not {"hid", "screenshot", "selfupdate", "battery"} & {p._module for p in w.plugins}
    # The native plugins' declared tiers are part of this node's floor.
    assert authz.builtin_tier("sms.send") == "exec"
    listed = {r["module"]: r for r in w.admin.plugin_list()["plugins"]}
    assert listed["ui_android"]["loaded"] and listed["hid"]["state"] == "not_placed"
