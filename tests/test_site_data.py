"""The Data page (P4: Journal, Records; L1, L2, L5, K1, K3, K4, J5) and devices on the
Workers page (P4, P7, D1, D4, D6, D8), driven through real HTTP on localhost, as the
browser drives them.

The sample hub: two users; workers desk and nas on band Home; a direct lamp device
on Home with its status polled, and a tv device pinned to desk; calls, a stream
recorded and closed, events, a chain of two jobs, logins; notes linked to each other
and to a blob, and a second blob nobody links."""

import asyncio
import json
import os
import pathlib
from urllib.parse import quote

import yarl

from helpers import Clock, eventually
from rook_hub import Hub, bands, devices, journal, records
from test_devices import Lamp
from test_site import COOKIE, HOME, Box, ask, browser, login

PLUGINS = pathlib.Path(__file__).parent / "plugins"
SECRET = "hunter2-deploy-key-77"          # the value the site redacts
MANUAL, PHOTO = b"%PDF-1.4 the lamp manual\x00\xff", b"\x89PNG\r\n\x1a\nnot really a photo"


def test_site_data(tmp_path):
    asyncio.run(scenario(tmp_path))


def kinds(entries):
    return {e["kind"] for e in entries}


async def scenario(tmp_path):
    clock = Clock()
    hub = Hub(tmp_path / "hub", clock=clock, vault_key=os.urandom(32))
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    bob = hub.auth.create_user("Bob", "battery staple")
    a = {"user": alice["guid"], "token": None}   # an actor, as hub caps and plugins take it
    lamp = Lamp()
    boxes = {name: Box(hub, name, alice["guid"]) for name in ("desk", "nas")}
    try:
        home = bands.create(hub, "Home", HOME, user=alice["guid"])
        desk = boxes["desk"]
        for name in ("lamp", "notes"):
            hub.plugins.install(PLUGINS / name, a)
        [added] = (await hub.call("hub", "device.add", {"name": "desk lamp", "driver": "lamp", "route": "direct",
                                                       "band": home["moniker"], "interval": 60,
                                                       "config": {"url": lamp.url}}, "add the lamp", **a))["result"].values()
        [tv] = (await hub.call("hub", "device.add", {"name": "tv", "driver": "lamp", "route": "pinned",
                                                    "via": desk.moniker, "band": home["moniker"],
                                                    "config": {"url": lamp.url}}, "add the tv", **a))["result"].values()
        clock.advance(60)                          # the lamp's status poll comes due, once (D6)
        [due] = devices.due(hub, clock())
        await devices.poll(hub, due)
        for box in boxes.values():                 # from here the clock moves less than three heartbeats (H3)
            await box.connect(HOME)
        clock.advance(10)
        early = clock() - 1

        # Calls, one with the secret in its why, and a stream that records it and is closed.
        opened = await hub.call(desk.guid, "shell.open", {}, f"deploy with {SECRET}", **a)
        stream = opened["stream"]
        await hub.streams.write(stream, f"export KEY={SECRET}\n", **a)
        await hub.streams.close(stream, "the deploy is done", **a)
        assert (await hub.call(added["moniker"], "lamp.on", {}, "light the desk", **a))["type"] == "result"
        clock.advance(10)

        # A chain of two jobs (J3, J5): the door opens, the lamp goes on, then off.
        first = (await hub.call("hub", "job.create", {"title": "door light", "match": {"event": "door.open"},
                                                      "call": {"worker": added["moniker"], "cap": "lamp.on"}},
                                "light up at the door", **a))["result"]["job"]
        second = (await hub.call("hub", "job.create", {"title": "lights out",
                                                       "match": {"event": "job.success", "payload": {"job": first["guid"]}},
                                                       "call": {"worker": added["moniker"], "cap": "lamp.off"}},
                                 "then turn it off", **a))["result"]["job"]
        hub.events.emit("door.open", {"door": "front"}, **a)
        await eventually(lambda: len(journal.read(hub, kind="event", text="job.success")) == 2)
        clock.advance(10)
        middle = clock() - 1
        notes = hub.plugins.call("note.create", {"title": "Lamp setup", "text": "the lamp lives on the desk"}, a)
        other = hub.plugins.call("note.create", {"title": "Desk", "text": "the desk is by the window"}, a)
        manual = records.store_blob(hub, MANUAL, "application/pdf", "lamp-manual.pdf", "the vendor manual", **a)
        photo = records.store_blob(hub, PHOTO, "image/png", "stray.png", **a)
        records.link(hub, notes["guid"], manual["guid"])
        records.link(hub, other["guid"], notes["guid"])
        agent = hub.auth.mint("agent", user=alice["guid"])

        async with browser() as client:
            # 1. Without a session every request of the Data page, and the opt-out, is refused (T4).
            requests = [("GET", "/api/journal", None), ("GET", "/api/journal/choices", None),
                        ("POST", "/api/redact", {"value": SECRET}), ("GET", "/api/records", None),
                        ("GET", "/api/record-types", None), ("GET", "/api/unlinked-blobs", None),
                        ("GET", f"/api/records/{notes['note']}", None),
                        ("GET", f"/api/records/{manual['moniker']}/bytes", None),
                        ("POST", f"/api/workers/{desk.moniker}/serves-devices", {"serves": False})]
            for method, path, body in requests:
                status, reply = await ask(client, method, base + path, body)
                assert status == 401 and reply["code"] == "no_session", (method, path)
            async with browser() as other_client:          # an agent's token is not a session
                other_client.cookie_jar.update_cookies({COOKIE: agent})
                for method, path, body in requests:
                    status, reply = await ask(other_client, method, base + path, body)
                    assert status == 401 and reply["code"] == "token_scope", (method, path)
            assert devices.opted_out(hub) == set() and journal.read(hub, cap="journal.redact") == []

            assert (await login(client, base, "Bob", "battery staple"))[0] == 200
            await client.post(base + "/api/logout")
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            session = hub.auth.verify(client.cookie_jar.filter_cookies(yarl.URL(base))[COOKIE].value)

            # 2. The Data page is installed in the Hub segment, after Jobs, in P4's order (P2, P4).
            status, reply = await ask(client, "GET", base + "/api/session")
            assert [p["id"] for p in reply["nav"][0]["pages"]] == ["workers", "auth", "secrets", "jobs", "data", "settings"]
            assert reply["nav"][0]["pages"][4] == {"id": "data", "label": "Data", "path": "/data",
                                                   "script": "/site/pages/data.js"}
            for path in ("/data", "/site/pages/data.js"):
                async with client.get(base + path) as response:
                    assert response.status == 200

            # 3. The journal, newest first, with every kind (L1): who, worker or device, cap, why, result.
            status, reply = await ask(client, "GET", base + "/api/journal?limit=1000")
            everything = reply["entries"]
            assert status == 200 and [e["id"] for e in everything] == sorted((e["id"] for e in everything), reverse=True)
            assert kinds(everything) == {"call", "auth", "band", "event", "job", "stream"}
            assert everything[-1]["id"] == 1
            lamp_call = next(e for e in everything if e["why"] == "light the desk")
            assert (lamp_call["kind"], lamp_call["worker"], lamp_call["cap"], lamp_call["user"]) == (
                "call", added["moniker"], "lamp.on", alice["moniker"])
            assert lamp_call["result"]["type"] == "result"

            # 4. Each filter narrows (L2).
            async def entries(query):
                status, reply = await ask(client, "GET", f"{base}/api/journal?{query}")
                assert status == 200, (query, reply)
                return reply["entries"]
            assert kinds(await entries("kind=call")) == {"call"}
            assert {e["worker"] for e in await entries(f"worker={desk.moniker}")} == {desk.moniker}
            on_lamp = await entries(f"worker={added['moniker']}")     # a device, by its moniker (D1)
            assert on_lamp and {e["worker"] for e in on_lamp} == {added["moniker"]}
            assert {e["kind"] for e in on_lamp} == {"call", "job"}
            assert {e["cap"] for e in await entries("cap=shell.open")} == {"shell.open"}
            bobs = await entries(f"user={bob['moniker']}")
            assert bobs and {e["user"] for e in bobs} == {bob["moniker"]} and kinds(bobs) == {"auth"}
            assert [e["why"] for e in await entries("text=LIGHT+THE+DESK")] == ["light the desk"]
            def iso(at):
                return quote(journal.iso(at))
            since = await entries(f"since={iso(middle)}")
            assert since and all(e["at"] >= journal.iso(middle) for e in since) and len(since) < len(everything)
            until = await entries(f"until={iso(early)}")
            assert until and all(e["at"] < journal.iso(early) for e in until) and len(until) < len(everything)
            ran = await entries(f"job={first['moniker']}")
            assert ran and {e["job"] for e in ran} == {first["moniker"]}
            run = ran[0]["run"]
            chain = await entries(f"run={run}")
            assert {e["job"] for e in chain} == {first["moniker"], second["moniker"]}
            assert sorted(e["kind"] for e in chain) == ["event", "event", "job", "job"]     # J5: one run ID
            assert len(await entries(f"kind=job&run={run}&cap=lamp.off")) == 1
            for bad in ("limit=0", "limit=x", "since=yesterday", "worker=nobody-at-all", "user=nobody-at-all"):
                status, reply = await ask(client, "GET", f"{base}/api/journal?{bad}")
                assert status == 400 and reply["next"], bad

            # Reading further back reaches the oldest entry.
            assert len(await entries("limit=3")) == 3
            assert [e["id"] for e in await entries(f"limit={len(everything)}")][-1] == 1

            # What the filters offer: kinds, users, workers and devices, caps, jobs.
            status, choices = await ask(client, "GET", base + "/api/journal/choices")
            assert choices["kinds"] == ["call", "auth", "band", "event", "job", "stream"]
            assert {u["name"] for u in choices["users"]} == {"Alice", "Bob"}
            assert {(w["moniker"], w["device"]) for w in choices["workers"]} >= {
                ("hub", False), (desk.moniker, False), (added["moniker"], True), (tv["moniker"], True)}
            assert {"shell.exec", "lamp.on", "journal.redact", "note.create"} <= set(choices["caps"])
            assert choices["jobs"] == [{"title": "door light", "moniker": first["moniker"]},
                                       {"title": "lights out", "moniker": second["moniker"]}]

            # 5. Redact through the site (L5): a journal.redact call by the user; the value is gone
            #    from the journal and the stream's recording, and is in neither the reply nor the journal.
            def raw_journal():
                return " ".join(f"{r[0]} {r[1]}" for r in hub._db.execute("SELECT why, result FROM journal"))
            assert SECRET in raw_journal()
            assert any(SECRET.encode() in r[0] for r in hub._db.execute("SELECT data FROM stream_data"))
            async with client.post(base + "/api/redact", json={"value": SECRET}) as response:
                text = await response.text()
                assert response.status == 200 and SECRET not in text
                assert json.loads(text) == {"redacted": 1, "recordings": 1}
            assert SECRET not in raw_journal()
            assert not any(SECRET.encode() in r[0] for r in hub._db.execute("SELECT data FROM stream_data"))
            assert any(b"(redacted)" in r[0] for r in hub._db.execute("SELECT data FROM stream_data"))
            [done] = journal.read(hub, cap="journal.redact")
            assert (done["kind"], done["user"], done["token"], done["worker"]) == (
                "call", alice["moniker"], session["moniker"], "hub")
            assert done["result"]["result"] == {"redacted": 1, "recordings": 1}
            assert (await entries("text=deploy+with"))[0]["why"] == "deploy with (redacted)"
            for bad in ({}, {"value": ""}, {"value": 7}):
                status, reply = await ask(client, "POST", base + "/api/redact", bad)
                assert status == 400 and reply["code"] == "bad_message"

            # 6. Records (K1-K4): types with their plugin, browse by type, search, open, links both ways.
            status, reply = await ask(client, "GET", base + "/api/record-types")
            assert reply["types"] == [{"type": "blob", "plugin": "core", "count": 2},
                                      {"type": "note", "plugin": "notes", "count": 2}]
            status, reply = await ask(client, "GET", base + "/api/records?type=note")
            assert [r["title"] for r in reply["records"]] == ["Desk", "Lamp setup"]
            note = reply["records"][1]
            assert note["owner"] == note["creator"] == {"name": "Alice", "moniker": alice["moniker"]}
            assert note["body"] == {"text": "the lamp lives on the desk"} and note["created_at"] and note["updated_at"]
            assert note["creator_token"] is None
            status, reply = await ask(client, "GET", base + "/api/records?text=window")
            assert [r["title"] for r in reply["records"]] == ["Desk"]
            status, reply = await ask(client, "GET", base + "/api/records?type=blob&text=vendor")
            assert [r["moniker"] for r in reply["records"]] == [manual["moniker"]]
            status, reply = await ask(client, "GET", base + "/api/records?limit=1")
            assert len(reply["records"]) == 1
            status, reply = await ask(client, "GET", f"{base}/api/records/{notes['note']}")
            assert reply["record"]["title"] == "Lamp setup" and reply["record"]["type"] == "note"
            assert [r["moniker"] for r in reply["links"]["to"]] == [manual["moniker"]]
            assert [r["moniker"] for r in reply["links"]["from"]] == [other["note"]]
            status, reply = await ask(client, "GET", f"{base}/api/records/{manual['moniker']}")
            assert reply["record"]["body"] == {"hash": manual["body"]["hash"], "size": len(MANUAL),
                                               "mime": "application/pdf"}
            assert [r["moniker"] for r in reply["links"]["from"]] == [notes["note"]] and reply["links"]["to"] == []
            status, reply = await ask(client, "GET", base + "/api/unlinked-blobs")
            assert [r["moniker"] for r in reply["records"]] == [photo["moniker"]]
            assert records.get(hub, photo["guid"])                    # shown, never deleted (K4)

            # A blob downloads byte for byte, as a download and never as a page of the site.
            async with client.get(f"{base}/api/records/{manual['moniker']}/bytes") as response:
                assert response.status == 200 and await response.read() == MANUAL
                assert response.headers["Content-Type"] == "application/pdf"
                assert response.headers["Content-Disposition"] == 'attachment; filename="lamp-manual.pdf"'
                assert response.headers["X-Content-Type-Options"] == "nosniff"
            async with client.get(f"{base}/api/records/{photo['moniker']}/bytes") as response:
                assert await response.read() == PHOTO
            status, reply = await ask(client, "GET", f"{base}/api/records/{notes['note']}/bytes")
            assert status == 400 and reply["code"] == "not_a_blob"
            status, reply = await ask(client, "GET", f"{base}/api/records/no-such-record")
            assert status == 400 and reply["code"] == "unknown_record" and reply["next"]
            assert records.search(hub, type="note") and len(records.search(hub)) == 4    # nothing changed (K2)

            # 7. Devices on the Workers page, in their band, with route, reachability and status (P4, P7, D1, D4, D6).
            status, view = await ask(client, "GET", base + "/api/workers")
            assert view["stats"] == {"online": 2, "pending": 0, "devices": 2}
            [home_group, roaming] = view["groups"]
            assert home_group["band"]["name"] == "Home" and roaming["band"] is None and roaming["devices"] == []
            assert [w["name"] for w in home_group["workers"]] == ["desk", "nas"]
            shown = {d["name"]: d for d in home_group["devices"]}
            assert set(shown) == {"desk lamp", "tv"}
            assert shown["desk lamp"]["device"] is True and shown["desk lamp"]["driver"] == "lamp"
            assert shown["desk lamp"]["route"] == {"kind": "direct"} and shown["desk lamp"]["interval"] == 60
            assert shown["desk lamp"]["reachable"] is True and shown["desk lamp"]["status"] == {"on": False}
            assert shown["desk lamp"]["polled_at"] and shown["desk lamp"]["owner"]["name"] == "Alice"
            assert {c["name"]: c["description"] for c in shown["desk lamp"]["caps"]} == {
                "lamp.on": "Turn the lamp on.", "lamp.off": "Turn the lamp off."}
            assert shown["tv"]["route"] == {"kind": "pinned", "via": desk.moniker}
            assert shown["tv"]["reachable"] is None and shown["tv"]["status"] is None
            assert all(w["serves_devices"] for w in home_group["workers"])

            # 8. The opt-out, on the worker's row (D8): off and on again, each journaled under the user.
            url = f"{base}/api/workers/{desk.moniker}/serves-devices"
            assert await ask(client, "POST", url, {"serves": False}) == (200, {"serves_devices": False})
            assert devices.opted_out(hub) == {desk.guid}
            status, view = await ask(client, "GET", base + "/api/workers")
            assert [w["serves_devices"] for w in view["groups"][0]["workers"]] == [False, True]
            assert await ask(client, "POST", url, {"serves": True}) == (200, {"serves_devices": True})
            assert devices.opted_out(hub) == set()
            changes = journal.read(hub, kind="auth", worker=desk.moniker)[:2]
            assert [(e["result"], e["user"], e["token"]) for e in changes] == [
                ({"serves_devices": True}, alice["moniker"], session["moniker"]),
                ({"serves_devices": False}, alice["moniker"], session["moniker"])]
            for bad in ({}, {"serves": "no"}):
                status, reply = await ask(client, "POST", url, bad)
                assert status == 400 and reply["code"] == "bad_message"
            status, reply = await ask(client, "POST", f"{base}/api/workers/nobody-at-all/serves-devices", {"serves": False})
            assert status == 400 and reply["code"] == "unknown_worker"
    finally:
        for box in boxes.values():
            if hasattr(box, "link"):
                await box.stop()
        await hub.stop_serving()
        hub.close()
        lamp.server.shutdown()
