"""The hub update service (rook/hub/plugins/updates) against a fake GitHub.

No test reaches the real GitHub: tests/updates_fakes.py serves releases.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import tarfile

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from rook.hub import release_meta, update_feed
from rook.hub.plugins.updates import hubself
from rook.hub.plugins.updates import service as svcmod
from rook.hub.plugins.updates.github import ReleaseError, ReleaseFeed, parse_sums
from rook.hub.plugins.updates.rollout import RolloutEngine
from rook.hub.plugins.updates.service import Refused, UpdateService, in_window
from tests.updates_fakes import FakeBand, FakeGitHub, release_json, sdist

REPO = "Bake-Ware/rook"
HUB = {"version": "0.2.0", "build": 500, "name": "500.lumpy.moose", "relay": "v0.3.0",
       "release": "", "python": "python3"}


@pytest.fixture
def keyenv(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_UPDATE_KEY", str(tmp_path / "update-key"))
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ROOK_UPDATES_DIR", raising=False)
    from rook.remote.update_keys import ensure_key, public_key_b64
    return public_key_b64(ensure_key())


def make_bundle(dirpath, build: int, url="http://hub.example.com/band-worker.pyz"):
    from rook.remote.update_keys import sign_manifest
    dirpath.mkdir(parents=True, exist_ok=True)
    pyz = dirpath / "band-worker.pyz"
    pyz.write_bytes(f"bundle {build}".encode() * 50)
    m = sign_manifest({"schema": 1, "build": build, "version": f"{build}.lumpy.moose",
                       "commit": "abc1234", "built_at": "x", "filename": "band-worker.pyz",
                       "sha256": hashlib.sha256(pyz.read_bytes()).hexdigest(),
                       "size": pyz.stat().st_size, "url": url})
    (dirpath / "band-worker.json").write_text(json.dumps(m), encoding="utf-8")
    return dirpath / "band-worker.json", pyz


@pytest_asyncio.fixture
async def gh():
    fake = FakeGitHub()
    server = TestServer(fake.app)
    await server.start_server()
    fake.base = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
def band():
    b = FakeBand()
    b.delay = 0.02
    return b


@pytest.fixture
def make_svc(tmp_path, keyenv, gh, band, monkeypatch):
    monkeypatch.setattr(hubself, "current_hub", lambda: dict(HUB))
    monkeypatch.setattr(RolloutEngine, "POLL", 0.02)
    launched = []
    monkeypatch.setattr(hubself, "launch", lambda argv, **kw: launched.append((argv, kw)) or
                        {"how": "test"})
    settings = {"mode": "notify", "channel": "stable", "window": "", "api_base": gh.base,
                "repo": REPO, "relay_repo": "Bake-Ware/telesthete", "gate_secs": 2,
                "settle_secs": 0.05, "batch": 2, "check_hours": 6, "notify_chat": True}
    bundles = {"dir": tmp_path / "bundle"}
    notes = []

    async def notify(text):
        notes.append(text)

    def build(**over):
        settings.update(over)
        s = UpdateService(tmp_path / "updates", settings=lambda: settings, client=lambda: band,
                          notify=notify, bundle_paths=lambda: (
                              (bundles["dir"] / "band-worker.json", bundles["dir"] / "band-worker.pyz")
                              if (bundles["dir"] / "band-worker.json").exists() else None))
        s.launched, s.notes, s.settings_dict, s.bundles = launched, notes, settings, bundles
        return s
    return build


def std_release(gh, version="0.3.0", build=530, **kw):
    assets = {"release.json": release_json(version, build, **kw),
              f"rook-{version}.tar.gz": sdist(version), "install-hub.sh": b"#!/bin/bash\necho hi\n"}
    return gh.add_release(REPO, "v" + version, assets, body=f"## Rook {version}\n- things")


# -- release metadata ----------------------------------------------------------

def test_version_compare():
    lt = release_meta.version_lt
    assert lt("0.4.1", "0.4.15") and lt("0.3.0rc1", "0.3.0") and not lt("0.4", "0.4.0")
    assert not lt("garbage", "0.1") and lt("v0.9", "v0.10")


def test_parse_release_json_drops_junk():
    meta = release_meta.parse_release_json({**json.loads(release_json("0.3.0", 530)),
                                            "evil": "x", "breaking_reasons": ["a"] * 50})
    assert "evil" not in meta and len(meta["breaking_reasons"]) == 10
    with pytest.raises(ValueError):
        release_meta.parse_release_json({"typ": "something"})


def test_member_classification_and_floor_notes():
    k = update_feed.member_kind
    assert k({"worker_id": "a", "build": 401})["kind"] == "worker"
    assert k({"worker_id": "e", "build": None, "plugins": ["info", "kvm"], "caps": ["kvm.type"],
              "version": "0.6.5"})["kind"] == "firmware"
    app = k({"worker_id": "p", "build": 142, "app_release": {"platform": "android", "version": "0.4.1"}})
    assert app["kind"] == "app" and app["app_version"] == "0.4.1"
    floors = {"min_worker_build": 364, "min_android_app": "0.4.2", "min_hub_build": 230}
    assert "0.4.1 is older than 0.4.2" in release_meta.below_floor(app, floors)
    assert release_meta.below_floor(k({"worker_id": "a", "build": 401}), floors) == ""
    row = {"worker_id": "p", "build": 142, "app_release": {"platform": "android", "version": "0.4.1"}}
    assert update_feed.worker_note(row) == ""          # fine for this hub (compat.py)
    note = update_feed.worker_note(row, pending={**floors, "version": "0.3.0"})
    assert "Will stop working after update 0.3.0" in note


def test_maintenance_window():
    at = lambda h, m: datetime.datetime(2026, 10, 14, h, m)
    assert in_window("", at(12, 0))
    assert in_window("02:00-05:00", at(3, 0)) and not in_window("02:00-05:00", at(5, 0))
    assert in_window("23:00-01:00", at(0, 30)) and not in_window("23:00-01:00", at(12, 0))
    assert not in_window("nonsense", at(3, 0))


# -- GitHub polling ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_uses_etags_filters_drafts_and_prereleases(gh, tmp_path):
    std_release(gh)
    gh.add_release(REPO, "v0.4.0rc1", {"release.json": release_json("0.4.0rc1", 540)}, prerelease=True)
    gh.add_release(REPO, "v0.5.0", {}, draft=True)
    feed = ReleaseFeed(REPO, tmp_path, api=gh.base)
    assert (await feed.poll(3600, force=True))["changed"]
    assert [r["tag"] for r in feed.releases()] == ["v0.3.0"]
    assert [r["tag"] for r in feed.releases(prerelease=True)] == ["v0.4.0rc1", "v0.3.0"]
    await feed.poll(3600, force=True)
    assert gh.requests[-1][1] and gh.requests[0][1] == ""   # second poll sent If-None-Match
    assert (await feed.poll(3600))["skipped"]               # not due yet
    # A fresh object reads the cached list back (restart).
    assert [r["tag"] for r in ReleaseFeed(REPO, tmp_path, api=gh.base).releases()] == ["v0.3.0"]


@pytest.mark.asyncio
async def test_poll_backs_off_and_honours_rate_limits(gh, tmp_path):
    feed = ReleaseFeed(REPO, tmp_path, api=gh.base)
    gh.fail = (500, {})
    await feed.poll(3600, force=True)
    first = feed.state["next_at"] - feed.state["checked_at"]
    await feed.poll(3600, force=True)
    second = feed.state["next_at"] - feed.state["checked_at"]
    assert feed.state["failures"] == 2 and second == pytest.approx(first * 2, rel=0.01)
    gh.fail = (403, {"Retry-After": "7200"})
    await feed.poll(3600, force=True)
    assert feed.state["next_at"] - feed.state["checked_at"] == pytest.approx(7200, abs=2)
    assert "403" in feed.state["error"]
    gh.fail = None
    std_release(gh)
    await feed.poll(3600, force=True)
    assert feed.state["failures"] == 0 and feed.state["error"] == ""


@pytest.mark.asyncio
async def test_downloads_are_checked_against_sha256sums(gh, tmp_path):
    std_release(gh)
    gh.add_release(REPO, "v0.3.1", {"release.json": release_json("0.3.1", 531),
                                    "install-hub.sh": b"good"},
                   tamper={"install-hub.sh": b"evil"})
    gh.add_release(REPO, "v0.3.2", {"install-hub.sh": b"x"}, sums=False)
    feed = ReleaseFeed(REPO, tmp_path, api=gh.base)
    await feed.poll(3600, force=True)
    ok = await feed.fetch(feed.find("v0.3.0"), "install-hub.sh")
    assert ok.read_bytes().startswith(b"#!/bin/bash")
    with pytest.raises(ReleaseError, match="does not match SHA256SUMS"):
        await feed.fetch(feed.find("v0.3.1"), "install-hub.sh")
    assert not (feed.root / "v0.3.1" / "install-hub.sh").exists()
    with pytest.raises(ReleaseError, match="no SHA256SUMS"):
        await feed.fetch(feed.find("v0.3.2"), "install-hub.sh")
    meta = await feed.meta(feed.find("v0.3.0"))
    assert meta["build"] == 530 and feed.find("v0.3.0")["meta"] == meta
    assert parse_sums("ab" * 32 + " *./x.whl\n")["x.whl"] == "ab" * 32


# -- the worker feed ---------------------------------------------------------------

def test_feed_snapshots_resign_with_their_own_url(tmp_path, keyenv):
    from rook.worker._update_verify import verify_manifest
    man, pyz = make_bundle(tmp_path / "b", 510)
    feed = update_feed.WorkerFeed(tmp_path / "updates")
    assert feed.snapshot(man, pyz) == 510
    m = feed.manifest_for(510)
    assert m["url"] == "http://hub.example.com/updates/worker/510/band-worker.pyz"
    assert verify_manifest(m, keyenv) and m["sha256"] == json.loads(man.read_text())["sha256"]
    assert feed.released_manifest() is None       # nothing released yet: serve as before
    feed.release(510, by="test")
    assert feed.released_manifest()["build"] == 510
    for b in range(511, 520):
        man, pyz = make_bundle(tmp_path / f"b{b}", b)
        feed.snapshot(man, pyz)
    feed.prune(keep=3)
    assert feed.builds() == [510, 517, 518, 519]      # the released build is kept
    assert feed.pyz_for("../../etc") is None


# -- breaking updates and the hub ----------------------------------------------------------

@pytest.mark.asyncio
async def test_breaking_release_names_who_is_cut_off_and_needs_ack(make_svc, gh, band, monkeypatch):
    std_release(gh, min_worker_build=505, min_android_app="0.4.2", breaking=True,
                reasons=["workers older than build 505 are no longer supported (was 142)"])
    band.add("w1", "garage", 501)
    band.add("w2", "office", 510)
    band.add("p1", "autumns-phone", 142, app={"platform": "android", "version": "0.4.1"},
             caps=("device.update",))
    s = make_svc()
    make_bundle(s.bundles["dir"], 510)
    s.sync_feed()
    await s.check(force=True)
    rel = s.rook.find("v0.3.0")
    a = s.analyse(rel)
    assert a["breaking"]
    cut = {c["name"]: c for c in a["cut_off"]}
    assert set(cut) == {"garage", "autumns-phone"}
    assert cut["garage"]["fix"] == "update_first" and "build 510" in cut["garage"]["how"]
    assert cut["autumns-phone"]["fix"] == "app" and "does not update itself" in cut["autumns-phone"]["how"]
    monkeypatch.setattr(hubself, "detect", lambda repo="": {"kind": "installer", "can_apply": True,
                                                            "mode": "user", "systemd": True})
    with pytest.raises(Refused) as e:
        await s.apply_hub("v0.3.0", "human:owner")
    assert e.value.data["needs_ack"] and len(e.value.data["cut_off"]) == 2
    assert not s.launched
    # The announcement went out once, and says it is breaking.
    assert len(s.notes) == 1 and "BREAKING" in s.notes[0]
    # The summary tells the Workers page who will be cut off.
    assert update_feed.summary(s.root)["pending_floors"]["min_worker_build"] == 505
    s.acknowledge("v0.3.0", "human:owner")
    op = await s.apply_hub("v0.3.0", "human:owner")
    argv = s.launched[0][0]
    assert argv[:2] == ["bash", str(s.rook.root / "v0.3.0" / "install-hub.sh")]
    assert argv[argv.index("--version") + 1] == "v0.3.0" and "--user" in argv
    assert op["target_build"] == 530 and s.state["hub_apply"]
    with pytest.raises(Refused, match="already in progress"):
        await s.apply_hub("v0.3.0", "human:owner")
    # The hub restarts on the new release: the update is recorded as done.
    monkeypatch.setattr(hubself, "current_hub", lambda: {**HUB, "version": "0.3.0", "build": 530})
    s.check_hub_apply()
    assert s.state["hub_apply"] is None
    ev = s.history()[0]
    assert ev["kind"] == "hub" and ev["event"] == "finished" and ev["result"] == "ok"


@pytest.mark.asyncio
async def test_hub_without_self_update_explains_itself(make_svc, gh, monkeypatch):
    std_release(gh)
    s = make_svc()
    await s.check(force=True)
    monkeypatch.setattr(hubself, "detect", lambda repo="": {
        "kind": "installer", "can_apply": False, "why": "installed system-wide",
        "manual": "curl -fsSL https://x/{tag}/install-hub.sh | sudo bash -s -- --system --version {tag} --yes"})
    with pytest.raises(Refused) as e:
        await s.apply_hub("v0.3.0", "human:owner")
    assert "system-wide" in str(e.value) and "--version v0.3.0" in e.value.data["manual"]


@pytest.mark.asyncio
async def test_rook_hub_deploy_path_builds_a_signed_hub_release(make_svc, gh, keyenv, monkeypatch, tmp_path):
    from rook.hubdeploy import manifest as mf
    std_release(gh)
    s = make_svc()
    await s.check(force=True)
    monkeypatch.setattr(hubself, "detect", lambda repo="": {
        "kind": "rook-hub", "can_apply": True, "config": "/etc/rook/hub-deploy.json", "scope": "user"})
    await s.apply_hub("v0.3.0", "human:owner")
    argv, kw = s.launched[0]
    assert argv[-3:-1] == ["deploy", argv[-2]] and argv[-1] == "--yes"
    m = mf.verify(mf.load_manifest(argv[-2]), keyenv)
    assert m["version"] == "530.lumpy.moose" and m["github_tag"] == "v0.3.0"
    with tarfile.open(s.root / "hub" / m["filename"]) as tf:
        names = tf.getnames()
    assert "530.lumpy.moose/rook/__init__.py" in names and all(n.startswith("530.lumpy.moose") for n in names)
    assert kw["user_scope"] is True


@pytest.mark.asyncio
async def test_auto_mode_applies_only_non_breaking_releases_in_the_window(make_svc, gh, monkeypatch):
    std_release(gh, breaking=True, reasons=["new band key format"])
    s = make_svc(mode="auto", window="02:00-05:00")
    monkeypatch.setattr(hubself, "detect", lambda repo="": {"kind": "installer", "can_apply": True,
                                                            "mode": "user", "systemd": True})
    await s.check(force=True)
    assert await s.auto(datetime.datetime(2026, 10, 14, 12, 0)) == "outside the maintenance window"
    assert "breaking" in await s.auto(datetime.datetime(2026, 10, 14, 3, 0))
    assert not s.launched
    std_release(gh, "0.3.1", 531)
    await s.check(force=True)
    assert await s.auto(datetime.datetime(2026, 10, 14, 3, 0)) == "hub update started"
    assert "v0.3.1" in s.launched[0][0]


# -- staged worker rollouts --------------------------------------------------------------------

def _feed_with(s, *builds):
    for b in builds:
        make_bundle(s.bundles["dir"], b)
        s.sync_feed()


@pytest.mark.asyncio
async def test_rollout_canary_first_then_the_rest_then_released(make_svc, band):
    for i, name in enumerate(["alpha", "bravo", "charlie", "delta"]):
        band.add(f"w{i}", name, 500)
    band.add("p1", "phone", 142, app={"platform": "android", "version": "0.4.1"}, caps=())
    s = make_svc(canary="charlie")
    _feed_with(s, 500)
    assert s.feed.released_build() == 500             # first sight: nothing changes for workers
    _feed_with(s, 510)
    assert s.feed.released_build() == 500             # a new bundle waits for a rollout
    ro = await s.start_rollout({"mode": "all"}, "human:owner")
    assert ro["targets"][0]["name"] == "charlie" and ro["targets"][0]["phase"] == "canary"
    assert {t["name"] for t in ro["targets"]} == {"alpha", "bravo", "charlie", "delta"}
    await asyncio.wait_for(s._rollout_task, 10)
    assert ro["status"] == "done", ro
    applies = [t for c, t, _ in band.calls if c == "worker.apply"]
    assert applies[0] == "w2"                          # the canary went first
    assert all(w["build"] == 510 for wid, w in band.workers.items() if wid.startswith("w"))
    assert s.feed.released_build() == 510             # a rollout to all releases the build
    assert s.history()[0]["result"] == "done"


@pytest.mark.asyncio
async def test_failed_canary_stops_the_rollout_before_anyone_else(make_svc, band):
    for i, name in enumerate(["alpha", "bravo", "charlie"]):
        band.add(f"w{i}", name, 500)
    band.behaviour["w0"] = "rollback"
    s = make_svc()
    _feed_with(s, 500, 510)
    ro = await s.start_rollout({"mode": "all"}, "human:owner")
    await asyncio.wait_for(s._rollout_task, 10)
    assert ro["status"] == "stopped" and "alpha" in ro["reason"]
    assert "rolled itself back" in ro["targets"][0]["detail"]
    assert [t for c, t, _ in band.calls if c == "worker.apply"] == ["w0"]
    assert {t["status"] for t in ro["targets"][1:]} == {"cancelled"}
    assert s.feed.released_build() == 500


@pytest.mark.asyncio
async def test_gate_failures_held_workers_and_selection(make_svc, band):
    band.add("w0", "alpha", 500)
    band.add("w1", "bravo", 500, band="bbbb2222")
    band.add("w2", "charlie", 500, band="bbbb2222", os="windows")
    band.add("w3", "delta", 500, band="bbbb2222")
    band.behaviour.update({"w1": "held", "w3": "never"})
    s = make_svc(canary="charlie")
    _feed_with(s, 500, 510)
    assert {m["name"] for m in s.resolve({"mode": "band", "value": "bbbb2222"})} == {"bravo", "charlie", "delta"}
    assert [m["name"] for m in s.resolve({"mode": "os", "value": "windows"})] == ["charlie"]
    ro = await s.start_rollout({"mode": "band", "value": "bbbb2222"}, "human:owner")
    await asyncio.wait_for(s._rollout_task, 15)
    rows = {t["name"]: t for t in ro["targets"]}
    assert rows["charlie"]["status"] == "done" and rows["bravo"]["status"] == "held"
    assert rows["delta"]["status"] == "failed" and "did not come back" in rows["delta"]["detail"]
    assert ro["status"] == "stopped"
    assert s.feed.released_build() == 500     # a partial selection never releases the build
    assert band.workers["w0"]["build"] == 500


@pytest.mark.asyncio
async def test_hub_restart_mid_rollout_is_reported(make_svc, band):
    band.add("w0", "alpha", 500)
    s = make_svc()
    _feed_with(s, 500, 510)
    s.state["rollout"] = {"id": "x", "status": "running", "build": 510, "targets": [
        {"name": "alpha", "status": "waiting", "detail": ""}]}
    await s.tick()
    assert s.state["rollout"]["status"] == "stopped"
    assert s.state["rollout"]["targets"][0]["status"] == "cancelled"


# -- apps and firmware --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_apk_and_notify_phones(make_svc, gh, band):
    apk = b"PK fake apk" * 100
    man = {"package": "systems.bake.rook", "version_name": "0.4.16", "version_code": 20,
           "sha256": hashlib.sha256(apk).hexdigest(), "size": len(apk)}
    gh.add_release(REPO, "v0.3.0", {"release.json": release_json("0.3.0", 530),
                                    "rook-worker.apk": apk,
                                    "rook-worker-apk.json": json.dumps(man).encode()})
    band.add("p1", "phone", 142, app={"platform": "android", "version": "0.4.15"},
             caps=("device.update",))
    band.add("p2", "tablet", 142, app={"platform": "android", "version": "0.4.16"},
             caps=("device.update",))
    s = make_svc()
    await s.check(force=True)
    res = await s.publish_apk("v0.3.0", "human:owner", notify=True)
    assert res["notified"]["sent"] == ["phone"]
    assert [t for c, t, _ in band.calls if c == "device.update"] == ["p1"]
    pub = update_feed.published_apk(s.root)
    assert pub and pub[1]["version_code"] == 20 and pub[0].read_bytes() == apk
    rows = {m["name"]: m for m in s.members()}
    assert rows["phone"]["state"] == "behind" and rows["tablet"]["state"] == "current"


@pytest.mark.asyncio
async def test_firmware_rows(make_svc, gh, band):
    std_release(gh)
    band.add("e1", "kvm-dongle", None, plugins=("info", "kvm"), caps=("kvm.type",), version="0.6.5")
    band.add("w1", "desk", 510, caps=("worker.apply", "dongle.status"))
    s = make_svc()
    await s.check(force=True)
    assert await s.refresh_firmware() == 1
    rows = {m["name"]: m for m in s.members()}
    assert rows["kvm-dongle"]["kind"] == "firmware" and rows["kvm-dongle"]["state"] == "behind"
    assert rows["kvm-dongle"]["target"] == "0.6.7" and not rows["kvm-dongle"]["can_push"]
    dongle = rows["RP2040 dongle on desk"]
    assert dongle["fw_version"] == "0.0.9" and dongle["state"] == "behind"


@pytest.mark.asyncio
async def test_overview_shape(make_svc, gh, band, monkeypatch):
    std_release(gh)
    band.add("w1", "desk", 500)
    s = make_svc()
    _feed_with(s, 500, 510)
    await s.check(force=True)
    monkeypatch.setattr(hubself, "detect", lambda repo="": {"kind": "docker", "can_apply": False,
                                                            "why": "container", "manual": "pull"})
    o = s.overview()
    assert o["hub"]["latest"] == "v0.3.0" and o["hub"]["update_available"]
    assert o["releases"][0]["notes"].startswith("## Rook 0.3.0")
    assert o["releases"][0]["can_apply"] is False and o["feed"] == {"released": 500, "newest": 510,
                                                                     "builds": [500, 510]}
    assert o["members"][0]["state"] == "behind"
    json.dumps(o)  # the account API sends it as JSON


# -- the dashboard side ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_serves_the_released_build_and_snapshots(tmp_path, keyenv, monkeypatch):
    from aiohttp.test_utils import TestClient
    from rook.remote import setup_store
    from rook.remote.bootstrap import CombinedServer
    from rook.remote.enrollment import EnrollmentStore
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    setup_store.save({"band_name": "home", "band_psk": "piano-haste-nugget-drift-mangle",
                      "hub_public": "192.0.2.10:8765", "pyz_domain": "192.0.2.10:7005"})
    EnrollmentStore().import_config()
    server = CombinedServer(web_user="owner", web_pass="password", port=7005,
                            domain="192.0.2.10:7005", hub_public="192.0.2.10:8765")
    manual = tmp_path / "operator"
    server._bundle.manual_dir = manual
    make_bundle(manual, 520)
    feed = update_feed.WorkerFeed()
    old_man, old_pyz = make_bundle(tmp_path / "old", 510)
    feed.snapshot(old_man, old_pyz)
    async with TestClient(TestServer(server._app)) as client:
        served = await (await client.get("/band-worker.json")).json()
        assert served["build"] == 520                 # no feed yet: the bundle, as before
        feed.release(510, by="test")
        served = await (await client.get("/band-worker.json")).json()
        assert served["build"] == 510 and served["url"].endswith("/updates/worker/510/band-worker.pyz")
        pyz = await client.get("/updates/worker/510/band-worker.pyz")
        assert pyz.status == 200 and await pyz.read() == old_pyz.read_bytes()
        assert (await client.get("/updates/worker/999/band-worker.pyz")).status == 404
        # New installs still get the newest bundle.
        assert (await (await client.get("/band-worker.pyz")).read()).startswith(b"bundle 520")
        update_feed.WorkerFeed().root.mkdir(parents=True, exist_ok=True)
        (update_feed.updates_dir() / "summary.json").write_text(json.dumps({"latest": {
            "version": "0.3.0", "html_url": "https://github.example.com/r",
            "assets": {"rook-desktop_0.3.0_amd64.AppImage": "https://dl.example.com/a",
                       "rook-0.3.0.tar.gz": "https://dl.example.com/s"}}}))
        d = await (await client.get("/updates/desktop.json?version=0.1.0&platform=linux")).json()
        assert d["update_available"] and [x["name"] for x in d["downloads"]] == [
            "rook-desktop_0.3.0_amd64.AppImage"]


# -- the account API ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_api_is_operator_only_and_reports_refusals(make_svc, gh, band, monkeypatch):
    from types import SimpleNamespace

    import httpx
    from starlette.applications import Starlette

    from rook.hub.plugins.updates.web import routes
    std_release(gh, min_worker_build=505, breaking=True)
    band.add("w1", "garage", 501)
    s = make_svc()
    await s.check(force=True)
    monkeypatch.setattr(hubself, "detect", lambda repo="": {"kind": "installer", "can_apply": True,
                                                            "mode": "user", "systemd": True})
    changed = []
    settings = SimpleNamespace(set=lambda key, value, **kw: changed.append((key, value)) or {"ok": True})
    node = SimpleNamespace(host=SimpleNamespace(plugins=[SimpleNamespace(NAMESPACE="updates", service=s)]),
                           settings=settings)
    accounts = SimpleNamespace(session=lambda c: {"id": c, "username": c, "csrf": "k",
                                                  "admin": c == "op"} if c else None)
    app = Starlette(routes=routes(lambda: node, accounts))
    api, op = "/updates/account-api", {"Cookie": "rook_account=op"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get(api)).status_code == 401
        assert (await c.get(api, headers={"Cookie": "rook_account=m1"})).status_code == 403
        page = (await c.get(api, headers=op)).json()
        assert page["csrf"] == "k" and page["hub"]["latest"] == "v0.3.0"
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "apply_hub", "tag": "v0.3.0"})
        assert r.status_code == 409 and r.json()["needs_ack"] and r.json()["cut_off"][0]["name"] == "garage"
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "settings", "mode": "auto"})
        assert r.json()["ok"] and changed == [("updates.mode", "auto")]
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "cancel"})
        assert r.status_code == 409 and "no rollout" in r.json()["error"]
        assert (await c.post(api, headers=op, json={"csrf": "k", "action": "nope"})).status_code == 400
