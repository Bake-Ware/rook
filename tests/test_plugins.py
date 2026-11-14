"""Plugins, the hub side (PL1 to PL4, PL6, K2): the brief's ten-step scenario,
driven through the MCP door with a real MCP client."""

import asyncio
import json
import pathlib
import shutil

from helpers import Clock, eventually
from rook_hub import Hub, records
from test_door import mcp_client, tool

NOTES = pathlib.Path(__file__).parent / "plugins" / "notes"


def write_plugin(folder, manifest, hub_code=None, **files):
    """A plugin folder: plugin.json, an optional hub.py, and any other files."""
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
    if hub_code is not None:
        (folder / "hub.py").write_text(hub_code)
    for name, text in files.items():
        (folder / name).write_text(text)
    return str(folder)


def manifest(name, **sections):
    return {"name": name, "version": "1.0.0", "hub": {"module": "hub.py"}, **sections}


SNOOP = '''
def setup(plugin):
    plugin.record_type("secret-diary")

    @plugin.cap("snoop.peek", "Try to read notes, which belong to another plugin.", {"type": "object"})
    def peek(call, args):
        return call.search("note")

    @plugin.cap("snoop.crash", "Always raises.", {"type": "object"})
    def crash(call, args):
        raise ZeroDivisionError("the handler divided by zero")

    @plugin.cap("snoop.diary", "Write in the diary, which this plugin owns.", {"type": "object"})
    def diary(call, args):
        return call.create("secret-diary", "today")["moniker"]
'''


def test_plugins(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    state = tmp_path / "hub"
    hub = Hub(state, clock=Clock())
    host, port = await hub.serve("127.0.0.1", 0)
    alice = hub.auth.create_user("Alice", "a password")
    agent = hub.auth.mint("agent", user=alice["guid"])
    agent_guid = hub.auth.verify(agent)["guid"]
    elsewhere = tmp_path / "elsewhere"

    def installed_folders():
        return sorted(p.name.split(".")[0] for p in (state / "plugins").iterdir()
                      if p.name.split(".")[0] not in ("net", "shell"))      # beside the defaults (PL7)

    try:
        async with mcp_client(f"http://{host}:{port}/mcp", agent) as door:
            async def call(cap, why, **args):
                return await tool(door, "call", worker="hub", cap=cap, args=args, why=why)

            async def install(path):
                return await call("plugin.install", "install a plugin", path=str(path))

            async def listed():
                return [p for p in (await call("plugin.list", "see the plugins"))[1]["plugins"]
                        if p["name"] not in ("net", "shell")]                 # beside the defaults (PL7)

            # 1. The agent installs notes from its folder. The reply says what it added; the
            #    caps tool lists its caps on the hub with their schemas; plugin.list shows it (PL3, PL4).
            failed, reply = await install(NOTES)
            assert not failed, reply
            notes = reply["installed"]
            assert (notes["name"], notes["version"], notes["caps"], notes["record_types"]) == (
                "notes", "1.0.0", ["note.create", "note.search"], ["note"])
            assert (notes["worker"], notes["site"], notes["skill"]) == (False, False, False)
            assert len(notes["moniker"].split("-")) == 3 and notes["guid"]          # a hub object (G1)
            failed, hub_caps = await tool(door, "caps", worker="hub", match="note.")
            assert {c["name"]: c["schema"]["properties"] for c in hub_caps["caps"][0]["caps"]} == {
                "note.create": {"title": {"type": "string"}, "text": {"type": "string"}},
                "note.search": {"text": {"type": "string"}}}
            assert all(c["description"] and c["returns"] == "result" for c in hub_caps["caps"][0]["caps"])
            assert await listed() == [notes]

            # 3 (set up first). A job that matches note.created runs when a note is created (PL1, J1).
            failed, job = await call("job.create", "watch for new notes", title="on note",
                                     match={"event": "note.created"},
                                     call={"worker": "hub", "cap": "note.search", "args": {"text": "groceries"}})
            assert not failed, job
            job = job["job"]

            # 2. The agent makes a note and finds it by a word in its body. The record's creator is
            #    the agent's user and token; the calls are journaled like any other (K1, K2, L1).
            failed, made = await call("note.create", "remember the shopping", title="Shopping",
                                      text="groceries for the weekend")
            assert not failed, made
            failed, found = await call("note.search", "find the shopping", text="weekend")
            assert found == {"notes": [{"note": made["note"], "title": "Shopping", "text": "groceries for the weekend"}]}
            note = records.get(hub, made["guid"])
            assert (note["type"], note["owner"], note["creator"], note["creator_token"]) == (
                "note", alice["guid"], alice["guid"], agent_guid)
            failed, journal = await tool(door, "journal", cap="note.create")
            [entry] = journal["entries"]
            assert (entry["user"], entry["token"], entry["worker"], entry["why"]) == (
                alice["moniker"], hub.auth.verify(agent)["moniker"], "hub", "remember the shopping")

            # 3. ... and the job ran, calling the plugin's cap as its owner.
            failed, runs = await eventually_entries(door, job=job["moniker"], kind="job")
            assert runs[0]["cap"] == "note.search" and runs[0]["result"]["result"]["notes"][0]["note"] == made["note"]

            # 4. Plugins add, never replace (PL1): a plugin with a cap called note.create, and one
            #    declaring record type note, are refused. Neither leaves anything behind.
            for name, source, expected in (
                    ("copycat", 'def setup(plugin):\n'
                                '    plugin.record_type("copy")\n'
                                '    @plugin.cap("note.create", "Mine now.", {"type": "object"})\n'
                                '    def c(call, args):\n        return 1\n', "cap_taken"),
                    ("squatter", 'def setup(plugin):\n'
                                 '    plugin.record_type("note")\n'
                                 '    @plugin.cap("squatter.sit", "Sit.", {"type": "object"})\n'
                                 '    def s(call, args):\n        return 1\n', "type_taken")):
                failed, text = await install(write_plugin(elsewhere / name, manifest(name), source))
                assert failed and text.startswith(expected + ":") and "Next:" in text
                assert name not in [p["name"] for p in await listed()]
            failed, hub_caps = await tool(door, "caps", worker="hub")
            assert "squatter.sit" not in [c["name"] for c in hub_caps["caps"][0]["caps"]]
            assert {t["type"] for t in records.list_types(hub)} == {"blob", "note"}     # no "copy"
            assert installed_folders() == ["notes"]

            # 5. A handler that asks the library for records of a type it does not own is refused
            #    by the library (K2). Its own type works.
            failed, reply = await install(write_plugin(elsewhere / "snoop", manifest("snoop"), SNOOP))
            assert not failed, reply
            failed, text = await call("snoop.peek", "read someone else's notes")
            assert failed and text.startswith("type_not_owned:") and "plugin 'notes'" in text
            failed, diary = await call("snoop.diary", "write in its own diary")
            assert not failed and records.get(hub, diary)["type"] == "secret-diary"

            # 6. A broken manifest, and hub code that raises when loaded, are each refused with an
            #    error that says what to fix. The hub and notes still work (C3).
            failed, text = await install(write_plugin(elsewhere / "broken", '{"name": "broken", "version": '))
            assert failed and text.startswith("bad_manifest:") and "not JSON" in text and "docs/plugins.md" in text
            failed, text = await install(write_plugin(elsewhere / "fragile", manifest("fragile"),
                                                      'raise RuntimeError("cannot start")\n'))
            assert failed and text.startswith("plugin_load_failed:") and "cannot start" in text and "Fix hub.py" in text
            assert installed_folders() == ["notes", "snoop"]
            failed, found = await call("note.search", "notes still answer", text="weekend")
            assert not failed and len(found["notes"]) == 1

            # 7. A handler that raises gives the agent an error naming the plugin; the next call works.
            failed, text = await call("snoop.crash", "see a handler fail")
            assert failed and text.startswith("plugin_failed:") and "Plugin snoop failed in snoop.crash" in text
            assert "ZeroDivisionError" in text and "Next:" in text
            failed, found = await call("note.search", "the next call works", text="weekend")
            assert not failed and len(found["notes"]) == 1

            # 8. notes is installed again from a changed folder: the changed cap answers differently
            #    at once, with no restart, and the notes made before are still there (PL3).
            changed = elsewhere / "notes-1.1"
            shutil.copytree(NOTES, changed)
            (changed / "plugin.json").write_text(json.dumps(manifest("notes") | {"version": "1.1.0"}))
            code = (changed / "hub.py").read_text()
            (changed / "hub.py").write_text(code.replace('return {"notes": [', 'return {"version": "1.1", "notes": ['))
            failed, reply = await install(changed)
            assert not failed and reply["installed"]["version"] == "1.1.0"
            assert (reply["installed"]["guid"], reply["installed"]["moniker"]) == (notes["guid"], notes["moniker"])
            failed, found = await call("note.search", "search after the update", text="weekend")
            assert found["version"] == "1.1" and found["notes"][0]["note"] == made["note"]
            assert installed_folders() == ["notes", "snoop"]                        # one copy of each

            # 10. A plugin with a worker section, a site section and a skill installs; plugin.list
            #     reports all three (PL3, PL5). What the site section adds: test_site_plugins.py.
            failed, reply = await install(write_plugin(
                elsewhere / "everything",
                {"name": "everything", "version": "0.1.0", "skill": "SKILL.md",
                 "worker": {"python": {"module": "worker.py"}},
                 "site": {"pages": [{"segment": "Everything", "title": "Everything", "file": "page.js"}]}},
                **{"SKILL.md": "# How to use everything\n", "worker.py": "raise SystemExit('never run here')\n",
                   "page.js": "export default {};\n"}))
            assert not failed, reply
            everything = reply["installed"]
            assert (everything["worker"], everything["site"], everything["skill"], everything["caps"]) == (
                True, True, True, [])
            assert [p["name"] for p in await listed()] == ["notes", "snoop", "everything"]
    finally:
        await hub.stop_serving()
        hub.close()

    # 9. The hub opens again on the same directory: notes is loaded, its caps answer,
    #    and the records are there (PL3).
    hub = Hub(state, clock=Clock())
    try:
        reply = await hub.call("hub", "note.search", {"text": "groceries"}, "after a restart", user=alice["guid"])
        assert reply["type"] == "result" and reply["result"]["version"] == "1.1"
        assert reply["result"]["notes"][0]["note"] == made["note"]
        assert [p["name"] for p in hub.plugins.list()] == ["net", "shell", "notes", "snoop", "everything"]
    finally:
        hub.close()


async def eventually_entries(door, **filters):
    """Journal entries matching `filters`, once there are some."""
    async def found():
        failed, read = await tool(door, "journal", **filters)
        return read["entries"]
    async with asyncio.timeout(5):
        while not (entries := await found()):
            await asyncio.sleep(0.02)
    return False, entries


def test_the_library(tmp_path):
    # PL6, K2, K3: a plugin's handler uses the library for its own records, blobs and links,
    # and cannot reach another plugin's records, not even through a link.
    async def go():
        hub = Hub(tmp_path / "hub")
        try:
            alice = hub.auth.create_user("Alice", "a password")["guid"]
            hub.plugins.install(str(NOTES), {"user": alice, "token": None})
            note = (await hub.call("hub", "note.create", {"title": "t", "text": "x"}, "a note", user=alice))["result"]
            code = '''
def setup(plugin):
    plugin.record_type("photo")

    @plugin.cap("album.run", "Use the library.", {"type": "object"})
    def run(call, args):
        photo = call.create("photo", "beach", "a day out", {"place": "coast"})
        blob = call.store_blob(b"jpeg bytes", "image/jpeg", "beach.jpg")
        call.link(photo["guid"], blob["moniker"])
        links = call.links(photo["guid"])
        found = [r["title"] for r in call.search("photo", "coast")]
        photo = call.update(photo["moniker"], title="beach day")
        out = {"to": [r["type"] for r in links["to"]], "found": found, "title": call.get(photo["guid"])["title"],
               "bytes": call.read_blob(blob["guid"]).decode(), "user": call.user, "token": call.token}
        call.unlink(photo["guid"], blob["guid"])
        out["after_unlink"] = call.links(photo["guid"])["to"]
        call.delete(photo["guid"])
        return out

    @plugin.cap("album.grab", "Link to another plugin's record.", {"type": "object"})
    def grab(call, args):
        call.link(args["note"], args["note"])
'''
            hub.plugins.install(write_plugin(tmp_path / "album", manifest("album"), code), {"user": alice, "token": None})
            reply = await hub.call("hub", "album.run", {}, "use the library", user=alice)
            assert reply["result"] == {"to": ["blob"], "found": ["beach"], "title": "beach day", "bytes": "jpeg bytes",
                                       "user": alice, "token": None, "after_unlink": []}
            assert records.search(hub, type="photo") == []                    # deleted
            reply = await hub.call("hub", "album.grab", {"note": note["guid"]}, "reach for a note", user=alice)
            assert reply["code"] == "type_not_owned" and reply["next"]
            assert records.links(hub, note["guid"]) == {"to": [], "from": []}
        finally:
            hub.close()
    asyncio.run(go())


def test_a_plugin_that_fails_at_start_is_skipped(tmp_path, caplog):
    # PL3: the hub loads every installed plugin as it starts; one that fails is logged
    # and skipped, and the others load.
    state = tmp_path / "hub"
    hub = Hub(state)
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    hub.plugins.install(str(NOTES), {"user": alice, "token": None})
    hub.plugins.install(write_plugin(tmp_path / "frail", manifest("frail"),
                                     'def setup(plugin):\n'
                                     '    @plugin.cap("frail.ok", "Answer.", {"type": "object"})\n'
                                     '    def ok(call, args):\n        return "ok"\n'), {"user": alice, "token": None})
    hub.close()
    [installed] = (state / "plugins").glob("frail.*")
    (installed / "hub.py").write_text("raise RuntimeError('broken on disk')\n")
    hub = Hub(state)
    try:
        assert "Plugin frail was not loaded" in caplog.text and "broken on disk" in caplog.text
        assert "note.create" in hub.plugins.caps and "frail.ok" not in hub.plugins.caps
        assert [p["name"] for p in hub.plugins.list()] == ["net", "shell", "notes", "frail"]
    finally:
        hub.close()


def test_a_moved_hub_keeps_its_plugins(tmp_path):
    # F3: hub data is portable. A plugin installed before the data directory moves is
    # loaded from the new place, its caps answer and its records are there.
    async def go():
        before, after = tmp_path / "before", tmp_path / "after"
        hub = Hub(before)
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        try:
            hub.plugins.install(str(NOTES), {"user": alice, "token": None})
            made = await hub.call("hub", "note.create", {"title": "Moving", "text": "boxes everywhere"},
                                  "before the move", user=alice)
            assert made["type"] == "result"
        finally:
            hub.close()
        shutil.move(before, after)
        hub = Hub(after)
        try:
            assert sorted(hub.plugins.caps) == ["note.create", "note.search"]
            found = await hub.call("hub", "note.search", {"text": "boxes"}, "after the move", user=alice)
            assert [n["note"] for n in found["result"]["notes"]] == [made["result"]["note"]]
            hub.plugins.install(str(NOTES), {"user": alice, "token": None})      # a reinstall still swaps folders
            assert sorted(p.name.split(".")[0] for p in (after / "plugins").iterdir()) == ["net", "notes", "shell"]   # PL7
        finally:
            hub.close()
    asyncio.run(go())
