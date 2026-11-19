"""The heartbeat history after the stress test (H5 as Bake narrowed it, H4, H1): a stored
heartbeat is its health numbers, with no caps; raw rows older than 12 hours are rolled into
one-minute buckets (lowest, highest, how many) by the hub cap heartbeat.roll-up and deleted;
newer rows are untouched; a second run changes nothing; a hub with heartbeats in the old shape
is converted once as it starts; and the charts answer the same for the last 12 hours, and from
the buckets beyond. A real hub on a clock the test moves by hand."""

import asyncio
import datetime
import json

from nacl.signing import SigningKey

from helpers import Clock, link_fields
from rook_hub import Hub, history

MINUTE, HOUR, DAY = 60, 3600, 86400
START = 400_000 * HOUR              # on a minute (and an hour)
GB = 1 << 30
CAP = {"name": "demo.echo", "description": "Echo the text back.", "returns": "result",
       "schema": {"type": "object", "properties": {"text": {"type": "string"}}}}
WHY = "a test of the heartbeat roll-up"


class Box:
    """An approved worker whose heartbeats are handed to the hub as its link would."""

    def __init__(self, hub, owner, name):
        self.hub, self.name = hub, name
        signing_key = SigningKey.generate()
        self.key = signing_key.verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": name, "key": self.key, "macs": [], "caps": [],
                   **link_fields(signing_key)})
        self.guid = hub.approve(self.key, user=owner)["guid"]

    def message(self, health, **more):
        return {"type": "heartbeat", "protocol": 1, "identity": {"name": self.name, "key": self.key},
                "facts": {"os": "linux", "arch": "x86_64", "hostname": self.name, "form": "python",
                          "version": "0.1.0", "macs": []}, "caps": [CAP] * 20, "band": {"gateway_mac": None},
                "health": health, **more}

    def beat(self, health, **more):
        reply, accepted = self.hub._heartbeat(self.key, self.message(health, **more))
        assert accepted == self.guid, reply


def table(hub, name, order):
    with hub._lock:
        return [dict(r) for r in hub._db.execute(f"SELECT * FROM {name} ORDER BY {order}")]


def roll_up(hub, user):
    reply = asyncio.run(hub.call("hub", "heartbeat.roll-up", {}, WHY, user=user))
    assert reply["type"] == "result", reply
    return reply["result"]


def test_a_stored_heartbeat_is_its_health_numbers(tmp_path):
    """The caps, facts and the rest are kept once, as the worker's current state; a stored heartbeat
    keeps its health block, config_version and max_message (the hub reads the last two)."""
    hub = Hub(tmp_path / "hub", clock=Clock(START))
    try:
        owner = hub.auth.create_user("Alice", "pw")["guid"]
        box = Box(hub, owner, "laptop")
        box.beat({"cpu": 12, "ram": {"total": 8 * GB, "free": 2 * GB}}, config_version="abc", max_message=4096,
                 plugins=[], streams=0, open_streams=[])
        box.beat({"cpu": 13})
        first, second = (json.loads(r["body"]) for r in table(hub, "heartbeats", "id"))
        assert first == {"health": {"cpu": 12, "ram": {"total": 8 * GB, "free": 2 * GB}}, "config_version": "abc",
                         "max_message": 4096}
        assert second == {"health": {"cpu": 13}}
        record = hub.get_worker(box.guid)
        assert record["caps"] == [CAP] * 20 and record["facts"]["hostname"] == "laptop"   # once, current
        box.beat({"cpu": 14}, max_message=4096)
        assert hub.largest_message(box.guid) == 4096       # read from the worker's state (D4)
    finally:
        hub.close()


# One worker, beats in two old minutes and in the last 12 hours.
OLD_A = START + 10 * MINUTE          # three heartbeats: cpu 10, 20, 60; battery in two; ram 25 % and 75 %
OLD_B = START + 11 * MINUTE          # one: a gpu appears


def test_rows_older_than_12_hours_become_minute_buckets(tmp_path):
    clock = Clock(START)
    hub = Hub(tmp_path / "hub", clock=clock)
    try:
        owner = hub.auth.create_user("Alice", "pw")["guid"]
        box, desk = Box(hub, owner, "laptop"), Box(hub, owner, "desk")
        for at, health in [(OLD_A + 5, {"cpu": 10, "battery": 80, "ram": {"total": 8 * GB, "free": 6 * GB}}),
                           (OLD_A + 25, {"cpu": 60, "ram": {"total": 8 * GB, "free": 2 * GB}, "uptime": 100}),
                           (OLD_A + 45, {"cpu": 20, "battery": 70, "uptime": 140}),
                           (OLD_B + 30, {"cpu": 40, "gpu": 5, "vram": {"total": 24 * GB, "free": 6 * GB}})]:
            clock.now = at
            box.beat(health)
        clock.now = OLD_A + 50
        desk.beat({"temperature": 41.5})
        late = START + 13 * HOUR             # more than 12 hours after the old minutes ...
        for i in range(3):                   # ... and these are less than 12 hours old at the roll-up
            clock.now = late + i * 20
            box.beat({"cpu": 90 + i})
        newer = table(hub, "heartbeats", "id")[-3:]
        clock.now = START + 13 * HOUR + 2 * MINUTE

        done = roll_up(hub, owner)
        assert done["rolled_up"] == 5 and done["minutes"] == 3
        minutes = table(hub, "heartbeat_minutes", "worker, minute")
        a, b = [m for m in minutes if m["worker"] == box.guid]
        assert (a["minute"], a["count"]) == (OLD_A // MINUTE, 3) and (b["minute"], b["count"]) == (OLD_B // MINUTE, 1)
        assert (a["cpu_min"], a["cpu_max"], a["battery_min"], a["battery_max"]) == (10, 60, 70, 80)
        assert (a["ram_min"], a["ram_max"], a["ram_total"]) == (25, 75, 8 * GB)
        assert (a["uptime_min"], a["uptime_max"], a["gpu_min"], a["gpu_max"]) == (100, 140, None, None)
        assert (b["cpu_min"], b["cpu_max"], b["gpu_min"], b["vram_max"], b["vram_total"]) == (40, 40, 5, 75, 24 * GB)
        assert (b["battery_min"], b["ram_total"]) == (None, None)
        [d] = [m for m in minutes if m["worker"] == desk.guid]
        assert (d["count"], d["temperature_min"], d["temperature_max"]) == (1, 41.5, 41.5)
        assert table(hub, "heartbeats", "id") == newer            # gone as raw; the newer rows untouched

        again = roll_up(hub, owner)                               # a second run changes nothing
        assert again["rolled_up"] == 0 and again["minutes"] == 3
        assert table(hub, "heartbeat_minutes", "worker, minute") == minutes
        assert table(hub, "heartbeats", "id") == newer
    finally:
        hub.close()


def test_a_minute_the_12_hours_end_inside_is_completed_by_the_next_run(tmp_path):
    clock = Clock(START)
    hub = Hub(tmp_path / "hub", clock=clock)
    try:
        owner = hub.auth.create_user("Alice", "pw")["guid"]
        box = Box(hub, owner, "laptop")
        for at, cpu in ((OLD_A + 10, 30), (OLD_A + 40, 5)):
            clock.now = at
            box.beat({"cpu": cpu})
        clock.now = OLD_A + 25 + 12 * HOUR            # the 12 hours end inside the minute
        assert roll_up(hub, owner)["rolled_up"] == 1
        [m] = table(hub, "heartbeat_minutes", "minute")
        assert (m["count"], m["cpu_min"], m["cpu_max"]) == (1, 30, 30)
        clock.now += 30
        assert roll_up(hub, owner)["rolled_up"] == 1
        [m] = table(hub, "heartbeat_minutes", "minute")
        assert (m["count"], m["cpu_min"], m["cpu_max"]) == (2, 5, 30)
        assert table(hub, "heartbeats", "id") == []
    finally:
        hub.close()


def test_the_charts_answer_the_same_for_12_hours_and_from_buckets_beyond(tmp_path):
    """A day of heartbeats every 20 s; the same questions asked before and after the roll-up."""
    clock = Clock(START)
    hub = Hub(tmp_path / "hub", clock=clock)
    try:
        owner = hub.auth.create_user("Alice", "pw")["guid"]
        laptop, desk = Box(hub, owner, "laptop"), Box(hub, owner, "desk")
        plan = {}                                          # laptop: minute -> its cpu values
        for i in range(DAY // 20):
            clock.now = START + i * 20 + 7                # never on a minute
            cpu = (i * 37) % 101
            laptop.beat({"cpu": cpu, "ram": {"total": 8 * GB, "free": (i % 8) * GB}, "uptime": i * 20,
                         **({"battery": 100 - i % 50} if (i // 90) % 2 else {})})
            plan.setdefault(int(clock.now // MINUTE), []).append(cpu)
            if i % 3:                                      # the desk misses every third one
                desk.beat({"cpu": (i * 7) % 89, "temperature": 40 + i % 20})
        clock.now = START + DAY                            # on a minute

        def ask():
            return {"laptop 12 h": asyncio.run(history.series(hub, laptop.guid, 12 * HOUR, 600)),
                    "desk 12 h": asyncio.run(history.series(hub, desk.guid, 12 * HOUR)),
                    "counts 12 h": asyncio.run(history.counts(hub, 12 * HOUR, 300)),
                    "counts day": asyncio.run(history.counts(hub, DAY, HOUR)),
                    "counts month": asyncio.run(history.counts(hub, 30 * DAY, DAY)),
                    "laptop day": asyncio.run(history.series(hub, laptop.guid, DAY, MINUTE)),
                    "desk day": asyncio.run(history.series(hub, desk.guid, DAY, HOUR))}
        before = ask()
        assert roll_up(hub, owner)["rolled_up"] == (DAY // 2) // 20 + (DAY // 2) // 20 * 2 // 3
        after = ask()

        for name in ("laptop 12 h", "desk 12 h", "counts 12 h", "counts day", "counts month"):
            assert after[name] == before[name], name      # the same answer, raw or rolled up
        old, new = before["laptop day"], after["laptop day"]
        assert new["fields"] == old["fields"] == ["cpu", "ram", "battery", "uptime"] and new["totals"] == old["totals"]
        assert [b["count"] for b in new["buckets"]] == [b["count"] for b in old["buckets"]]
        assert len(new["buckets"]) == DAY // MINUTE
        older = 0
        for was, got in zip(old["buckets"], new["buckets"]):
            at = datetime.datetime.fromisoformat(got["at"]).timestamp()
            if at >= START + DAY // 2:                     # the last 12 hours: raw, as before
                assert got == was
            else:                                          # older: the middle of the minute's lowest and highest
                cpus = plan[int(at // MINUTE)]
                assert got["cpu"] == round((min(cpus) + max(cpus)) / 2, 2) and got["uptime"] == was["uptime"]
                older += 1
        assert older == DAY // MINUTE // 2
        desk_old, desk_new = before["desk day"]["buckets"], after["desk day"]["buckets"]
        assert [b["count"] for b in desk_new] == [b["count"] for b in desk_old]
        assert desk_new[-12:] == desk_old[-12:]             # the last 12 hours
    finally:
        hub.close()


OLD_HOURS = "CREATE TABLE heartbeat_hours (worker TEXT NOT NULL, hour INTEGER NOT NULL, count INTEGER NOT NULL)"


def test_a_hub_with_heartbeats_in_the_old_shape_is_converted_once(tmp_path):
    """Whole heartbeats (caps and all) and an hourly roll-up, as the core before this kept them:
    at the next start the old rows are rolled up, the rest trimmed, the hourly table dropped;
    a second start changes nothing."""
    clock = Clock(START)
    hub = Hub(tmp_path / "hub", clock=clock)
    owner = hub.auth.create_user("Alice", "pw")["guid"]
    box = Box(hub, owner, "laptop")
    whole = []
    with hub._lock, hub._db:
        for at, cpu in ((OLD_A + 5, 10), (OLD_A + 35, 50), (START + 20 * HOUR, 70), (START + 20 * HOUR + 15, 80)):
            message = box.message({"cpu": cpu}, config_version="v1")
            whole.append(message)
            hub._db.execute("INSERT INTO heartbeats (worker, at, body) VALUES (?, ?, ?)",
                            (box.guid, at, json.dumps(message)))
        hub._db.execute(OLD_HOURS)
        hub._db.execute("INSERT INTO heartbeat_hours VALUES (?, ?, 4)", (box.guid, START // HOUR))
        size = hub._db.execute("SELECT avg(length(body)) FROM heartbeats").fetchone()[0]
    hub.close()
    clock.now = START + 22 * HOUR

    hub = Hub(tmp_path / "hub", clock=clock)
    try:
        [m] = table(hub, "heartbeat_minutes", "minute")
        assert (m["minute"], m["count"], m["cpu_min"], m["cpu_max"]) == (OLD_A // MINUTE, 2, 10, 50)
        rows = table(hub, "heartbeats", "id")
        assert [json.loads(r["body"]) for r in rows] == [history.stored(w) for w in whole[2:]]
        assert [json.loads(r["body"]) for r in rows] == [{"health": {"cpu": 70}, "config_version": "v1"},
                                                         {"health": {"cpu": 80}, "config_version": "v1"}]
        with hub._lock:
            assert hub._db.execute("SELECT 1 FROM sqlite_master WHERE name = 'heartbeat_hours'").fetchone() is None
            assert hub._db.execute("SELECT avg(length(body)) FROM heartbeats").fetchone()[0] < size / 10
        kept = table(hub, "heartbeat_minutes", "minute"), rows
    finally:
        hub.close()

    clock.now += 6 * HOUR             # a second start: nothing converted again, nothing rolled up at start
    hub = Hub(tmp_path / "hub", clock=clock)
    try:
        assert (table(hub, "heartbeat_minutes", "minute"), table(hub, "heartbeats", "id")) == kept
    finally:
        hub.close()
