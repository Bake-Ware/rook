"""Pairing codes carry a plugin profile: minted with the band's defaults,
handed over at enrollment, applied by the worker on its first start."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


# -- pairing codes carry a plugin profile ------------------------------------------------


def test_a_profile_never_widens_the_enable_allowlist(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    from rook.worker import admin as wadmin, core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(wadmin, "_WORKER_DIR", tmp_path)
    monkeypatch.setattr(wadmin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(wadmin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    (tmp_path / "enrollment.json").write_text(json.dumps(
        {"plugin_profile": {"enable": ["shell", "file_ops"], "disable": []}}))
    monkeypatch.setenv("ROOK_ENROLLMENT_FILE", str(tmp_path / "enrollment.json"))
    w = core.Worker(NS(send=AsyncMock()), enabled=["info"], name="t")
    assert w.registry.has("info.host") or any(c.startswith("info.") for c in w.registry.list())
    assert not w.registry.has("shell.exec") and not any(c.startswith("file.") for c in w.registry.list())


def test_worker_applies_the_profile_before_loading(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    from rook.worker import admin as wadmin, core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(wadmin, "_WORKER_DIR", tmp_path)
    monkeypatch.setattr(wadmin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(wadmin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    (tmp_path / "enrollment.json").write_text(json.dumps(
        {"plugin_profile": {"enable": [], "disable": ["shell", "selfupdate", "info"]}}))
    monkeypatch.setenv("ROOK_ENROLLMENT_FILE", str(tmp_path / "enrollment.json"))
    w = core.Worker(NS(send=AsyncMock()), name="t")
    assert not w.registry.has("shell.exec")                  # off from the first start
    assert w.host.status["shell"]["state"] == "disabled"
    assert w.registry.has("worker.restart")                  # protected: never switched off
    assert any(c.startswith("info.") for c in w.registry.list())


def test_old_worker_enrollment_ignores_the_profile():
    # The profile is an extra top-level key of the enroll reply; a worker
    # without this change keeps it inside its saved device record, unused.
    src = (ROOT / "rook/worker/enroll.py").read_text(encoding="utf-8")
    assert "issued.pop('plugin_profile',None)" in src
