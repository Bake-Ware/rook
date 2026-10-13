"""The tier floor never drops below master's.

``fixtures/master_tier_floor.json`` is master's (9f0d6df) hand-written
``BUILTIN`` table plus the effective tier master gave each Android native cap
(undeclared caps resolved to exec). It is frozen on purpose: when the floor
moved from one hand table to the plugins' own ``@capability(risk=...)``, a
tier could quietly go down. Lowering one now means editing the fixture in the
same change, which a reviewer sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rook.core import authz

HERE = Path(__file__).resolve().parent
SNAPSHOT = json.loads((HERE / "fixtures" / "master_tier_floor.json").read_text(encoding="utf-8"))


@pytest.fixture
def hub_floor(monkeypatch):
    """The floor as the hub builds it: no rook_android package to read."""
    monkeypatch.setattr(authz, "FLOOR_PACKAGES", ["rook.worker.plugins", "rook.hub.plugins"])
    monkeypatch.setattr(authz, "_floor", None)
    yield authz.builtin_table()
    authz._floor = None


def _at_least(cap: str, got: str, want: str) -> None:
    assert authz.tier_rank(got) >= authz.tier_rank(want), f"{cap}: {got} < master's {want}"


def test_snapshot_is_master_and_complete():
    assert len(SNAPSHOT["builtin"]) == 178
    assert {"shell.exec", "secret.get", "worker.update"} <= set(SNAPSHOT["builtin"])
    assert {"sms.list", "ui.text", "notify.post", "device.clipboard_get"} <= set(SNAPSHOT["android"])


@pytest.mark.parametrize("section", ["builtin", "android"])
def test_no_cap_is_below_masters_tier(hub_floor, section):
    for cap, want in SNAPSHOT[section].items():
        # What a peer announces cannot pull a cap under the floor.
        for announced in (None, "read"):
            _at_least(cap, authz.effective_tier(cap, announced), want["tier"])


def test_builtin_tags_are_kept(hub_floor):
    for cap, want in SNAPSHOT["builtin"].items():
        missing = set(want["tags"]) - set(authz.builtin_tags(cap))
        assert not missing, f"{cap}: lost tags {sorted(missing)}"


def _android_declared(monkeypatch):
    from test_plugin_contract import _android_stubs, _plugin_caps
    _android_stubs(monkeypatch)
    return {c: fn._rook_cap_meta for c, fn, _m in _plugin_caps(["rook_android.plugins"])}


def test_android_caps_declare_at_least_master(monkeypatch):
    """On the phone the app's own declarations are the floor: they too must
    not undercut master, and they keep the tags master had."""
    declared = _android_declared(monkeypatch)
    assert set(declared) == set(SNAPSHOT["android"]), "an Android cap was added or removed: " \
        "add it to the fixture (a new cap has no master tier) and to authz.CORE"
    for cap, want in SNAPSHOT["android"].items():
        _at_least(cap, declared[cap].risk, want["tier"])
        missing = set(want["tags"]) - set(declared[cap].tags)
        assert not missing, f"{cap}: lost tags {sorted(missing)}"


def test_hub_floor_matches_android_declarations(monkeypatch, hub_floor):
    """The hub cannot import rook_android, so authz.CORE carries the Android
    caps; it must not sit below what the app declares, or the hub would
    accept an Android peer announcing less than the app's own code says."""
    declared = _android_declared(monkeypatch)
    for cap, meta in declared.items():
        assert cap in hub_floor, f"{cap}: no hub-side floor (add it to authz.CORE)"
        _at_least(cap, hub_floor[cap][0], meta.risk)
        missing = set(meta.tags) - set(hub_floor[cap][1]) - {"personal"}
        assert not missing, f"{cap}: hub floor lacks tags {sorted(missing)}"
