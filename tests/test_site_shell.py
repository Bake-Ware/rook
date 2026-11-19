"""A plugin's shell part (house agent brief HA1; 0.5's manifest `shell:`): stylesheets and classic
scripts the site loads on every page once a person is signed in, in the manifest's order, driven
through real HTTP on localhost. The built-in `agents` plugin ships the chat dock this way."""

import asyncio
import json
import pathlib
import shutil

from rook_hub import Hub, plugins, site
from test_site import ask, browser, login

SHELF = pathlib.Path(__file__).parent / "plugins" / "shelf"
AGENTS = pathlib.Path(plugins.__file__).parent / "defaults" / "agents"
DOCK_FILES = ["shell/dock.css", "shell/rookie.js", "shell/rookie-map.js", "shell/chat-widgets.js", "shell/dock.js"]


def shelf_with_shell(tmp_path, name, files):
    """The sample plugin shelf, copied as `name`, with a shell part naming `files` (each written)."""
    folder = tmp_path / name
    shutil.copytree(SHELF, folder)
    for file, text in files.items():
        (folder / file).parent.mkdir(parents=True, exist_ok=True)
        (folder / file).write_text(text)
    manifest = json.loads((folder / "plugin.json").read_text())
    manifest["name"] = name
    manifest["site"] = {"shell": [{"file": f} for f in files]}
    (folder / "plugin.json").write_text(json.dumps(manifest))
    return folder


def test_a_plugins_shell_files_are_listed_in_order_and_served_to_a_session(tmp_path):
    asyncio.run(shell_files(tmp_path))


async def shell_files(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    hub.auth.create_user("Alice", "correct horse")
    try:
        async with browser() as client, browser() as stranger:
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200

            # Every hub has the agents plugin's five, in the order its manifest lists them.
            status, reply = await ask(client, "GET", base + "/api/session")
            assert reply["shell"] == [
                {"plugin": "agents", "kind": "style" if f.endswith(".css") else "script",
                 "url": f"/plugin-files/agents/{f}"} for f in DOCK_FILES]

            # A plugin installed later comes after it, its own files in its order.
            folder = shelf_with_shell(tmp_path, "lamp-dock", {
                "shell/b.js": "window.b = 1;", "shell/a.css": "#x { color: red; }", "shell/c.js": "window.c = 2;"})
            status, reply = await ask(client, "POST", base + "/api/plugins", {"path": str(folder)})
            assert status == 200, reply
            status, reply = await ask(client, "GET", base + "/api/session")
            assert [(p["plugin"], p["kind"], p["url"]) for p in reply["shell"][len(DOCK_FILES):]] == [
                ("lamp-dock", "script", "/plugin-files/lamp-dock/shell/b.js"),
                ("lamp-dock", "style", "/plugin-files/lamp-dock/shell/a.css"),
                ("lamp-dock", "script", "/plugin-files/lamp-dock/shell/c.js")]

            # Served to a session only, a stylesheet as CSS and a script as JavaScript.
            for file, kind in (("shell/a.css", "text/css"), ("shell/b.js", "text/javascript")):
                async with client.get(f"{base}/plugin-files/lamp-dock/{file}") as response:
                    assert response.status == 200 and response.headers["Content-Type"].startswith(kind)
                    assert await response.read() == (folder / file).read_bytes()
                async with stranger.get(f"{base}/plugin-files/lamp-dock/{file}") as response:
                    assert response.status == 401
            for file in DOCK_FILES:
                async with client.get(f"{base}/plugin-files/agents/{file}") as response:
                    assert response.status == 200 and await response.read() == (AGENTS / file).read_bytes()

            # The shell loads them: shell.js reads `shell` from the session and has the two places.
            text = (pathlib.Path(site.__file__).parent / "site" / "shell.js").read_text()
            assert "session.shell" in text and 'id="work"' in text and 'id="shell-dock"' in text

            # Refused at install, saying what to fix: a file that is neither .css nor .js, one that
            # is missing, and one named twice.
            for name, entries, says in (
                    ("bad-kind", [{"file": "shell/a.html"}], "must end in .css"),
                    ("bad-missing", [{"file": "shell/nothing.js"}], "is missing"),
                    ("bad-twice", [{"file": "shell/a.js"}, {"file": "shell/a.js"}], "used twice")):
                folder = shelf_with_shell(tmp_path, name, {"shell/a.html": "<p>", "shell/a.js": ";"})
                manifest = json.loads((folder / "plugin.json").read_text())
                manifest["site"] = {"shell": entries}
                (folder / "plugin.json").write_text(json.dumps(manifest))
                status, reply = await ask(client, "POST", base + "/api/plugins", {"path": str(folder)})
                assert status == 400 and reply["code"] == "bad_manifest" and says in reply["message"], reply
    finally:
        await hub.stop_serving()
        hub.close()
