"""The hourly roll-up of the heartbeat history (H5, H4, H1; Bake 13 Nov: "build the hourly roll-up,
delete nothing"), on a clock the test moves by hand: the figures per hour with a field that comes
and goes, a restart in the middle of an hour, the one-time roll-up of history kept before it, and
a month read from the roll-up equal, bucket for bucket, to the same question read from the raw rows."""

import asyncio
import json

from nacl.signing import SigningKey

from helpers import Clock
from rook_hub import Hub, history

HOUR = 3600
START = 400_000 * HOUR + 7          # just after the start of an hour, so no heartbeat lands on a boundary
GB = 1 << 30


class Box:
    """An approved worker whose heartbeats are handed to the hub as its link would."""

    def __init__(self, hub, owner, name):
        self.hub, self.name = hub, name
        self.key = SigningKey.generate().verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": name, "key": self.key, "macs": [], "caps": []})
        self.guid = hub.approve(self.key, user=owner)["guid"]

    def beat(self, health):
        message = {"type": "heartbeat", "protocol": 1, "identity": {"name": self.name, "key": self.key},
                   "facts": {"os": "linux", "arch": "x86_64", "hostname": self.name, "form": "python",
                             "version": "0.1.0", "macs": []}, "caps": [], "band": {"gateway_mac": None},
                   "health": health}
        reply, accepted = self.hub._heartbeat(self.key, message, json.dumps(message).encode())
        assert accepted == self.guid, reply


def hours(hub, worker):
    """The worker's roll-up, hour by hour, as plain dicts."""
    with hub._lock:
        return [dict(r) for r in hub._db.execute(
            "SELECT * FROM heartbeat_hours WHERE worker = ? ORDER BY hour", (worker,))]


def raw_count(hub):
    with hub._lock:
        return hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0]


# Three hours: cpu in every heartbeat; battery in some (a laptop unplugged and back); gpu only
# in the last hour (it appeared); ram as total and free. The hours hold 3, 1 and 2 heartbeats.
PLAN = [
    (0, 600, {"cpu": 10, "battery": 80, "ram": {"total": 8 * GB, "free": 6 * GB}}),
    (0, 1800, {"cpu": 20, "battery": 70, "ram": {"total": 8 * GB, "free": 4 * GB}}),
    (0, 3000, {"cpu": 30, "ram": {"total": 8 * GB, "free": 2 * GB}}),
    (1, 1200, {"cpu": 70}),
    (2, 300, {"cpu": 40, "battery": 60, "gpu": 5}),
    (2, 2400, {"cpu": 50, "gpu": 15, "ram": {"total": 16 * GB, "free": 8 * GB}}),
]


def play(hub, clock, box, plan):
    for hour, second, health in plan:
        clock.now = START - 7 + hour * HOUR + second
        box.beat(health)


def test_rollup_per_hour_with_a_field_that_comes_and_goes(tmp_path):
    clock = Clock(START)
    hub = Hub(tmp_path / "hub", clock=clock)
    try:
        owner = hub.auth.create_user("Alice", "pw")["guid"]
        box = Box(hub, owner, "laptop")
        play(hub, clock, box, PLAN)
        h0, h1, h2 = hours(hub, box.guid)
        base = (START - 7) // HOUR
        assert [h["hour"] for h in (h0, h1, h2)] == [base, base + 1, base + 2]
        assert [h["count"] for h in (h0, h1, h2)] == [3, 1, 2]
        assert h0["last_at"] == START - 7 + 3000 and h2["last_at"] == START - 7 + 2 * HOUR + 2400
        assert (h0["cpu_n"], h0["cpu_sum"], h0["cpu_min"], h0["cpu_max"]) == (3, 60, 10, 30)
        # The battery figures cover only the heartbeats that carried it.
        assert (h0["battery_n"], h0["battery_sum"], h0["battery_min"], h0["battery_max"]) == (2, 150, 70, 80)
        assert (h1["battery_n"], h1["battery_min"], h1["battery_max"]) == (0, None, None)
        assert (h2["battery_n"], h2["battery_sum"], h2["battery_min"]) == (1, 60, 60)
        # The GPU appeared in the last hour only.
        assert (h0["gpu_n"], h1["gpu_n"], h2["gpu_n"], h2["gpu_sum"], h2["gpu_max"]) == (0, 0, 2, 20, 15)
        # RAM is the share in use (25, 50, 75 percent), with the largest total seen.
        assert (h0["ram_n"], h0["ram_sum"], h0["ram_min"], h0["ram_max"], h0["ram_total"]) == (3, 150, 25, 75, 8 * GB)
        assert (h2["ram_n"], h2["ram_sum"], h2["ram_total"]) == (1, 50, 16 * GB)

        # Over the three hours, one bucket: each average is the average of the heartbeats,
        # (10+20+30+70+40+50) / 6 = 36.67, not of the hourly averages ((20+70+45) / 3 = 45).
        clock.now = START - 7 + 3 * HOUR - 60
        reply = asyncio.run(history.series(hub, box.guid, 3 * HOUR, 3 * HOUR))
        assert reply["source"] == "hourly" and reply["step"] == 3 * HOUR
        [bucket] = reply["buckets"]
        assert bucket["count"] == 6 and bucket["cpu"] == 36.67
        assert bucket["battery"] == 70.0 and bucket["gpu"] == 10.0 and bucket["ram"] == 50.0
        assert reply["fields"] == ["cpu", "ram", "gpu", "battery"] and reply["totals"] == {"ram": 16 * GB}
        assert raw_count(hub) == len(PLAN)
    finally:
        hub.close()


def test_restart_in_the_middle_of_an_hour(tmp_path):
    """The same figures whether or not the hub restarted halfway through an hour."""
    clock = Clock(START)
    straight = Hub(tmp_path / "straight", clock=clock)
    owner = straight.auth.create_user("Alice", "pw")["guid"]
    box = Box(straight, owner, "laptop")
    play(straight, clock, box, PLAN)
    expected = [{k: v for k, v in h.items() if k != "worker"} for h in hours(straight, box.guid)]
    straight.close()

    hub = Hub(tmp_path / "restarted", clock=clock)
    owner = hub.auth.create_user("Alice", "pw")["guid"]
    box = Box(hub, owner, "laptop")
    play(hub, clock, box, PLAN[:2])                 # two heartbeats into the first hour, then a restart
    hub.close()
    hub = Hub(tmp_path / "restarted", clock=clock)
    box.hub = hub
    try:
        play(hub, clock, box, PLAN[2:])
        assert [{k: v for k, v in h.items() if k != "worker"} for h in hours(hub, box.guid)] == expected
        assert raw_count(hub) == len(PLAN)
    finally:
        hub.close()


def test_history_kept_before_the_rollup_is_rolled_up_once(tmp_path):
    """A hub with heartbeats and no roll-up rolls them up as it starts, to the figures the
    incremental path gives for the same heartbeats; and it does so only once."""
    clock = Clock(START)
    hub = Hub(tmp_path / "hub", clock=clock)
    owner = hub.auth.create_user("Alice", "pw")["guid"]
    laptop, desk = Box(hub, owner, "laptop"), Box(hub, owner, "desk")
    play(hub, clock, laptop, PLAN)
    play(hub, clock, desk, [(h, s + 60, dict(health, cpu=health["cpu"] + 1)) for h, s, health in PLAN])
    incremental = {w: hours(hub, w) for w in (laptop.guid, desk.guid)}
    with hub._lock, hub._db:
        hub._db.execute("DELETE FROM heartbeat_hours")      # as a hub kept its history before this slice
    hub.close()

    hub = Hub(tmp_path / "hub", clock=clock)
    try:
        assert {w: hours(hub, w) for w in (laptop.guid, desk.guid)} == incremental
        assert raw_count(hub) == 2 * len(PLAN)
        laptop.hub = hub
        clock.now = START - 7 + 3 * HOUR + 100
        laptop.beat({"cpu": 99})                            # new heartbeats add to it as before
        hub.close()
        hub = Hub(tmp_path / "hub", clock=clock)            # a second start rolls nothing up again
        assert [h["count"] for h in hours(hub, laptop.guid)] == [3, 1, 2, 1]
        assert raw_count(hub) == 2 * len(PLAN) + 1
    finally:
        hub.close()


def test_a_month_from_the_rollup_equals_the_raw_rows(tmp_path, monkeypatch):
    """A month's series and counts, answered from the roll-up, equal bucket for bucket the
    same question answered from the raw rows; a short span is still answered from the raw rows."""
    clock = Clock(START)
    hub = Hub(tmp_path / "hub", clock=clock)
    try:
        owner = hub.auth.create_user("Alice", "pw")["guid"]
        laptop, desk = Box(hub, owner, "laptop"), Box(hub, owner, "desk")
        month = 30 * 24 * HOUR
        for i in range(month // 1200):                     # every 20 minutes, never on an hour boundary
            clock.now = START + i * 1200 + 13
            laptop.beat({"cpu": i % 97, "ram": {"total": 8 * GB, "free": (i % 8) * GB}, "uptime": i * 1200,
                         **({"battery": 100 - i % 50} if (i // 30) % 2 else {})})       # unplugged now and then
            if i % 3:                                      # the desk misses every third one
                desk.beat({"cpu": (i * 7) % 89, "temperature": 40 + i % 20})
        clock.now = START - 7 + month                       # the hour boundary after the last heartbeat
        before = raw_count(hub)

        def ask(span, step=None):
            return (asyncio.run(history.series(hub, laptop.guid, span, step)),
                    asyncio.run(history.series(hub, desk.guid, span, step)),
                    asyncio.run(history.counts(hub, span, step)))
        hourly = ask(month)
        assert [r["source"] for r in hourly] == ["hourly"] * 3 and hourly[0]["step"] == 6 * HOUR
        assert len(hourly[0]["buckets"]) == 120 and sum(b["count"] for b in hourly[0]["buckets"]) == month // 1200
        assert hourly[0]["fields"] == ["cpu", "ram", "battery", "uptime"]

        original = history._window

        def from_raw(hub, span, step):                      # the same window, read from the raw rows
            source, now, end, since, step, *_ = original(hub, span, step)
            return "raw", now, end, since, step, history.SERIES, history.COUNTS
        monkeypatch.setattr(history, "_window", from_raw)
        raw = ask(month)
        monkeypatch.setattr(history, "_window", original)
        for got, want in zip(hourly, raw):
            assert dict(got, source="raw") == want

        short = ask(3 * HOUR)                               # a short span: the raw rows, as before
        assert [r["source"] for r in short] == ["raw"] * 3 and short[0]["step"] == 90
        assert ask(6 * HOUR, HOUR)[0]["source"] == "hourly"   # a step of an hour or more: the roll-up
        assert raw_count(hub) == before                     # nothing was deleted
    finally:
        hub.close()
