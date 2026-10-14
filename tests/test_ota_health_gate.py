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


def _swapped(seen_ago=5, probe_target=True, **extra):
    st = {"stage": "swapped", "target_build": BUILD, "prev_build": BUILD - 1,
          "attempts": 0, "at": int(time.time()), "probe_target": probe_target, **extra}
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
    # heard=False: nothing at all arrives (peers are only learned from
    # announces, so there is no one to probe either).
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
    assert st["recheck"] is True
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


@pytest.mark.parametrize("case", [
    {"seen_ago": None}, {"seen_ago": "missing", "probe_target": None},   # pre-gate swap
    {"seen_ago": 3600},
    {"probe_target": False},                    # node-less hub: nobody announced
    {"at": int(time.time()) - 3600},            # swapped long ago (host was off)
])
@pytest.mark.asyncio
async def test_gate_not_enforced_without_recent_contact_before_the_update(su, case):
    """(c) The old build had not heard the band (never, recorded nothing
    because it predates the gate, or too long ago), had nobody to probe, or
    the swap itself is old: rolling back can't fix that, so only the alive
    check applies."""
    w = FakeWorker(peers=[], answer=False, heard=False)
    su._worker = w
    _swapped(**case)
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

@pytest.mark.parametrize("case", ["peer", "nodeless-swap", "nodeless-calls"])
@pytest.mark.asyncio
async def test_gate_over_a_real_band(band_factory, su, monkeypatch, tmp_path, case):  # noqa: F811
    """A real Worker after an update boot over the test relay.

    peer: another worker announces; the probe gets an (addressed) reply and
    the build is kept. nodeless-*: a hub without its node (only a band
    client, which never announces): the old build recorded no probe target,
    so the gate isn't enforced; and if it was enforced anyway (a peer left)
    but the hub keeps calling us, the alive check decides, no rollback."""
    from rook.worker import core
    from rook.worker.core import Worker
    from rook.worker.transports.telesthete_hub import TelestheteHubTransport
    monkeypatch.setenv("ROOK_UPDATE_HEALTH_SECS", "3")
    band = await band_factory(routing=True)
    other = None
    if case == "peer":
        other = await band.worker(monkeypatch, tmp_path, name="peer")
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "gated-id")
    _swapped(probe_target=case != "nodeless-swap")
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=band.relay.port)
    w = Worker(t, enabled=["selfupdate"], name="gated")
    gated = next(p for p in w.plugins if p.NAMESPACE == "worker")
    restarts = []
    gated._schedule_restart = lambda argv, delay=1.0: restarts.append(argv)
    band.workers.append(w)
    band.tasks.append(asyncio.create_task(w.run()))
    await _until(lambda: gated._health_task is not None)
    client = await band.client() if case != "peer" else None

    async def poke():
        while True:        # announces come every 30 s; don't wait for one
            await asyncio.sleep(0.2)
            if other is not None:
                await other.announce()
            if case == "nodeless-calls":
                await client.call("caps.describe", target=w.worker_id, timeout=2)

    poker = asyncio.create_task(poke())
    try:
        await asyncio.wait_for(gated._health_task, 10)
    finally:
        poker.cancel()
    assert restarts == [] and not su_mod._PREV.exists()
    assert su_mod._PYZ.read_bytes() == b"new build"
    check = gated._last_result()["check"]
    if case == "peer":
        assert check == "band"
        assert w.band_rx_at is not None and w.band_rtt_at is not None
        assert w.band_peers()[0] == other.worker_id
        assert w.worker_id not in w.band_peers()
    elif case == "nodeless-swap":
        assert check == "alive"
    else:
        assert check == "alive (no probe target)" and w.band_rx_at is not None
        assert w.band_peers() == []


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


# -- review follow-ups -------------------------------------------------------

@pytest.mark.asyncio
async def test_heard_but_nobody_to_probe_keeps_the_build(su):
    """Calls arrive (the band works) but no peer announces: nothing to
    probe, so the alive check decides instead of a rollback."""
    su._worker = FakeWorker(peers=[], answer=False, heard=True)
    _swapped()
    await _boot(su)
    assert su.restarts == [] and su._worker.calls == []
    assert su._last_result()["check"] == "alive (no probe target)"


@pytest.mark.parametrize("restored_reaches_band", [True, False])
@pytest.mark.asyncio
async def test_rollback_is_rechecked_by_the_restored_build(su, monkeypatch,
                                                         restored_reaches_band):
    """Hub outage mid-window: the new build is rolled back, then the restored
    build can't reach the band either, so the result is inconclusive and the
    build is held back only for about an hour. If the restored build does
    reach the band, the new build was at fault: the 24 h wait stands."""
    su._worker = FakeWorker(answer=False)
    _swapped()
    await _boot(su)
    assert su._last_result()["result"] == "rolled_back"

    monkeypatch.setattr("rook.worker._build_info.BUILD", BUILD - 1)
    restored = SelfUpdatePlugin()
    restored._worker = FakeWorker(answer=restored_reaches_band)
    await restored._boot_check()
    await asyncio.wait_for(restored._recheck_task, 5)
    res = restored._last_result()
    assert res["from_build"] == BUILD and res["to_build"] == BUILD - 1
    assert restored._skip_reason(BUILD)
    if restored_reaches_band:
        assert res["result"] == "rolled_back"
        monkeypatch.setattr(su_mod, "_RETRY_INCONCLUSIVE_SECS", 0.0)
        assert restored._skip_reason(BUILD)                  # still 24 h
    else:
        assert res["result"] == "inconclusive" and "inconclusive" in res["reason"]
        assert restored.heartbeat()["update"].endswith("(inconclusive)")
        monkeypatch.setattr(su_mod, "_RETRY_INCONCLUSIVE_SECS", 0.0)
        assert restored._skip_reason(BUILD) is None          # short wait only


@pytest.mark.asyncio
async def test_crash_loop_rollback_is_not_rechecked(su):
    su._worker = FakeWorker()
    _swapped(stage="verifying", attempts=su_mod._MAX_BOOT_ATTEMPTS)
    await su._boot_check()
    assert json.loads(su_mod._STATE.read_text(encoding="utf-8"))["recheck"] is False


@pytest.mark.asyncio
async def test_suspend_gap_extends_the_window(su, monkeypatch):
    """A gate tick that returns far later than planned (the host slept)
    pushes the deadline out by the gap instead of rolling back at once."""
    real = time.monotonic
    jumped = {"by": 0.0}

    def clock():
        return real() + jumped["by"]

    monkeypatch.setattr(su_mod, "_now", clock)
    w = FakeWorker(peers=[], heard=True, answer=False)
    w._peers = ["hub-node"]
    su._worker = w
    _swapped()

    async def sleep_then_wake():
        await asyncio.sleep(0.3)
        jumped["by"] = 120.0              # "suspended" for two minutes: without
        await asyncio.sleep(0.3)          # the extension the gate ends right here
        w.band_rtt_at = real()            # the band is back after resume

    t = asyncio.create_task(sleep_then_wake())
    await _boot(su)
    await t
    assert su.restarts == [] and su._last_result()["check"] == "band"


@pytest.mark.asyncio
async def test_plugin_reload_does_not_count_as_a_boot(su):
    """A reload of the selfupdate plugin re-runs the boot check in the same
    process (same boot id): no attempt is counted."""
    su._worker = FakeWorker()
    _swapped(stage="verifying", attempts=su_mod._MAX_BOOT_ATTEMPTS,
             boots=su_mod._MAX_BOOT_ATTEMPTS, boot=su_mod._boot_id())
    await _boot(su)
    assert su.restarts == [] and su._last_result()["check"] == "band"


@pytest.mark.asyncio
async def test_restart_after_staying_up_is_not_a_crash_boot(su):
    """The previous run stayed up _HEALTH_SECS (e.g. restarted for a settings
    change): its boot doesn't count as a crash. Total boots stay bounded."""
    su._worker = FakeWorker()
    _swapped(stage="verifying", attempts=su_mod._MAX_BOOT_ATTEMPTS, boots=3,
             boot="previous-run", survived="previous-run")
    await su._boot_check()
    st = json.loads(su_mod._STATE.read_text(encoding="utf-8"))
    assert st["attempts"] == su_mod._MAX_BOOT_ATTEMPTS and st["boots"] == 4
    assert st["boot"] == su_mod._boot_id()
    await asyncio.wait_for(su._health_task, 5)
    assert su.restarts == []

    su_mod._PREV.write_bytes(b"old build")      # the confirm above removed it
    _swapped(stage="verifying", attempts=1, boots=su_mod._MAX_BOOTS,
             boot="previous-run", survived="previous-run")
    await su._boot_check()
    assert len(su.restarts) == 1 and su._last_result()["reason"].startswith("crash loop")


@pytest.mark.asyncio
async def test_gate_marks_the_run_survived(su):
    su._worker = FakeWorker(peers=["hub-node"], answer=False)
    _swapped()
    await su._boot_check()
    await _until(lambda: json.loads(su_mod._STATE.read_text(encoding="utf-8"))
                 .get("survived") == su_mod._boot_id(), timeout=3)
    su._health_task.cancel()


@pytest.mark.asyncio
async def test_failed_rollback_copy_is_recorded(su, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("shutil.copy2", broken)
    su._worker = FakeWorker(answer=False)
    _swapped()
    await _boot(su)
    res = su._last_result()
    assert res["result"] == "rollback_failed" and "disk full" in res["reason"]
    assert res["reason"].startswith("no band contact") and su.restarts == []


@pytest.mark.asyncio
async def test_swap_records_the_probe_target(su):
    w = FakeWorker(peers=[])
    su._worker = w

    async def ok(path):
        return True

    su._smoke_test = ok
    tmp = su_mod._WORKER_DIR / "band-worker.pyz.dl"
    for peers, rtt, want in (([], None, False), (["hub-node"], None, True), ([], 1.0, True)):
        w._peers, w.band_rtt_at = peers, rtt
        tmp.write_bytes(b"newer")
        await su._swap_and_restart(tmp, BUILD + 1, BUILD)
        assert json.loads(su_mod._STATE.read_text(encoding="utf-8"))["probe_target"] is want


@pytest.mark.asyncio
async def test_late_reply_still_proves_the_round_trip(tmp_path, monkeypatch):
    from rook.worker import core
    from rook.worker.core import Worker

    class T:
        band_id = None
        sent: list = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "id")
    w = Worker(T(), enabled=[], name="me")
    with pytest.raises(asyncio.TimeoutError):
        await w.request("caps.describe", {}, target="hub", timeout=0.05)
    mid = T.sent[-1]["id"]
    await w._on_message(json.dumps({"id": "someone-else", "from": "hub", "ok": True}).encode(), (0,))
    assert w.band_rtt_at is None
    await w._on_message(json.dumps({"id": mid, "from": "hub", "ok": True}).encode(), (0,))
    assert w.band_rtt_at is not None
    await w._on_message(json.dumps({"id": ["junk"], "from": "hub", "ok": True}).encode(), (0,))
