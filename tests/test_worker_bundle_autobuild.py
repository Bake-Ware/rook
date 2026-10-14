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

from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer
from rook.remote.enrollment import EnrollmentStore
from rook.remote.worker_bundle import WorkerBundle, newest_source_mtime

LAN = "192.0.2.10"
ROOT = Path(__file__).resolve().parents[1]


def _bbw():
    spec = importlib.util.spec_from_file_location("bbw_auto", ROOT / "rook/remote/build_band_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_lookup_prefers_the_operator_build(tmp_path):
    manual, auto = tmp_path / "manual", tmp_path / "auto"
    manual.mkdir()
    b = WorkerBundle(lambda: "http://hub.lan:7005", out_dir=auto, manual_dir=manual)
    assert b.path("band-worker.pyz") is None
    assert b.stale_reason() == "no worker bundle yet"
    auto.mkdir()
    (auto / "band-worker.pyz").write_bytes(b"auto")
    (auto / "band-worker.json").write_text(json.dumps({"url": "http://hub.lan:7005/band-worker.pyz"}))
    assert b.path("band-worker.pyz") == auto / "band-worker.pyz"
    # The enrollment download is the same worker unless an operator drops one.
    assert b.path("band-worker-enrollment.pyz") == auto / "band-worker.pyz"
    (manual / "band-worker.pyz").write_bytes(b"manual")
    assert b.path("band-worker.pyz") == manual / "band-worker.pyz"
    assert b.path("band-worker.json") is None      # never mixes the two builds
    assert b.stale_reason() == "" and b.ensure() is False
    (manual / "band-worker-enrollment.pyz").write_bytes(b"enroll")
    assert b.path("band-worker-enrollment.pyz") == manual / "band-worker-enrollment.pyz"


def test_auto_bundle_staleness(tmp_path, monkeypatch):
    monkeypatch.delenv("ROOK_PUBLIC_BASE", raising=False)
    auto = tmp_path / "auto"
    auto.mkdir()
    base = ["http://hub.lan:7005"]
    b = WorkerBundle(lambda: base[0], out_dir=auto, manual_dir=tmp_path / "none")
    (auto / "band-worker.pyz").write_bytes(b"x")
    (auto / "band-worker.json").write_text(json.dumps({"url": "http://hub.lan:7005/band-worker.pyz"}))
    assert b.stale_reason() == ""
    base[0] = "https://rook.example.com"
    assert b.stale_reason() == "public URL changed"
    base[0] = "http://hub.lan:7005"
    old = newest_source_mtime() - 60
    os.utime(auto / "band-worker.pyz", (old, old))
    assert b.stale_reason() == "worker code is newer than the bundle"


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


@pytest.fixture
def lan_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ROOK_PUBLIC_BASE", raising=False)
    setup_store.save({"band_name": "home", "band_psk": "piano-haste-nugget-drift-mangle",
                      "hub_public": f"{LAN}:8765", "pyz_domain": f"{LAN}:7005"})
    EnrollmentStore().import_config()
    server = CombinedServer(web_user="owner", web_pass="password", port=7005,
                            domain=f"{LAN}:7005", hub_public=f"{LAN}:8765")
    server._bundle.manual_dir = tmp_path / "no-operator-build"
    assert server._bundle.out_dir == tmp_path / "data" / "worker-bundle"
    return server


@pytest.mark.asyncio
async def test_fresh_hub_builds_a_signed_bundle_in_the_background(lan_hub):
    from rook.remote.update_keys import load_signing_key, public_key_b64
    from rook.worker._update_verify import verify_manifest
    server = lan_hub
    async with TestClient(TestServer(server._app)) as client:
        first = await client.get("/band-worker.pyz")
        assert first.status == 503 and first.headers["Retry-After"] == "60"
        assert "retry in a minute" in await first.text()
        assert (await client.get("/band-worker.json")).status == 503
        deadline = time.monotonic() + 300
        while server._bundle.building and time.monotonic() < deadline:
            server._bundle.wait(1)
        assert not server._bundle.error, server._bundle.error
        manifest = await (await client.get("/band-worker.json")).json()
        pyz = await (await client.get("/band-worker.pyz")).read()
        enrollment_pyz = await (await client.get("/band-worker-enrollment.pyz")).read()
    pub = public_key_b64(load_signing_key())
    assert manifest["sig"] and verify_manifest(manifest, pub)
    assert manifest["url"] == f"http://{LAN}:7005/band-worker.pyz"
    assert manifest["sha256"] == hashlib.sha256(pyz).hexdigest() and pyz == enrollment_pyz
    bundle = server._bundle.out_dir / "band-worker.pyz"
    with zipfile.ZipFile(bundle) as z:
        assert pub in z.read("rook/worker/_update_pubkey.py").decode()
        assert "VERSION =" in z.read("rook/worker/_build_info.py").decode()
    assert server._bundle.stale_reason() == ""
