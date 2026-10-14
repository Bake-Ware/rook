"""Pairing codes carry a plugin profile: minted with the band's defaults,
handed over at enrollment, applied by the worker on its first start."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rook.hub import plugin_profiles as pp
from rook.hub.settings_store import SettingsStore

ROOT = Path(__file__).resolve().parents[1]


# -- pairing codes carry a plugin profile ------------------------------------------------

@pytest.mark.asyncio
async def test_pairing_profile_mint_enroll_and_first_start(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from rook.remote import setup_store
    from rook.remote.bootstrap import CombinedServer
    from rook.remote.enrollment import EnrollmentStore
    from rook.worker import admin as wadmin, enroll as wenroll
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ROOK_ENROLLMENT_FILE", str(tmp_path / "worker" / "enrollment.json"))
    monkeypatch.delenv("ROOK_SETTINGS_DB", raising=False)
    monkeypatch.delenv("ROOK_PUBLIC_BASE", raising=False)
    setup_store.save({"band_name": "home", "band_psk": "piano-haste-nugget-drift-mangle",
                      "hub_public": "127.0.0.1:8765", "pyz_domain": "http://127.0.0.1:7005"})
    EnrollmentStore().import_config()
    band_id = EnrollmentStore().bands()[0]["id"]
    # The operator chose plugins for new workers on this band (Manage > Plugins).
    pp.set_worker_defaults(SettingsStore(), band_id, {"hid": False, "shell": True,
                                                      "selfupdate": True}, actor="human:op")
    server = CombinedServer(web_user="owner", web_pass="password", port=7005,
                            domain="http://127.0.0.1:7005", hub_public="127.0.0.1:8765")
    async with TestClient(TestServer(server._app)) as client:
        origin = {"Origin": "http://127.0.0.1:7005"}
        await client.post("/login", data={"user": "owner", "pass": "password"})
        data = await (await client.get("/account/session", headers={"Accept": "application/json"})).json()
        band = next(b for b in data["bands"] if b["role"] == "owner" and b["active"])
        pair = await client.post("/account/action", data={"csrf": data["user"]["csrf"], "op": "pair",
                                                          "band_id": band["id"]},
                                 headers={"X-Rook-View": "account", **origin})
        assert pair.status == 200, await pair.text()
        grant = (await pair.json())["pairing"]
        assert grant["profile"] == {"enable": ["selfupdate", "shell"], "disable": ["hid"]}
        # The countdown refresh keeps it.
        again = await client.post("/account/pairing", json={"csrf": data["user"]["csrf"],
                                                            "band_id": band["id"],
                                                            "session": grant["session"]},
                                  headers=origin)
        assert (await again.json())["profile"] == grant["profile"]
        # A worker enrolls with the code (the installer's --enroll --pair-code).
        base = str(client.make_url("")).rstrip("/")
        await asyncio.to_thread(wenroll.enroll, base, grant["code"])
    saved = wenroll.load()
    assert saved["plugin_profile"] == grant["profile"]
    assert "plugin_profile" not in saved["device"]
    # First start: the profile becomes the persisted plugin state.
    monkeypatch.setattr(wadmin, "_WORKER_DIR", tmp_path / "worker")
    monkeypatch.setattr(wadmin, "_PLUGIN_STATE", tmp_path / "worker" / "plugins.json")
    applied = wadmin.apply_enrollment_profile()
    assert applied["disable"] == ["hid"] and "hid" in wadmin.load_disabled()
    assert wadmin.load_enabled() >= {"shell"}
    assert wadmin.apply_enrollment_profile() is None          # once
    # An operator's later switch wins over the profile.
    state = json.loads((tmp_path / "worker" / "plugins.json").read_text())
    state["disabled"] = []
    (tmp_path / "worker" / "plugins.json").write_text(json.dumps(state))
    assert wadmin.apply_enrollment_profile() is None and "hid" not in wadmin.load_disabled()


def test_worker_applies_the_profile_before_loading(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    from rook.worker import admin as wadmin, core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(wadmin, "_WORKER_DIR", tmp_path)
    monkeypatch.setattr(wadmin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(wadmin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    (tmp_path / "enrollment.json").write_text(json.dumps(
        {"plugin_profile": {"enable": [], "disable": ["shell", "selfupdate"]}}))
    monkeypatch.setenv("ROOK_ENROLLMENT_FILE", str(tmp_path / "enrollment.json"))
    w = core.Worker(NS(send=AsyncMock()), name="t")
    assert not w.registry.has("shell.exec")                  # off from the first start
    assert w.host.status["shell"]["state"] == "disabled"
    assert w.registry.has("worker.restart")                  # protected: never switched off


def test_old_worker_enrollment_ignores_the_profile():
    # The profile is an extra top-level key of the enroll reply; a worker
    # without this change keeps it inside its saved device record, unused.
    src = (ROOT / "rook/worker/enroll.py").read_text(encoding="utf-8")
    assert "issued.pop('plugin_profile',None)" in src
