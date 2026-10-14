"""OTA band-reachability gate (rook/worker/plugins/selfupdate.py).

After an update boot the new build is kept only once it has proven a band
round trip; otherwise it is rolled back to band-worker.pyz.prev at the end of
the window instead of waiting for crash boots. The gate is enforced only when
the old build heard the band shortly before it swapped."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from rook.worker._build_info import BUILD
from rook.worker.plugins import selfupdate as su_mod
from rook.worker.plugins.selfupdate import SelfUpdatePlugin

from test_band_protocol import PSK, _until, band_factory  # noqa: F401  (fixture)


class FakeWorker:
    """The slice of Worker the gate uses: contact stamps, peers, request."""

    def __init__(self, peers=("hub-node",), answer=True, heard=True):
        self.band_rx_at = time.monotonic() if heard else None
        self.band_rtt_at = None
        self._peers = list(peers)
        self.answer = answer
        self.calls: list[tuple] = []
        self.announces = 0

    def band_peers(self):
        return list(self._peers)

    async def request(self, cap, args=None, target=None, timeout=10.0):
        self.calls.append((cap, args, target))
        if not self.answer:
            await asyncio.sleep(min(timeout, 0.05))
            raise asyncio.TimeoutError
        self.band_rtt_at = time.monotonic()
        return {"id": "x", "from": target, "ok": True, "result": {}}

    async def announce(self, full=False):
        self.announces += 1


@pytest.fixture
def su(tmp_path, monkeypatch):
    """A selfupdate plugin whose files live in tmp_path, with a fast gate and
    restarts recorded instead of performed."""
    for name, fname in (("_PYZ", "band-worker.pyz"), ("_PREV", "band-worker.pyz.prev"),
                        ("_STATE", "update_state.json"), ("_RESULT", "update_result.json"),
                        ("_HOLD", "hold")):
        monkeypatch.setattr(su_mod, name, tmp_path / fname)
    monkeypatch.setattr(su_mod, "_WORKER_DIR", tmp_path)
    monkeypatch.setattr(su_mod, "_PROBE_SECS", 0.1)
    monkeypatch.setattr(su_mod, "_HEALTH_SECS", 0.05)
    monkeypatch.setenv("ROOK_UPDATE_HEALTH_SECS", "1")
    monkeypatch.delenv("ROOK_UPDATE_URL", raising=False)
    p = SelfUpdatePlugin()
    p.restarts = []
    p._schedule_restart = lambda argv, delay=1.0: p.restarts.append(argv)
    (tmp_path / "band-worker.pyz").write_bytes(b"new build")
    (tmp_path / "band-worker.pyz.prev").write_bytes(b"old build")
    return p


def _swapped(seen_ago=5, **extra):
    st = {"stage": "swapped", "target_build": BUILD, "prev_build": BUILD - 1,
          "attempts": 0, "at": int(time.time()), **extra}
    if seen_ago != "missing":
        st["band_seen_ago"] = seen_ago
    su_mod._STATE.write_text(json.dumps(st), encoding="utf-8")


async def _boot(p):
    await p._boot_check()
    if p._health_task is not None:
        await asyncio.wait_for(p._health_task, 10)


@pytest.mark.asyncio
async def test_round_trip_confirms_the_update(su):
    """(a) The new build reaches the hub node: kept, .prev removed."""
    su._worker = FakeWorker(peers=["hub-node", "other"])
    _swapped()
    await _boot(su)
    assert not su_mod._STATE.exists()
    assert not su_mod._PREV.exists()
    assert su_mod._PYZ.read_bytes() == b"new build"
    assert su.restarts == []
    # One probe, aimed at the hub node, with a cap every node answers.
    assert su._worker.calls == [("caps.describe", {"prefix": "worker.status"}, "hub-node")]
    res = su._last_result()
    assert res["result"] == "ok" and res["check"] == "band" and res["build"] == BUILD
    assert su.heartbeat() is None


@pytest.mark.asyncio
async def test_any_reply_counts_without_probing(su):
    """A reply to another of our requests (e.g. a pushed-secret fetch) proves
    the round trip as well; nothing heard yet means nobody to probe."""
    w = FakeWorker(peers=[], heard=False)
    su._worker = w
    _swapped()

    async def later():
        await asyncio.sleep(0.2)
        w.band_rx_at = w.band_rtt_at = time.monotonic()

    t = asyncio.create_task(later())
    await _boot(su)
    await t
    assert w.calls == []
    assert su._last_result()["check"] == "band" and not su_mod._PREV.exists()


@pytest.mark.parametrize("heard", [True, False])
@pytest.mark.asyncio
async def test_no_band_contact_rolls_back_at_the_deadline(su, monkeypatch, heard):
    """(b) No round trip within ROOK_UPDATE_HEALTH_SECS: .prev restored and a
    restart scheduled at once (no waiting for three crash boots), the reason
    in update_state.json, the result file, the log and the heartbeat."""
    w = FakeWorker(peers=["hub-node", "other"] if heard else [], answer=False, heard=heard)
    su._worker = w
    _swapped()
    began = time.monotonic()
    await _boot(su)
    assert 0.9 <= time.monotonic() - began < 5
    assert su_mod._PYZ.read_bytes() == b"old build"
    assert len(su.restarts) == 1
    st = json.loads(su_mod._STATE.read_text(encoding="utf-8"))
    assert st["stage"] == "rolledback" and st["reason"].startswith("no band contact")
    assert ("no reply to" in st["reason"]) is heard
    assert ("heard nothing" in st["reason"]) is not heard
    if heard:   # probes rotate over the peers heard
        assert {c[2] for c in w.calls} == {"hub-node", "other"}
    res = su._last_result()
    assert res == {**res, "result": "rolled_back", "from_build": BUILD, "to_build": BUILD - 1}
    assert w.announces == 1        # best-effort last word before the restart
    hb = su.heartbeat()            # the bad build, seconds before its restart
    assert hb["update"].startswith("rolled back: no band contact") and hb["bad_build"] == BUILD

    # The restored build boots: it clears the state, keeps reporting why,
    # and refuses the bad build from a hub push until it is old news.
    monkeypatch.setattr("rook.worker._build_info.BUILD", BUILD - 1)
    restored = SelfUpdatePlugin()
    await restored._boot_check()
    assert not su_mod._STATE.exists()
    assert restored.heartbeat()["update"].startswith("rolled back: no band contact")
    assert restored._status()["update"]["last"]["result"] == "rolled_back"
    monkeypatch.setattr("rook.worker._update_verify.verify_manifest", lambda m, pk=None: True)
    res = await restored._apply_manifest({"build": BUILD, "sha256": "x"})
    assert res["action"] == "skipped" and "rolled back" in res["reason"]
    restored._worker = w
    res = await restored._ota_begin({"build": BUILD, "sha256": "x"}, drop_id=1)
    assert res["action"] == "skipped" and restored._ota_rx is None
    # A newer build is not held back.
    async def no_fetch(url, timeout):
        raise OSError("offline")
    restored._http_get = no_fetch
    res = await restored._apply_manifest({"build": BUILD + 1, "sha256": "x", "url": "u"})
    assert "download failed" in res["error"]
    # ...and the skip expires.
    monkeypatch.setattr(su_mod, "_RETRY_BAD_SECS", 0.0)
    assert restored._skip_reason(BUILD) is None


@pytest.mark.parametrize("seen_ago", [None, "missing", 3600])
@pytest.mark.asyncio
async def test_gate_not_enforced_without_recent_contact_before_the_update(su, seen_ago):
    """(c) The old build had not heard the band (never, recorded nothing
    because it predates the gate, or too long ago): rolling back can't fix an
    unreachable band, so only the alive check applies."""
    w = FakeWorker(peers=[], answer=False, heard=False)
    su._worker = w
    _swapped(seen_ago=seen_ago)
    await _boot(su)
    assert w.calls == [] and su.restarts == []
    assert not su_mod._STATE.exists() and not su_mod._PREV.exists()
    assert su._last_result()["check"] == "alive"


@pytest.mark.asyncio
async def test_gate_can_be_switched_off(su, monkeypatch):
    monkeypatch.setenv("ROOK_UPDATE_HEALTH_SECS", "0")
    su._worker = FakeWorker(answer=False)
    _swapped()
    await _boot(su)
    assert su.restarts == [] and su._last_result()["check"] == "alive"


@pytest.mark.asyncio
async def test_no_worker_bound_falls_back_to_the_alive_check(su):
    _swapped()
    await _boot(su)
    assert su._last_result()["check"] == "alive"


@pytest.mark.asyncio
async def test_crash_loop_rollback_is_unchanged_and_recorded(su):
    su._worker = FakeWorker()
    _swapped(stage="verifying", attempts=su_mod._MAX_BOOT_ATTEMPTS)
    await su._boot_check()
    assert su._health_task is None
    assert su_mod._PYZ.read_bytes() == b"old build" and len(su.restarts) == 1
    st = json.loads(su_mod._STATE.read_text(encoding="utf-8"))
    assert st["stage"] == "rolledback" and st["reason"].startswith("crash loop")
    assert su._last_result()["reason"].startswith("crash loop")


@pytest.mark.asyncio
async def test_no_new_swap_while_verifying(su, monkeypatch):
    """A push during the gate would overwrite .prev (the last build known to
    work) with the unverified one."""
    su._worker = FakeWorker(peers=[], answer=False, heard=False)
    _swapped()
    await su._boot_check()
    monkeypatch.setattr("rook.worker._update_verify.verify_manifest", lambda m, pk=None: True)
    res = await su._apply_manifest({"build": BUILD + 5, "sha256": "x"})
    assert res["action"] == "skipped" and "still verifying" in res["reason"]
    su._health_task.cancel()


@pytest.mark.asyncio
async def test_swap_records_pre_update_band_contact(su):
    w = FakeWorker()
    w.band_rx_at = time.monotonic() - 42
    su._worker = w

    async def ok(path):
        return True

    su._smoke_test = ok
    tmp = su_mod._WORKER_DIR / "band-worker.pyz.dl"
    tmp.write_bytes(b"newer")
    res = await su._swap_and_restart(tmp, BUILD + 1, BUILD)
    assert res["action"] == "updated"
    st = json.loads(su_mod._STATE.read_text(encoding="utf-8"))
    assert 41 <= st["band_seen_ago"] <= 44
    # Never heard the band: recorded as None, so the gate won't be enforced.
    w.band_rx_at = None
    tmp.write_bytes(b"newer")
    await su._swap_and_restart(tmp, BUILD + 1, BUILD)
    assert json.loads(su_mod._STATE.read_text(encoding="utf-8"))["band_seen_ago"] is None


# -- over a real relay -------------------------------------------------------

@pytest.mark.parametrize("peer", [True, False])
@pytest.mark.asyncio
async def test_gate_over_a_real_band(band_factory, su, monkeypatch, tmp_path, peer):  # noqa: F811
    """A real Worker after an update boot: with a peer on the band its probe
    gets an (addressed) reply and the build is kept; alone on the band it is
    rolled back. Also checks the Worker's contact stamps."""
    from rook.worker import core
    from rook.worker.core import Worker
    from rook.worker.transports.telesthete_hub import TelestheteHubTransport
    monkeypatch.setenv("ROOK_UPDATE_HEALTH_SECS", "3")
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "gated-id")
    band = await band_factory(routing=True)
    other = None
    if peer:
        other = await band.worker(monkeypatch, tmp_path, name="peer")
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "gated-id")
    _swapped()
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=band.relay.port)
    w = Worker(t, enabled=["selfupdate"], name="gated")
    gated = next(p for p in w.plugins if p.NAMESPACE == "worker")
    restarts = []
    gated._schedule_restart = lambda argv, delay=1.0: restarts.append(argv)
    band.workers.append(w)
    band.tasks.append(asyncio.create_task(w.run()))
    await _until(lambda: gated._health_task is not None)
    if peer:
        async def poke():
            while True:        # announces come every 30 s; don't wait for one
                await asyncio.sleep(0.2)
                await other.announce()
        poker = asyncio.create_task(poke())
        try:
            await asyncio.wait_for(gated._health_task, 10)
        finally:
            poker.cancel()
        assert w.band_rx_at is not None and w.band_rtt_at is not None
        assert w.band_peers()[0] == other.worker_id
        assert w.worker_id not in w.band_peers()
        assert restarts == [] and not su_mod._PREV.exists()
        assert gated._last_result()["check"] == "band"
    else:
        await asyncio.wait_for(gated._health_task, 10)
        assert w.band_rx_at is None and w.band_rtt_at is None
        assert len(restarts) == 1 and su_mod._PYZ.read_bytes() == b"old build"
        assert "heard nothing" in gated._last_result()["reason"]


def test_worker_contact_stamps_ignore_our_own_traffic(tmp_path, monkeypatch):
    from rook.worker import core
    from rook.worker.core import Worker

    class T:
        band_id = None

        async def send(self, payload):
            pass

    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "id")
    w = Worker(T(), enabled=[], name="me")
    asyncio.run(w._on_message(json.dumps({"kind": "announce", "worker_id": w.worker_id}).encode(), (0,)))
    assert w.band_rx_at is None and w.band_peers() == []
    for wid, grants in (("a", False), ("hub", True), ("b", False)):
        msg = {"kind": "announce", "worker_id": wid, **({"grants": []} if grants else {})}
        asyncio.run(w._on_message(json.dumps(msg).encode(), (0,)))
    assert w.band_rx_at is not None and w.band_rtt_at is None
    assert w.band_peers() == ["hub", "b", "a"]      # hub node first, then newest
    for i in range(Worker.BAND_PEERS_MAX + 5):
        asyncio.run(w._on_message(json.dumps({"kind": "announce", "worker_id": f"p{i}"}).encode(), (0,)))
    assert len(w.band_peers()) == Worker.BAND_PEERS_MAX
