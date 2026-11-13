"""What a plugin adds to the site (PL1, PL3, P1, P2, P4, P6; Bake 13 Nov), driven through real
HTTP on localhost, as the browser drives it: pages in a segment of their own, a settings
section and a component, their files served to a session only, a cap called from a plugin
page as the user, and pins on a plugin's page across reinstalls."""

import asyncio
import json
import pathlib
import re
import shutil

from helpers import Clock, eventually
from memory_link import connector
from rook_hub import Hub, journal, site
from rook_worker import Worker
from test_plugins import write_plugin
from test_site import ask, browser, login

PLUGINS = pathlib.Path(__file__).parent / "plugins"
SHELF = PLUGINS / "shelf"
SHELL = pathlib.Path(site.__file__).parent / "site" / "shell.js"


def copy_shelf(tmp_path, name, **site_changes):
    """The sample plugin shelf, copied, with its manifest's site section changed."""
    folder = tmp_path / name
    shutil.copytree(SHELF, folder)
    manifest = json.loads((folder / "plugin.json").read_text())
    manifest["site"].update(site_changes)
    (folder / "plugin.json").write_text(json.dumps(manifest))
    return folder, manifest


def write_manifest(folder, manifest):
    (folder / "plugin.json").write_text(json.dumps(manifest))


def test_site_plugins(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    try:
        async with browser() as client, browser() as stranger:
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            session = hub.auth.list_tokens()[-1]["moniker"]

            # 1. Before the install: no Library segment.
            status, reply = await ask(client, "GET", base + "/api/session")
            assert [s["segment"] for s in reply["nav"]] == ["Hub"] and reply["components"] == []

            # 2. Install the sample through the Settings page: with no restart, its page is in the
            #    nav under its new segment (P2), its component and settings section are listed (PL1, P4).
            status, reply = await ask(client, "POST", base + "/api/plugins", {"path": str(SHELF)})
            assert status == 200, reply
            installed = reply["installed"]
            assert installed["pages"] == [{"segment": "Library", "title": "Shelf", "path": "/plugins/shelf/shelf"}]
            assert (installed["settings_sections"], installed["components"]) == (["Shelf"], ["book-card"])
            status, reply = await ask(client, "GET", base + "/api/session")
            assert [s["segment"] for s in reply["nav"]] == ["Hub", "Library"]
            assert reply["nav"][1]["pages"] == [{"id": "shelf/shelf", "label": "Shelf", "path": "/plugins/shelf/shelf",
                                                 "script": "/plugin-files/shelf/site/shelf.js", "plugin": "shelf"}]
            assert reply["components"] == [{"name": "book-card", "script": "/plugin-files/shelf/site/book-card.js",
                                            "plugin": "shelf"}]
            status, reply = await ask(client, "GET", base + "/api/settings")
            assert reply["sections"][-1] == {"id": "shelf/shelf", "label": "Shelf", "plugin": "shelf",
                                             "script": "/plugin-files/shelf/site/settings.js"}
            status, reply = await ask(client, "GET", base + "/api/plugins")
            [listed] = [p for p in reply["plugins"] if p["name"] == "shelf"]
            assert listed["pages"][0]["path"] == "/plugins/shelf/shelf" and listed["components"] == ["book-card"]

            # 3. The page's address serves the shell; the files the manifest names are served to a
            #    session (P1), and nothing else: not to a stranger, and no file it does not name.
            async with client.get(base + "/plugins/shelf/shelf") as response:
                assert response.status == 200 and "shell.js" in await response.text()
            for name in ("shelf.js", "book-card.js", "settings.js"):
                async with client.get(f"{base}/plugin-files/shelf/site/{name}") as response:
                    assert response.status == 200 and response.headers["Content-Type"].startswith("text/javascript")
                    assert response.headers["Cache-Control"] == "no-store"
                    assert await response.read() == (SHELF / "site" / name).read_bytes()
                async with stranger.get(f"{base}/plugin-files/shelf/site/{name}") as response:
                    assert response.status == 401 and (await response.json())["code"] == "no_session"
            for path in ("/plugin-files/shelf/hub.py", "/plugin-files/shelf/plugin.json",
                         "/plugin-files/shelf/site/%2e%2e/hub.py", "/plugin-files/notes/site/shelf.js"):
                async with client.get(base + path) as response:
                    assert response.status == 404 and (await response.json())["code"] == "unknown_file", path

            # 4. A plugin's page calls its cap with call(): POST /api/call, as the user, journaled
            #    under the user and the session with the page's why (Bake 13 Nov, L1).
            body = {"worker": "hub", "cap": "shelf.books", "args": {}, "why": "Listed on the Shelf page."}
            status, reply = await ask(client, "POST", base + "/api/call", body)
            assert status == 200 and reply["type"] == "result" and reply["result"]["books"][0]["title"] == "The Hobbit"
            [entry] = journal.read(hub, kind="call", cap="shelf.books")
            assert (entry["user"], entry["token"], entry["why"]) == (alice["moniker"], session, "Listed on the Shelf page.")
            status, reply = await ask(stranger, "POST", base + "/api/call", body)
            assert status == 401 and reply["code"] == "no_session"
            status, reply = await ask(client, "POST", base + "/api/call", dict(body, cap="shelf.nothing"))
            assert status == 400 and reply["code"] == "unknown_cap" and reply["next"]
            for broken in (dict(body, why=""), dict(body, args=[1])):
                status, reply = await ask(client, "POST", base + "/api/call", broken)
                assert status == 400 and reply["code"] == "bad_message"
            assert len(journal.read(hub, kind="call", cap="shelf.books")) == 1

            # 5. Refused at install, with what to fix; nothing of the plugin is kept (PL1).
            outside = tmp_path / "outside.js"
            outside.write_text("export default {};")
            refusals = [
                ({"pages": [{"segment": "Library", "title": "Escape", "file": "../outside.js"}]}, "bad_manifest",
                 "leaves the plugin's folder"),
                ({"pages": [{"segment": "Library", "title": "Escape", "file": str(outside)}]}, "bad_manifest",
                 "leaves the plugin's folder"),
                ({"components": [{"name": "far-away", "file": "site/link.js"}]}, "bad_manifest",
                 "leaves the plugin's folder"),
                ({"settings": [{"title": "Gone", "file": "site/missing.js"}]}, "bad_manifest", "is missing"),
                ({"pages": [{"segment": "Hub", "title": "Workers", "file": "site/shelf.js"}]}, "page_taken",
                 "core already has a page called 'workers'"),
                ({"pages": [{"segment": "Library", "title": "Shelf", "file": "site/shelf.js"}]}, "page_taken",
                 "plugin shelf already has a page called 'shelf'"),
                ({"components": [{"name": "pin-view", "file": "site/book-card.js"}]}, "component_taken",
                 "core already has a component called 'pin-view'"),
                ({"components": [{"name": "book-card", "file": "site/book-card.js"}]}, "component_taken",
                 "plugin shelf already has a component called 'book-card'"),
                ({"components": [{"name": "card", "file": "site/book-card.js"}]}, "bad_manifest", "at least one hyphen"),
                ({"pages": [{"segment": "Library", "file": "site/shelf.js"}]}, "bad_manifest", "segment, title, file"),
                ({"settings": [{"title": "Odds", "file": "site/settings.js"}, {"title": "odds!", "file": "site/settings.js"}]},
                 "bad_manifest", "used twice in this plugin"),
                ({"pages": [{"segment": "A", "title": "Twin", "file": "site/shelf.js"},
                            {"segment": "B", "title": "twin", "file": "site/shelf.js"}]}, "bad_manifest", "used twice"),
            ]
            for i, (change, code, said) in enumerate(refusals):
                folder, manifest = copy_shelf(tmp_path, f"other{i}", pages=[], settings=[], components=[])
                manifest["name"] = f"other{i}"
                manifest["hub"] = {"module": "hub.py"}
                (folder / "hub.py").write_text("def setup(plugin):\n    pass\n")
                (folder / "site" / "link.js").symlink_to(outside)
                manifest["site"].update(change)
                write_manifest(folder, manifest)
                status, reply = await ask(client, "POST", base + "/api/plugins", {"path": str(folder)})
                assert status == 400 and reply["code"] == code and said in reply["message"], (change, reply)
                assert reply["next"]
            assert [p["name"] for p in hub.plugins.list()] == ["shelf"]
            # The check before an install already says what to fix in the manifest.
            status, reply = await ask(client, "POST", base + "/api/plugins/check", {"path": str(tmp_path / "other0")})
            assert status == 400 and "leaves the plugin's folder" in reply["message"]

            # 6. Pins name the plugin and the page (P6, Bake 13 Nov): one survives a reinstall,
            #    and says so once the plugin's page is gone in a new version.
            pins = [{"label": "Library: Shelf", "href": "/plugins/shelf/shelf"}, {"label": "Workers", "href": "/workers"}]
            status, reply = await ask(client, "PUT", base + "/api/pins", {"pins": pins})
            assert reply == {"pins": pins}
            assert (await ask(client, "POST", base + "/api/plugins", {"path": str(SHELF)}))[0] == 200   # reinstall
            assert await ask(client, "GET", base + "/api/pins") == (200, {"pins": pins})
            folder, manifest = copy_shelf(tmp_path, "shelf-2", pages=[])
            manifest["version"] = "2.0.0"
            write_manifest(folder, manifest)
            assert (await ask(client, "POST", base + "/api/plugins", {"path": str(folder)}))[0] == 200
            status, reply = await ask(client, "GET", base + "/api/pins")
            assert reply["pins"][0] == dict(pins[0], gone="Plugin shelf has no page shelf now.")
            assert reply["pins"][1] == pins[1]
            status, reply = await ask(client, "GET", base + "/api/session")
            assert [s["segment"] for s in reply["nav"]] == ["Hub"]      # the segment went with its last page
            async with client.get(base + "/plugin-files/shelf/site/shelf.js") as response:
                assert response.status == 404                          # no longer named by the manifest
            async with client.get(base + "/plugins/shelf/shelf") as response:
                assert response.status == 200                          # the shell, which says the page is gone
            status, reply = await ask(client, "PUT", base + "/api/pins",
                                      {"pins": [{"label": "Library: Lost", "href": "/plugins/nowhere/lost"}]})
            assert reply["pins"][0]["gone"] == "No plugin called nowhere is installed."
    finally:
        await hub.stop_serving()
        hub.close()


def test_plugin_site_loads_at_start(tmp_path):
    """An installed plugin's site parts are there after a restart too (PL3)."""
    hub = Hub(tmp_path / "hub")
    user = hub.auth.create_user("Alice", "pw")
    hub.plugins.install(str(SHELF), {"user": user["guid"], "token": None})
    hub.close()
    hub = Hub(tmp_path / "hub")
    try:
        assert [s["segment"] for s in site.navigation(hub)] == ["Hub", "Library"]
        assert [c["name"] for c in site.plugin_components(hub)] == ["book-card"]
    finally:
        hub.close()


def test_core_components_match_the_shell():
    """The names refused to plugins are the components shell.js registers."""
    assert tuple(re.findall(r"app\.component\('([a-z-]+)', ", SHELL.read_text())) == site.COMPONENTS


def test_plugins_list_shows_where_worker_parts_loaded(tmp_path):
    asyncio.run(worker_parts(tmp_path))


async def worker_parts(tmp_path):
    """The Settings page's Plugins list says, per plugin, which workers loaded its worker part
    and which failed to, with why (PL3; brief 21 item 7). Real Python workers on in-memory links."""
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    running, monikers = [], []
    for name in ("alpha", "beta"):
        worker = Worker(tmp_path / name, base, connector(hub), name=name, caps={}, macs=[], knock_every=0.02)
        running.append((worker, asyncio.create_task(worker.run())))
        await eventually(lambda: any(k["key"] == worker.key for k in hub.list_pending()))
        monikers.append(hub.approve(worker.key, user=alice["guid"])["moniker"])
        await eventually(lambda: hub.get_worker(monikers[-1])["online"])
    try:
        async with browser() as client:
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            status, reply = await ask(client, "POST", base + "/api/plugins", {"path": str(PLUGINS / "greeter")})
            assert status == 200 and (reply["installed"]["loaded_on"], reply["installed"]["failed_on"]) == ([], [])
            broken = write_plugin(tmp_path / "broken", {"name": "broken", "version": "0.1.0",
                                                         "worker": {"python": {"module": "w.py"}}},
                                  **{"w.py": "raise RuntimeError('this part needs a GPU')\n"})
            assert (await ask(client, "POST", base + "/api/plugins", {"path": broken}))[0] == 200

            async def listed():
                status, reply = await ask(client, "GET", base + "/api/plugins")
                by_name = {p["name"]: p for p in reply["plugins"]}
                done = len(by_name["greeter"]["loaded_on"]) == 2 and len(by_name["broken"]["failed_on"]) == 2
                return by_name if done else None
            for _ in range(200):
                if by_name := await listed():
                    break
                await asyncio.sleep(0.02)
            assert by_name, "the workers never reported their plugin parts"
            assert sorted(p["worker"] for p in by_name["greeter"]["loaded_on"]) == sorted(monikers)
            assert all(p["version"] == "1.0.0" and "error" not in p for p in by_name["greeter"]["loaded_on"])
            assert by_name["greeter"]["failed_on"] == [] and by_name["broken"]["loaded_on"] == []
            assert sorted(by_name["broken"]["failed_on"], key=lambda p: p["worker"]) == [
                {"worker": m, "version": "0.1.0", "error": "RuntimeError: this part needs a GPU"} for m in sorted(monikers)]
            assert by_name["greeter"]["worker"] and by_name["greeter"]["pages"] == []    # site parts listed beside them
    finally:
        for worker, task in running:
            task.cancel()
        await asyncio.gather(*(task for _, task in running), return_exceptions=True)
        await hub.stop_serving()
        hub.close()
