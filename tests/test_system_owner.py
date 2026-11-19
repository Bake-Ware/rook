"""The system owner and the job it owns (Bake 19 Nov: "Make a system role that can function as
owner."; brief 56). Every hub has a system owner from its first start, and a hub made before it
gets one at its next start, once. It cannot log in and a person cannot take its name. It owns the
built-in heartbeat roll-up job, which fires every 12 hours, runs as the system and rolls up the raw
heartbeats older than 12 hours (H5 as Bake narrowed it). A real hub on a clock the test moves."""

import asyncio
import json
import sqlite3

import pytest

from helpers import Clock, eventually
from rook_hub import Hub, history, jobs, journal, site
from rook_hub.auth import SYSTEM_MONIKER, SYSTEM_NAME, _hash_password
from rook_hub.errors import HubError
from test_history_rollup import Box

HOUR = 3600
START = 40_000 * 12 * HOUR          # on a 12-hour boundary, where "every 720m" fires
WHY = "a test of the system owner"


def users(hub):
    with hub._lock:
        return [dict(r) for r in hub._db.execute("SELECT * FROM users ORDER BY created_at")]


def built_in(hub):
    """The jobs the system owner owns, as job.list shows them."""
    return [j for j in jobs.list_jobs(hub, {}, None)["jobs"] if j["owner"] == SYSTEM_MONIKER]


def call(hub, cap, args, user):
    reply = asyncio.run(hub.call("hub", cap, args, WHY, user=user))
    assert reply["type"] == "result", reply
    return reply["result"]


def test_a_new_hub_has_the_system_owner_and_the_job_before_any_user(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock(START))
    try:
        [system] = users(hub)
        assert system["guid"] == hub.auth.system and system["system"] == 1
        assert (system["name"], system["moniker"], system["password"]) == (SYSTEM_NAME, SYSTEM_MONIKER, "")
        assert hub.auth.get_user(SYSTEM_MONIKER)["system"] is True
        assert not hub.auth.has_users() and hub.auth.list_users() == []   # no person: the site still offers setup
        [job] = built_in(hub)
        assert job["match"] == {"event": "schedule", "payload": {"schedule": "every 720m"}}
        assert job["call"] == {"worker": "hub", "cap": "heartbeat.roll-up", "args": {}}
        assert not job["paused"] and job["title"] == "Heartbeat roll-up"
        with hub._lock:
            assert hub._db.execute("SELECT count(*) FROM tokens").fetchone()[0] == 0   # none of its own

        # The first person is made by setup as before, and is the hub's only person.
        alice = hub.auth.create_user("Alice", "a password", first=True)
        assert hub.auth.has_users() and alice["system"] is False
    finally:
        hub.close()

    hub = Hub(tmp_path / "hub", clock=Clock(START + HOUR))   # the next start makes neither again
    try:
        assert [u["guid"] for u in users(hub) if u["system"]] == [system["guid"]]
        assert [j["guid"] for j in built_in(hub)] == [job["guid"]]
    finally:
        hub.close()


def older_core(path):
    """Turn the hub at `path` back into one made by a core without the system owner: no system row,
    its moniker free, no system column, no built-in job."""
    db = sqlite3.connect(path / "hub.db")
    with db:
        system = db.execute("SELECT guid FROM users WHERE system = 1").fetchone()[0]
        db.execute("DELETE FROM jobs WHERE owner = ?", (system,))
        db.execute("DELETE FROM users WHERE guid = ?", (system,))
        db.execute("DELETE FROM monikers WHERE guid = ?", (system,))
    db.execute("ALTER TABLE users DROP COLUMN system")
    db.close()


def test_a_hub_made_by_an_older_core_gets_both_on_its_next_start_once(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock(START))
    alice = hub.auth.create_user("Alice", "a password", first=True)
    hub.close()
    older_core(tmp_path / "hub")
    db = sqlite3.connect(tmp_path / "hub" / "hub.db")
    assert "system" not in {r[1] for r in db.execute("PRAGMA table_info(users)")}
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    db.close()

    hub = Hub(tmp_path / "hub", clock=Clock(START + HOUR))
    try:
        system = hub.auth.get_user(SYSTEM_MONIKER)
        assert system["system"] is True and hub.auth.get_user(alice["guid"])["system"] is False
        [job] = built_in(hub)
        assert hub.auth.login("Alice", "a password")["guid"] == alice["guid"]   # the person is as before
    finally:
        hub.close()
    hub = Hub(tmp_path / "hub", clock=Clock(START + 2 * HOUR))
    try:
        assert [u["guid"] for u in users(hub) if u["system"]] == [system["guid"]]
        assert [j["guid"] for j in built_in(hub)] == [job["guid"]]
    finally:
        hub.close()


def test_an_older_hub_with_a_person_called_system_keeps_them(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock(START))
    hub.close()
    older_core(tmp_path / "hub")
    db = sqlite3.connect(tmp_path / "hub" / "hub.db")
    with db:                                          # a person called "System", made by the older core
        db.execute("INSERT INTO monikers VALUES ('brave-quiet-otter', 'p-guid')")
        db.execute("INSERT INTO users (guid, moniker, name, name_key, password, created_at) "
                   "VALUES ('p-guid', 'brave-quiet-otter', 'System', 'system', ?, ?)", (_hash_password("pw"), START))
    db.close()

    hub = Hub(tmp_path / "hub", clock=Clock(START + HOUR))
    try:
        assert hub.auth.login("System", "pw")["guid"] == "p-guid"     # they log in as before
        assert hub.auth.get_user(SYSTEM_MONIKER)["system"] is True and built_in(hub)
    finally:
        hub.close()


def test_login_as_the_system_is_refused_and_a_person_cannot_take_its_name(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock(START))
    try:
        for name, password in (("system", ""), ("System", "anything"), ("SYSTEM", "x")):
            with pytest.raises(HubError) as refused:
                hub.auth.login(name, password)
            assert refused.value.message["code"] == "system_owner"
        with hub._lock:
            logins = [json.loads(r[0]) for r in hub._db.execute(
                "SELECT result FROM journal WHERE kind = 'auth' AND user = ?", (hub.auth.system,))]
        assert logins == [{"login": "system"}] * 3                  # journaled under it (L1)

        for name in ("system", "System", "ＳＹＳＴＥＭ"):            # NFKC and case, as login compares names
            with pytest.raises(HubError) as taken:
                hub.auth.create_user(name, "a password", first=name == "system")
            assert taken.value.message["code"] == "name_taken"
        assert not hub.auth.has_users()
    finally:
        hub.close()


def test_the_job_fires_at_12_hours_runs_as_the_system_and_rolls_up(tmp_path):
    clock = Clock(START + 60)
    hub = Hub(tmp_path / "hub", clock=clock)
    alice = hub.auth.create_user("Alice", "a password")
    box = Box(hub, alice["guid"], "box")
    [job] = built_in(hub)

    def finished():
        return [e for e in journal.read(hub, kind="job", job=job["moniker"]) if e["result"]["type"] != "running"]

    async def scenario():
        hub.events.start()
        try:
            for i in range(8):                             # two minutes of heartbeats, from START + 60
                clock.now = START + 60 + 15 * i
                box.beat({"cpu": 10 + i})
            clock.now = START + 12 * HOUR - 1              # a recent one, which stays raw
            box.beat({"cpu": 50})
            hub.events.tick()
            await asyncio.sleep(0.2)
            assert finished() == []                        # not yet 12 hours

            clock.now = START + 12 * HOUR + 600            # the 12-hour mark is passed
            hub.events.tick()
            return await eventually(finished)
        finally:
            await hub.events.stop()

    try:
        [run] = asyncio.run(scenario())
        assert run["user"] == SYSTEM_MONIKER and run["token"] is None   # the system is the actor (L1)
        assert run["cap"] == "heartbeat.roll-up" and run["worker"] == "hub"
        assert run["result"]["type"] == "result" and run["result"]["result"]["rolled_up"] == 8
        with hub._lock:
            raw = [r[0] for r in hub._db.execute("SELECT at FROM heartbeats ORDER BY at")]
            minutes = hub._db.execute("SELECT minute, count, cpu_min, cpu_max FROM heartbeat_minutes "
                                      "ORDER BY minute").fetchall()
            success = hub._db.execute("SELECT user, result FROM journal WHERE kind = 'event' AND "
                                      "json_extract(result, '$.event') = 'job.success'").fetchone()
        assert raw == [START + 12 * HOUR - 1]
        assert [tuple(m) for m in minutes] == [((START + 60) // 60, 4, 10, 13), ((START + 120) // 60, 4, 14, 17)]
        assert success["user"] == hub.auth.system                       # its completion, too
        assert history.RAW_KEPT == 12 * HOUR
    finally:
        hub.close()


def test_the_jobs_page_journal_and_door_show_the_system(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock(START))
    try:
        alice = hub.auth.create_user("Alice", "a password")
        [job] = site.jobs_view(hub)["jobs"]
        assert job["owner"] == {"moniker": SYSTEM_MONIKER, "name": SYSTEM_NAME}
        assert job["title"] == "Heartbeat roll-up" and job["call"]["cap"] == "heartbeat.roll-up"
        assert {"name": SYSTEM_NAME, "moniker": SYSTEM_MONIKER} in site.journal_choices(hub)["users"]
        [listed] = call(hub, "job.list", {}, alice["guid"])["jobs"]      # what an agent sees at the door
        assert listed["owner"] == SYSTEM_MONIKER

        # A job like any other: a person pauses it, and it stays paused, and not made again, at the next start.
        call(hub, "job.pause", {"job": job["moniker"]}, alice["guid"])
    finally:
        hub.close()
    hub = Hub(tmp_path / "hub", clock=Clock(START + HOUR))
    try:
        [again] = built_in(hub)
        assert again["moniker"] == job["moniker"] and again["paused"]
    finally:
        hub.close()
