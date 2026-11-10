"""A fresh hub builds its own worker bundle: signed with the hub's update key,
manifest URL on the hub's public base, operator-built bundles left alone."""

import hashlib
import importlib.util
import json
import os
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


LAN = "192.0.2.10"
ROOT = Path(__file__).resolve().parents[1]


def _bbw():
    spec = importlib.util.spec_from_file_location("bbw_auto", ROOT / "rook/remote/build_band_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pip_installed_telesthete_needs_no_any_rev(tmp_path, monkeypatch):
    bbw = _bbw()
    monkeypatch.delenv(bbw.ANY_REV_ENV, raising=False)
    # A pip install: a package dir with no git checkout around it.
    pkg = tmp_path / "site-packages" / "telesthete"
    (pkg / "protocol").mkdir(parents=True)
    for m in bbw.REQUIRED_TELESTHETE:
        (pkg / "protocol" / m).write_text("", encoding="utf-8")
    bbw.check_telesthete(pkg, pin="0" * 40)
    # site-packages inside some unrelated repository (a venv in a clone).
    if subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True).returncode == 0:
        bbw.check_telesthete(pkg, pin="0" * 40)
    # The telesthete this hub runs (pinned in pyproject.toml) is accepted as is.
    bbw.check_telesthete(bbw.TELESTHETE_ROOT)


