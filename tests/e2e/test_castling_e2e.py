"""Castling 0.5 as real processes: hub, worker and relay on localhost over
the real Telesthete transport (see conftest.py and docs/castling-e2e.md)."""
from __future__ import annotations

import asyncio
import json
import secrets
import time

from rook.core2 import journal
from rook.core2.transport import TelestheteLink, Transport

from harness import WHY, tv, until


def on_band(band, fn, seconds: float = 20.0):
    """Run ``await fn(transport)`` as one more node on the band (any PSK
    holder can be one: a node is not a hub because it says so)."""
    async def main():
        t = Transport(secrets.token_hex(16), [TelestheteLink(band.psk, relay=band.relay_addr)])
        await t.start(lambda b, m: asyncio.sleep(0))
        while not t.connected(band.band):
            await asyncio.sleep(0.02)
        await t.send(band.band, {"kind": "e2e-hello"})      # the relay learns where we are
        try:
            return await asyncio.wait_for(fn(t), seconds)
        finally:
            await t.close()
    return asyncio.run(main())


def chain_holds(band, chain: str = "general") -> list:
    entries = band.owner.get(f"/journal?chain={chain}&limit=500")["entries"][::-1]
    assert entries and entries[0]["prev"] == journal.GENESIS
    assert journal.first_break(entries) is None
    return entries


# -- 2. announce, listing, offline ------------------------------------------------------

def test_the_hub_lists_what_the_worker_announces(band):
    workers = {w["name"]: w for w in band.owner.get("/workers")["workers"]}
    w = workers["e2e-worker"]
    assert w["id"] == band.worker_id and w["online"] and w["core"] == "2.0" and w["problem"] is None
    assert set(w["plugins"]) == {"core", "e2e"} and w["facts"]["os"] == "linux"
    caps = band.caps()["caps"]
    assert caps["e2e.wipe"]["effects"] == ["destructive"] and caps["e2e.admin"]["risk"] == "admin"
    assert {"core.manifests", "core.trust.set", "core.plugin.install"} <= set(caps)
    # the hub fetched the e2e manifest from the worker (it isn't in its library)
    assert band.worker_calls("core.manifests") >= 1


def test_caps_stay_known_while_the_worker_is_offline_and_across_hub_restarts(fresh):
    b = fresh
    b.start_hub()
    b.start_worker()
    b.wait_caps("e2e.ping")
    assert b.owner.call("e2e.ping", {"text": "before"})["ok"]
    assert b.worker.stop() == 0
    assert "e2e.ping" in b.caps()["caps"]          # gone from the band, not from the hub
    assert b.hub.stop() == 0
    b.start_hub()
    w = next(w for w in b.owner.get("/workers")["workers"] if w["name"] == "e2e-worker")
    assert not w["online"] and set(w["plugins"]) == {"core", "e2e"}
    offline = b.caps()
    assert not offline["online"] and "e2e.ping" in offline["caps"]
    b.start_worker()
    until(lambda: b.caps().get("online"), 30, "the worker back online")
    after = b.owner.call("e2e.ping", {"text": "after"})
    assert after["ok"] and after["result"]["pid"] == b.worker.pid
    # the general chain runs on across the restart
    calls = [e for e in chain_holds(b) if e.get("cap") == "e2e.ping"]
    assert [e["args"]["text"] for e in calls] == ["before", "after"]


# -- 3. a call, end to end ------------------------------------------------------------------

def test_a_door_call_is_ticketed_verified_on_the_worker_and_journaled(band):
    out = band.owner.call("e2e.ping", {"text": "hello"})
    assert out["ok"], out
    assert out["result"] == {"text": "hello", "person": "person:bake", "agent": None, "pid": band.worker.pid}
    # the worker checked the ticket and ran it
    ran = until(lambda: [r for r in band.worker.records() if r.get("msg") == "call" and r.get("cap") == "e2e.ping"],
                10, "the worker's call log")
    assert ran[-1]["ok"] is True
    [entry] = band.owner.get(f"/journal?call={out['id']}")["entries"]
    assert entry["decision"] == "allow" and entry["ok"] is True and entry["why"] == WHY
    assert entry["worker"] == band.worker_id and entry["person"] == "person:bake" and len(entry["ticket"]) == 1
    assert entry["chain"] == "general" and entry["hash"] == journal.entry_hash(entry)
    entries = chain_holds(band)
    assert entries[-1]["call"] == out["id"]


# -- 4. refusals ----------------------------------------------------------------------------

def test_a_call_without_a_why_is_refused_before_any_ticket(band):
    for why in (None, "", "   "):
        out = band.owner.call("e2e.ping", why=why)
        assert out == {"id": out["id"], "ok": False, "error": "denied: why required"}, out
    entries = band.owner.get("/journal?cap=e2e.ping")["entries"]
    assert len(entries) == 3 and all(e["decision"] == "deny" and "ticket" not in e for e in entries)
    assert band.worker_calls("e2e.ping") == 0


def test_a_child_stays_under_its_preset_ceiling(band):
    over = band.kid.call("e2e.admin")
    assert not over["ok"] and over["error"].startswith("denied: person: "), over
    # a child's first use of a cap waits for an owner (approve_new), then runs as the child
    first = band.kid.call("e2e.ping", {"text": "may I"})
    assert first["held"] and "new cap" in first["error"], first
    mine = band.kid.post(f"/holds/{first['hold']}", {"decision": "approve"})
    assert not mine["ok"] and "may not release" in mine["error"]
    ok = band.owner.post(f"/holds/{first['hold']}", {"decision": "approve"})
    assert ok["ok"] and ok["result"]["person"] == "person:kid", ok
    again = band.kid.call("e2e.ping", {"text": "again"})
    assert again["ok"] and again["result"]["text"] == "again"
    # and no door gets a child past its preset
    assert not band.kid.post("/plugins/push", {"worker": "e2e-worker", "plugin": "tv", "why": WHY})["ok"]


def test_a_replayed_ticket_is_refused_by_the_worker(band):
    """Another node on the band copies a request the hub sent, ticket and
    all, and sends it again: the worker has seen that nonce."""
    seen: list = []

    async def sniff_and_replay(t):
        # a raw link sees every frame the relay forwards, not only ours
        spy = TelestheteLink(band.psk, relay=band.relay_addr)

        async def got(payload):
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            if isinstance(msg, dict) and msg.get("cap") == "e2e.ping":
                seen.append(msg)

        async def connected():
            await spy.send(json.dumps({"kind": "e2e-hello"}).encode())
        await spy.start(got, connected)
        try:
            out = await asyncio.to_thread(band.owner.call, "e2e.ping", {"text": "once"})
            assert out["ok"], out
            await asyncio.to_thread(until, lambda: seen, 10, "the hub's request on the band")
            req = {k: v for k, v in seen[0].items() if k in ("id", "cap", "target", "args", "ticket")}
            return await t.request(band.band, req, 10)
        finally:
            await spy.stop()

    replayed = on_band(band, sniff_and_replay)
    assert replayed["ok"] is False and replayed["error"] == "denied by worker: replay", replayed
    assert [r["text"] for r in [e["args"] for e in band.owner.get("/journal?cap=e2e.ping")["entries"]]] == ["once"]


def test_the_worker_ceiling_holds_even_when_the_hub_grants_more(band):
    """The home hub narrows its own ceiling on the worker (a signed
    membership through core.trust.set). The hub then refuses what's outside
    it, and the worker refuses a validly signed hub ticket for it too."""
    now = int(time.time())
    narrowed = band.owner.call("core.trust.set",
                               {"membership": band.membership("home", {"grants": ["e2e.ping"]}, now)})
    assert narrowed["ok"], narrowed
    # the announce carries the new ceiling; the hub's dispatch applies it
    via_hub = until(lambda: (r := band.owner.call("e2e.admin")) and not r["ok"] and r, 20, "the narrowed announce")
    assert via_hub["error"].startswith("denied: ceiling: "), via_hub
    assert band.owner.call("e2e.ping", {"text": "still"})["ok"]

    async def straight_to_the_worker(t):
        cid = secrets.token_hex(16)
        req = {"id": cid, "cap": "e2e.admin", "target": band.worker_id, "args": {},
               "ticket": band.ticket("e2e.admin", {}, cid)}
        return await t.request(band.band, req, 10)
    direct = on_band(band, straight_to_the_worker)
    assert direct["ok"] is False and direct["error"] == "denied by worker: outside_ceiling", direct

    # the home hub widens it again; core.trust.set is never outside a home ceiling
    widened = band.owner.call("core.trust.set", {"membership": band.membership("home", {"grants": ["*"]}, now + 1)})
    assert widened["ok"], widened
    band.wait_caps("e2e.admin")
    assert band.owner.call("e2e.admin")["ok"]


def test_a_destructive_cap_is_held_for_the_person(band):
    held = band.owner.call("e2e.wipe")
    assert held["held"] and not held["ok"] and held["error"] == "held: destructive", held
    holds = band.owner.get("/holds")["holds"]
    assert [(h["hold"], h["cap"], h["worker"], h["reasons"]) for h in holds] == \
        [(held["hold"], "e2e.wipe", "e2e-worker", ["destructive"])]
    assert band.worker_calls("e2e.wipe") == 0       # nothing reached it
    ran = band.owner.post(f"/holds/{held['hold']}", {"decision": "approve"})
    assert ran["ok"] and ran["result"] == {"wiped": True}, ran
    assert band.owner.get("/holds")["holds"] == []
    entries = band.owner.get("/journal?cap=e2e.wipe")["entries"]
    assert [e["decision"] for e in entries] == ["released", "held"] and entries[0]["by"] == "person:bake"
    refused = band.owner.call("e2e.wipe")
    assert band.owner.post(f"/holds/{refused['hold']}", {"decision": "refuse"}) == {"ok": True}
    assert band.owner.get("/holds")["holds"] == []
    assert band.worker_calls("e2e.wipe") == 1       # the refused one never ran
    chain_holds(band)


# -- 5 and 6. plugins pushed from the hub library, hot reload, rollback -------------------------

def push(band, plugin="tv", door=None):
    return (door or band.owner).post("/plugins/push", {"worker": "e2e-worker", "plugin": plugin, "why": WHY})


def tv_ping(band):
    out = band.owner.call("tv.ping")
    assert out["ok"], out
    return out["result"]


def test_plugins_install_from_the_library_check_roll_back_and_hot_reload(band):
    lib = band.library / "tv"
    pid = band.worker.pid
    fetches = band.worker_calls("core.manifests")

    tv(lib, "1.0.0")
    first = push(band)
    assert first["ok"] and first["result"]["version"] == "1.0.0" and first["result"]["previous"] is None, first
    band.wait_caps("tv.ping")
    assert tv_ping(band) == {"version": "1.0.0", "pid": pid}

    # a new version replaces the running one in the same process
    tv(lib, "1.1.0")
    second = push(band)
    assert second["ok"] and second["result"]["previous"] == "1.0.0", second
    until(lambda: tv_ping(band)["version"] == "1.1.0", 10, "1.1.0 to answer")
    assert tv_ping(band)["pid"] == pid

    # a version whose checks fail is rolled back automatically
    tv(lib, "1.2.0", broken=True)
    broken = push(band)
    assert not broken["ok"] and broken["error"].startswith("install failed: ") and \
        broken["error"].endswith("(rolled back)"), broken
    assert tv_ping(band) == {"version": "1.1.0", "pid": pid}

    # and a person can go back a version by hand
    back = band.owner.call("core.plugin.rollback", {"plugin": "tv"})
    assert back["ok"] and back["result"]["version"] == "1.0.0", back
    assert tv_ping(band) == {"version": "1.0.0", "pid": pid}
    assert sum(r.get("msg") == "worker started" for r in band.worker.records()) == 1
    # the hub knew each manifest it pushed: it never had to fetch one back
    assert band.worker_calls("core.manifests") == fetches

    installs = band.owner.get("/journal?cap=core.plugin.install")["entries"]
    assert [e["ok"] for e in installs] == [False, True, True] and all(e["why"] == WHY for e in installs)
    chain_holds(band)

    # installed plugins load again at start-up, re-checked against plugin.sig
    assert band.worker.stop() == 0
    band.start_worker()
    until(lambda: band.caps().get("online") and "tv.ping" in band.caps()["caps"], 30, "tv after a restart")
    assert tv_ping(band) == {"version": "1.0.0", "pid": band.worker.pid}


def test_only_library_plugins_and_owners_push(band):
    assert push(band, "nope")["error"] == "nope is not in the hub's library"
    tv(band.library / "tv", "1.0.0")
    assert push(band, door=band.kid)["error"] == "denied: only an owner pushes plugins"
    no_why = band.owner.post("/plugins/push", {"worker": "e2e-worker", "plugin": "tv"})
    assert no_why["error"] == "denied: why required", no_why
    assert "tv.ping" not in band.caps()["caps"]
