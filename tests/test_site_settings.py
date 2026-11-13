"""The site's Settings page with its Plugins section (P4, PL1, PL3, PL4), and stream steps on the
Jobs page (P8), driven through real HTTP on localhost, as the browser drives them."""

import asyncio
import base64
import json
import pathlib

from nacl.signing import SigningKey

from helpers import Clock, eventually
from memory_link import link_pair
from rook_hub import Hub, journal
from rook_protocol import decode, encode
from test_site import ask, browser, login

PLUGINS = pathlib.Path(__file__).parent / "plugins"
OUTPUT = b"backup started\r\nsnapshot 1f3e saved\r\n"


def write_plugin(folder, version, greeting, manifest_extra=None):
    """A third sample plugin, `greet`, whose cap greet.hello answers `greeting`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.json").write_text(json.dumps(
        {"name": "greet", "version": version, "hub": {"module": "hub.py"}, "skill": "SKILL.md",
         "site": {"settings": [{"title": "Greet", "file": "site/settings.js"}]}, **(manifest_extra or {})}))
    (folder / "SKILL.md").write_text("# greet\nCall greet.hello.\n")
    (folder / "hub.py").write_text(
        "def setup(plugin):\n"
        "    @plugin.cap('greet.hello', 'Say hello.', {'type': 'object', 'properties': {}})\n"
        "    def hello(call, args):\n"
        f"        return {{'text': {greeting!r}}}\n")
    return folder


class Box:
    """An approved worker whose cap shell.open opens a stream and writes OUTPUT to it."""

    def __init__(self, hub, owner):
        self.hub = hub
        self.key = SigningKey.generate().verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": "janitor", "key": self.key, "macs": [], "caps": []})
        record = hub.approve(self.key, user=owner)
        self.guid, self.moniker = record["guid"], record["moniker"]

    async def connect(self):
        hub_end, self.link = link_pair(self.key)
        self.serving = asyncio.create_task(self.hub.serve_link(hub_end))
        self.answering = asyncio.create_task(self._answer())
        await self.link.send(encode({
            "type": "heartbeat", "protocol": 1, "identity": {"name": "janitor", "key": self.key},
            "facts": {"os": "linux", "arch": "x86_64", "hostname": "janitor", "form": "python", "version": "0.1.0",
                      "macs": []}, "band": {"gateway_mac": None}, "health": {},
            "caps": [{"name": "shell.open", "description": "Open a terminal as a stream.", "returns": "stream",
                      "schema": {}}]}))
        await eventually(lambda: self.hub.get_worker(self.guid)["online"])

    async def _answer(self):
        handles = 0
        while True:
            try:
                message = decode(await self.link.recv())
            except EOFError:
                return
            if message["type"] == "call":
                handles += 1
                handle = f"t{handles}"
                await self.link.send(encode({"type": "stream_open", "id": message["id"], "stream": handle,
                                             "title": "nightly backup", "description": "restic, following the log"}))
                await self.link.send(encode({"type": "stream_data", "stream": handle,
                                             "data": base64.b64encode(OUTPUT).decode("ascii")}))

    async def stop(self):
        self.link.close()
        await self.serving
        await self.answering


def test_site_settings(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    for name in ("notes", "lamp"):
        hub.plugins.install(str(PLUGINS / name), {"user": alice["guid"], "token": None})
    box = Box(hub, alice["guid"])
    await box.connect()
    greet = write_plugin(tmp_path / "greet", "1.0.0", "hello")
    try:
        async with browser() as client:
            # 0. Without a session every request is refused, saying what to do (T4).
            for method, path in (("GET", "/api/settings"), ("GET", "/api/plugins"),
                                 ("POST", "/api/plugins/check"), ("POST", "/api/plugins")):
                status, reply = await ask(client, method, base + path, {"path": str(greet)} if method == "POST" else None)
                assert status == 401 and reply["code"] == "no_session" and reply["next"], (method, path)
            assert hub.plugins.list()[-1]["name"] == "lamp"          # nothing was installed
            for path in ("/settings", "/site/pages/settings.js", "/site/sections/plugins.js"):
                async with client.get(base + path) as response:
                    assert response.status == 200, path

            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            session = hub.auth.list_tokens()[-1]["moniker"]

            # 1. The nav ends with Settings (P2, P4); its sections are installed like pages: core's one is Plugins.
            status, reply = await ask(client, "GET", base + "/api/session")
            assert reply["nav"][0]["pages"][-1] == {"id": "settings", "label": "Settings", "path": "/settings",
                                                    "script": "/site/pages/settings.js"}
            assert await ask(client, "GET", base + "/api/settings") == (200, {"sections": [
                {"id": "plugins", "label": "Plugins", "script": "/site/sections/plugins.js"}]})

            # 2. The installed plugins and what each added (PL1, PL3).
            status, reply = await ask(client, "GET", base + "/api/plugins")
            listed = {p["name"]: p for p in reply["plugins"]}
            assert list(listed) == ["notes", "lamp"]
            assert listed["notes"]["caps"] == ["note.create", "note.search"]
            assert listed["notes"]["record_types"] == ["note"] and listed["notes"]["drivers"] == []
            assert listed["lamp"]["drivers"] == ["lamp"] and listed["lamp"]["caps"] == []
            assert listed["notes"]["version"] == "1.0.0" and listed["notes"]["moniker"] and listed["notes"]["installed_at"]
            assert (listed["notes"]["worker"], listed["notes"]["site"], listed["notes"]["skill"]) == (False, False, False)

            # 3. Install a third plugin through the site: first what it would do, then the install,
            #    through plugin.install as a call under the user (PL4, L1).
            status, reply = await ask(client, "POST", base + "/api/plugins/check", {"path": str(greet)})
            assert status == 200 and reply == {"plugin": {"name": "greet", "version": "1.0.0"}, "replaces": None}
            assert [p["name"] for p in hub.plugins.list()] == ["notes", "lamp"]   # checking installs nothing
            status, reply = await ask(client, "POST", base + "/api/plugins", {"path": str(greet)})
            assert status == 200 and reply["installed"]["name"] == "greet" and reply["installed"]["caps"] == ["greet.hello"]
            assert (reply["installed"]["site"], reply["installed"]["skill"]) == (True, True)
            [call] = journal.read(hub, kind="call", cap="plugin.install")
            assert (call["user"], call["token"], call["why"]) == (alice["moniker"], session, "Installed on the Settings page.")
            assert (await hub.call("hub", "greet.hello", {}, "try", user=alice["guid"]))["result"] == {"text": "hello"}
            status, reply = await ask(client, "GET", base + "/api/plugins")
            assert [p["name"] for p in reply["plugins"]] == ["notes", "lamp", "greet"]

            # 4. A reinstall: the check says it replaces the installed one; the install swaps the code at once (PL3).
            write_plugin(greet, "1.1.0", "hi there")
            status, reply = await ask(client, "POST", base + "/api/plugins/check", {"path": str(greet)})
            assert reply["plugin"] == {"name": "greet", "version": "1.1.0"}
            assert reply["replaces"]["name"] == "greet" and reply["replaces"]["version"] == "1.0.0"
            status, reply = await ask(client, "POST", base + "/api/plugins", {"path": str(greet)})
            assert status == 200 and reply["installed"]["version"] == "1.1.0"
            assert (await hub.call("hub", "greet.hello", {}, "try", user=alice["guid"]))["result"] == {"text": "hi there"}
            assert len(journal.read(hub, kind="call", cap="plugin.install")) == 2

            # 5. Refusals say what to fix, at the check and at the install; nothing is installed.
            broken = tmp_path / "broken"
            broken.mkdir()
            (broken / "plugin.json").write_text('{"name": "Broken Plugin", "version": "1"}')
            for path in ("/api/plugins/check", "/api/plugins"):
                status, reply = await ask(client, "POST", base + path, {"path": str(broken)})
                assert status == 400 and reply["code"] == "bad_manifest" and "plugin.json" in reply["next"], path
            failing = write_plugin(tmp_path / "failing", "1.0.0", "x")
            (failing / "plugin.json").write_text(json.dumps({"name": "failing", "version": "1.0.0",
                                                             "hub": {"module": "hub.py"}}))
            (failing / "hub.py").write_text("raise RuntimeError('no setup here')\n")
            status, reply = await ask(client, "POST", base + "/api/plugins", {"path": str(failing)})
            assert status == 400 and reply["code"] == "plugin_load_failed" and "hub.py" in reply["next"]
            status, reply = await ask(client, "POST", base + "/api/plugins/check", {"path": str(tmp_path / "nowhere")})
            assert status == 400 and reply["code"] == "bad_plugin" and reply["next"]
            status, reply = await ask(client, "POST", base + "/api/plugins", {})
            assert status == 400 and reply["code"] == "bad_message"
            assert [p["name"] for p in hub.plugins.list()] == ["notes", "lamp", "greet"]

            # 6. A job whose call opens a stream: its run step names the stream (P8), and the
            #    stream reads through the site, its bytes as the worker sent them.
            created = (await hub.call("hub", "job.create", {
                "title": "nightly-backup", "match": {"event": "nightly.start"},
                "call": {"worker": box.moniker, "cap": "shell.open", "args": {}}}, "test", user=alice["guid"]))["result"]
            hub.events.emit("nightly.start", {})
            await eventually(lambda: journal.read(hub, kind="job"))
            status, reply = await ask(client, "GET", f"{base}/api/jobs/runs?job={created['job']['moniker']}")
            [run] = reply["runs"]
            [step] = run["steps"]
            stream = hub.streams.list()[0]
            assert step["stream"] == stream["moniker"] and step["ok"] and step["cap"] == "shell.open"
            status, reply = await ask(client, "GET", f"{base}/api/streams/{step['stream']}")
            assert status == 200 and reply["stream"]["title"] == "nightly backup" and reply["stream"]["open"]

            status, read = await ask(client, "GET", f"{base}/api/streams/{step['stream']}/read?from=0&wait=5")
            assert status == 200
            assert b"".join(base64.b64decode(e["data"]) for e in read["entries"] if e["direction"] == "out") == OUTPUT
            # A step that returned a result names no stream.
            await hub.call("hub", "job.create", {"title": "say-hi", "match": {"event": "hi"},
                                                 "call": {"worker": "hub", "cap": "greet.hello", "args": {}}},
                           "test", user=alice["guid"])
            hub.events.emit("hi", {})
            await eventually(lambda: len(journal.read(hub, kind="job")) == 2)
            status, reply = await ask(client, "GET", base + "/api/jobs/runs")
            assert [s["stream"] for r in reply["runs"] for s in r["steps"]] == [None, stream["moniker"]]
    finally:
        await box.stop()
        await hub.stop_serving()
        hub.close()
