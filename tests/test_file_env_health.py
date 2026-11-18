"""The rest of PL7 (brief 41): the default plugins `file` (CAP-04), `worker` (worker.ping, CAP-05)
and `battery` (CAP-07), and shell.env in `shell` (CAP-02), with 0.5's names, arguments and
results. A real hub, served, and a real rook_worker over its real carrier, as in
test_default_plugins.py. None of 0.5's cut-offs is built: a 9 MiB file is read whole, 6,000
entries are listed, 150 matches are found, a 2,000-character line is given whole."""

import asyncio
import base64
import os
import time

from test_default_plugins import Rig, default_module, until

NEW = {"file": ["file.list", "file.read", "file.search", "file.stat", "file.write"],
       "worker": ["worker.ping"], "battery": ["battery.status"]}


async def ready(rig):
    await until(lambda: {c for caps in NEW.values() for c in caps} | {"shell.env"} <= set(rig.announced()))


def test_each_new_cap_is_announced_with_its_schema_and_one_line_description(tmp_path):
    for name, caps in NEW.items():
        declared = default_module(name)
        assert sorted(declared) == caps
        for cap in declared.values():
            assert cap.description and "\n" not in cap.description
            assert cap.schema["type"] == "object" and cap.schema.get("additionalProperties") is False
    assert "shell.env" in default_module("shell")

    async def go():
        async with Rig(tmp_path) as rig:
            await ready(rig)
            record = {c["name"]: c for c in rig.hub.get_worker(rig.guid)["caps"]}
            for name in (*NEW, "shell"):
                for cap_name, cap in default_module(name).items():
                    assert record[cap_name]["description"] == cap.description
                    assert record[cap_name]["schema"] == cap.schema
            assert {"battery", "file", "worker"} <= {p["name"] for p in rig.hub.get_worker(rig.guid)["plugins"]}
    asyncio.run(go())


def test_file_caps(tmp_path):
    asyncio.run(files(tmp_path))


async def files(tmp_path):
    async with Rig(tmp_path) as rig:
        await ready(rig)
        top = tmp_path / "files"

        # Written (making the folders), read back whole and in part.
        note = top / "notes" / "a.txt"
        assert await rig.ok("file.write", path=str(note), content="hello\nworld\n", create_parents=True) == \
            {"path": str(note), "written": 12}
        assert await rig.ok("file.read", path=str(note)) == \
            {"content": "hello\nworld\n", "size": 12, "encoding": "utf-8"}
        assert await rig.ok("file.read", path=str(note), max_bytes=5) == \
            {"content": "hello", "size": 12, "encoding": "utf-8", "truncated": True}
        assert await rig.ok("file.write", path=str(note), content="again\n", append=True) == \
            {"path": str(note), "written": 6}
        assert (await rig.ok("file.read", path=str(note)))["content"] == "hello\nworld\nagain\n"
        assert (await rig.ok("file.read", path=str(note), encoding="latin-1"))["encoding"] == "latin-1"

        # Binary content, as base64 both ways.
        data = bytes(range(256)) * 4
        blob = top / "blob.bin"
        assert (await rig.ok("file.write", path=str(blob), content=base64.b64encode(data).decode(),
                             encoding="base64"))["written"] == 1024
        read = await rig.ok("file.read", path=str(blob), encoding="base64")
        assert base64.b64decode(read["content"]) == data and read["size"] == 1024 and "truncated" not in read

        # Listed: entries with type, size, modified and mode; recursive adds the relative path.
        (top / ".hidden").write_text("x")
        os.symlink(note, top / "link")
        listed = {e["name"]: e for e in (await rig.ok("file.list", path=str(top)))["entries"]}
        assert set(listed) == {"notes", "blob.bin", ".hidden", "link"}
        assert listed["blob.bin"]["type"] == "file" and listed["blob.bin"]["size"] == 1024
        assert listed["notes"]["type"] == "dir" and listed["link"]["type"] == "symlink"
        assert listed["blob.bin"]["mode"] == oct(os.stat(blob).st_mode & 0o7777)
        assert isinstance(listed["blob.bin"]["modified"], float) and "path" not in listed["blob.bin"]
        deep = (await rig.ok("file.list", path=str(top), recursive=True, hidden=False))["entries"]
        assert sorted(e["path"] for e in deep) == ["blob.bin", "link", "notes", os.path.join("notes", "a.txt")]
        some = await rig.ok("file.list", path=str(top), limit=2)
        assert len(some["entries"]) == 2 and some["truncated"] is True

        # Found by search: path, line, text; ignore_case, glob, limit; binary files skipped.
        (top / "notes" / "b.md").write_text("World one\nnothing\nworld two\n")
        found = await rig.ok("file.search", pattern="world", path=str(top))
        assert sorted((m["path"], m["line"], m["text"]) for m in found["matches"]) == [     # link: a.txt, as in 0.5
            (str(top / "link"), 2, "world"), (str(top / "notes" / "a.txt"), 2, "world"),
            (str(top / "notes" / "b.md"), 3, "world two")]
        assert "truncated" not in found
        assert len((await rig.ok("file.search", pattern="world", path=str(top), ignore_case=True))["matches"]) == 4
        assert [m["line"] for m in (await rig.ok("file.search", pattern="world", path=str(top), glob="*.md",
                                                 ignore_case=True))["matches"]] == [1, 3]
        limited = await rig.ok("file.search", pattern="world", path=str(top), ignore_case=True, limit=1)
        assert len(limited["matches"]) == 1 and limited["truncated"] is True
        (top / "bin.dat").write_bytes(b"world\0world\n")
        assert all(m["path"] != str(top / "bin.dat")
                   for m in (await rig.ok("file.search", pattern="world", path=str(top)))["matches"])
        assert [m["line"] for m in (await rig.ok("file.search", pattern="world", path=str(note)))["matches"]] == [2]

        # Described by stat.
        stat = await rig.ok("file.stat", path=str(note))
        assert stat["exists"] is True and stat["type"] == "file" and stat["size"] == 18
        assert set(stat) == {"exists", "type", "size", "modified", "mode"}
        assert (await rig.ok("file.stat", path=str(top)))["type"] == "dir"
        assert (await rig.ok("file.stat", path=str(top / "link")))["type"] == "symlink"
        assert await rig.ok("file.stat", path=str(top / "missing")) == {"exists": False}

        # Anything that opens is read, waiting as long as it takes: a FIFO, until its writer is done.
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        reading = asyncio.create_task(rig.ok("file.read", path=str(fifo)))
        await asyncio.sleep(0.3)
        assert not reading.done()                       # waiting for a writer

        def writer():
            with open(fifo, "w") as f:
                f.write("through a fifo\n")
        await asyncio.to_thread(writer)
        assert await reading == {"content": "through a fifo\n", "size": 0, "encoding": "utf-8"}

        # Refusals, each with a reason and a next step (C3).
        for cap, args, said in [
                ("file.read", {"path": str(top / "missing")}, "not found"),
                ("file.read", {"path": str(top)}, "is a folder"),
                ("file.read", {"path": str(note), "lines": 3}, "file.read takes no argument lines"),
                ("file.read", {"path": str(note), "encoding": "no-such-codec"}, "unknown encoding"),
                ("file.read", {}, "file.read needs path"),
                ("file.write", {"path": str(top / "nowhere" / "x"), "content": ""}, "create_parents"),
                ("file.write", {"path": str(blob), "content": "not base64!", "encoding": "base64"}, "not base64"),
                ("file.list", {"path": str(note)}, "not a folder"),
                ("file.list", {"path": str(top), "limit": 0}, "limit must be a whole number, 1 or more"),
                ("file.search", {"pattern": "("}, "bad pattern"),
                ("file.search", {"pattern": "x", "path": str(top / "missing")}, "not found"),
                ("file.stat", {"path": str(note), "follow": True}, "file.stat takes no argument follow")]:
            reply = await rig.refused(cap, **args)
            assert reply["code"] == "cap_failed" and said in reply["message"], (cap, args, reply)

        # No cut-off of 0.5's: a 9 MiB file whole (0.5 stopped at 8 MiB), 6,000 entries (0.5: 5,000),
        # 150 matches (0.5: 100), a 2,000-character line whole (0.5: 1,000).
        big = top / "big.txt"
        big.write_text("y" * (9 * 1024 * 1024))
        whole = await rig.ok("file.read", path=str(big))
        assert len(whole["content"]) == 9 * 1024 * 1024 and "truncated" not in whole
        many = top / "many"
        many.mkdir()
        for i in range(6000):
            (many / f"{i}").touch()
        listing = await rig.ok("file.list", path=str(many))
        assert len(listing["entries"]) == 6000 and "truncated" not in listing
        (top / "lines.txt").write_text("".join(f"match {i}\n" for i in range(150)) + "match " + "z" * 1994 + "\n")
        hits = (await rig.ok("file.search", pattern="^match", path=str(top / "lines.txt")))["matches"]
        assert len(hits) == 151 and len(hits[-1]["text"]) == 2000


def test_shell_env_worker_ping_and_battery_status(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK41_PLAIN", "plain value")
    monkeypatch.setenv("ROOK41_API_TOKEN", "a credential-looking value")
    monkeypatch.delenv("ROOK41_MISSING", raising=False)

    async def go():
        async with Rig(tmp_path) as rig:
            await ready(rig)
            # shell.env by name, with a default, and by prefix. A credential-looking name is given as it
            # is: 0.5's masking is not built (Needs Bake).
            assert await rig.ok("shell.env", name="ROOK41_PLAIN") == "plain value"
            assert await rig.ok("shell.env", name="ROOK41_MISSING") is None
            assert await rig.ok("shell.env", name="ROOK41_MISSING", default="fallback") == "fallback"
            assert await rig.ok("shell.env", prefix="ROOK41_") == {
                "ROOK41_API_TOKEN": "a credential-looking value", "ROOK41_PLAIN": "plain value"}
            assert await rig.ok("shell.env", name="ROOK41_API_TOKEN") == "a credential-looking value"
            for args, said in [({}, "give name"), ({"name": "A", "prefix": "B"}, "not both"),
                               ({"name": "A", "all": True}, "shell.env takes no argument all")]:
                reply = await rig.refused("shell.env", **args)
                assert said in reply["message"], reply

            # worker.ping: pong, and the worker's clock.
            before = time.time()
            pong = await rig.ok("worker.ping")
            assert pong["pong"] is True and before <= pong["at"] <= time.time() and set(pong) == {"pong", "at"}
            assert "takes no argument" in (await rig.refused("worker.ping", loud=True))["message"]

            # battery.status: on a machine without a battery, a reason and a next step; with one,
            # percent and status.
            reply = await rig.call("battery.status")
            if reply["type"] == "error":
                assert reply["code"] == "cap_failed" and "has no battery" in reply["message"] and reply["next"]
            else:
                assert set(reply["result"]) == {"percent", "status"}
                assert isinstance(reply["result"]["percent"], float)
            assert "takes no argument" in (await rig.refused("battery.status", verbose=True))["message"]
    asyncio.run(go())
