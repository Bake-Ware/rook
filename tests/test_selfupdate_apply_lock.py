"""One bundle install at a time (rook/worker/plugins/selfupdate.py).

worker.apply / worker.check / worker.update and an in-band push finishing
all download -> verify -> swap. Two at once used to share one temporary file
(band-worker.pyz.dl) and could interleave; a second swap after the first
would also overwrite .prev, the rollback copy, with the unverified build."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from rook.worker._build_info import BUILD
from rook.worker.plugins import selfupdate as su_mod
from rook.worker.plugins.selfupdate import SelfUpdatePlugin

NEW = b"the new bundle " * 200


@pytest.fixture
def su(tmp_path, monkeypatch):
    for name, fname in (("_PYZ", "band-worker.pyz"), ("_PREV", "band-worker.pyz.prev"),
                        ("_STATE", "update_state.json"), ("_RESULT", "update_result.json"),
                        ("_HOLD", "hold")):
        monkeypatch.setattr(su_mod, name, tmp_path / fname)
    monkeypatch.setattr(su_mod, "_WORKER_DIR", tmp_path)
    monkeypatch.delenv("ROOK_UPDATE_URL", raising=False)
    monkeypatch.setattr("rook.worker._update_verify.verify_manifest", lambda m, pk=None: True)
    p = SelfUpdatePlugin()
    p.restarts = []
    p._schedule_restart = lambda argv, delay=1.0: p.restarts.append(argv)
    p.fetches = []

    async def slow_get(url, timeout):
        p.fetches.append(url)
        await asyncio.sleep(0.05)          # long enough for a second call to arrive
        return NEW

    async def smoke_ok(path):
        await asyncio.sleep(0.01)
        return True
    p._http_get = slow_get
    p._smoke_test = smoke_ok
    (tmp_path / "band-worker.pyz").write_bytes(b"old build")
    p.dir = tmp_path
    return p


def manifest(build=BUILD + 1, sha=None):
    return {"build": build, "sha256": sha or hashlib.sha256(NEW).hexdigest(),
            "url": "http://hub.example.com/band-worker.pyz"}


def leftovers(d):
    return sorted(p.name for p in d.iterdir() if p.name.endswith((".dl", ".inband")))


@pytest.mark.asyncio
async def test_two_concurrent_applies_swap_once(su):
    a, b = await asyncio.gather(su._apply(manifest()), su._apply(manifest()))
    results = sorted([a, b], key=lambda r: bool(r.get("ok")))
    assert results[0] == {"ok": False, "error": "an update is already in progress"}
    assert results[1]["action"] == "updated" and results[1]["to_build"] == BUILD + 1
    assert len(su.fetches) == 1 and len(su.restarts) == 1
    # The rollback copy is the build that ran, not the new one.
    assert (su.dir / "band-worker.pyz.prev").read_bytes() == b"old build"
    assert (su.dir / "band-worker.pyz").read_bytes() == NEW
    assert leftovers(su.dir) == []


@pytest.mark.asyncio
async def test_no_second_install_once_one_swapped_in(su):
    assert (await su._apply(manifest()))["action"] == "updated"
    # The restart is pending: a later push must not swap again (it would
    # overwrite .prev with the unverified build).
    res = await su._apply(manifest(BUILD + 2))
    assert not res["ok"] and "already installed" in res["error"]
    res = await su._update(manifest=manifest(BUILD + 2))
    assert not res["ok"] and "already installed" in res["error"]
    assert (su.dir / "band-worker.pyz.prev").read_bytes() == b"old build"
    assert len(su.fetches) == 1


@pytest.mark.asyncio
async def test_update_and_apply_do_not_interleave(su):
    su._persist = lambda argv: False
    up, ap = await asyncio.gather(su._update(manifest=manifest()), su._apply(manifest()))
    assert [r.get("ok") for r in (up, ap)].count(True) == 1
    refused = up if not up.get("ok") else ap
    assert refused["error"] == "an update is already in progress"
    assert len(su.fetches) == 1
    assert (su.dir / "band-worker.pyz.prev").read_bytes() == b"old build"
    assert leftovers(su.dir) == []


@pytest.mark.asyncio
async def test_failed_installs_leave_no_temp_file(su):
    async def offline(url, timeout):
        raise OSError("offline")
    good_get = su._http_get
    su._http_get = offline
    assert "download failed" in (await su._apply(manifest()))["error"]
    assert "download failed" in (await su._update(manifest=manifest()))["error"]
    assert leftovers(su.dir) == []
    su._http_get = good_get
    assert "sha256 mismatch" in (await su._apply(manifest(sha="0" * 64)))["error"]
    assert "sha256 mismatch" in (await su._update(manifest=manifest(sha="0" * 64)))["error"]
    assert leftovers(su.dir) == []

    async def smoke_bad(path):
        return False
    su._smoke_test = smoke_bad
    assert "selftest" in (await su._apply(manifest()))["error"]
    assert "selftest" in (await su._update(manifest=manifest()))["error"]
    assert leftovers(su.dir) == []
    # Failures release the claim: the next good push goes through.
    su._smoke_test = lambda path: _true()
    assert (await su._apply(manifest()))["action"] == "updated"
    assert leftovers(su.dir) == []


async def _true():
    return True


def test_staged_bundles_get_their_own_file(su):
    a, b = su._stage(b"one"), su._stage(b"two")
    assert a != b and a.parent == b.parent == su.dir
    assert a.name.startswith("band-worker.") and a.name.endswith(".pyz.dl")
    assert a.read_bytes() == b"one" and b.read_bytes() == b"two"


@pytest.mark.asyncio
async def test_an_in_band_push_finishing_during_an_apply_is_discarded(su):
    su._ota_manifest = manifest()
    task = asyncio.create_task(su._apply(manifest()))
    await asyncio.sleep(0.02)                       # the apply is downloading
    await su._finish_inband(NEW, True)
    assert (await task)["action"] == "updated"
    assert len(su.restarts) == 1
    assert (su.dir / "band-worker.pyz.prev").read_bytes() == b"old build"
    assert leftovers(su.dir) == []
