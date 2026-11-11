"""Bake, Nov 9: "Scroll chat to the bottom when it starts/opens. I want the
workers page to be the same as the older site. I don't like the archive button
where it is, archive should be a function on the sidebar per page. Like an
ellipse. in that ellipse I want a way to make a given page publicly accessible
for things like reports or reference"."""
from __future__ import annotations

import json
import re
from pathlib import Path

from rook.core2.hub.doors.web import caps as web_caps
from tests.test_core2_dashboard import Client, run, site  # noqa: F401 - the fixture

STATIC = Path(web_caps.__file__).with_name("static")
WORKERS = Path(web_caps.__file__).with_name("builtin") / "hub" / "pages" / "workers.html"


def test_a_public_page_opens_without_signing_in(site):
    async def go():
        hub, tokens = await site()
        try:
            owner = Client(hub)
            await owner.sign_in(tokens["bake"])
            anon = Client(hub)
            # private: nobody gets in without signing in
            status, out = await anon.json("POST", "/web/api/public/open", {"page": "hub/workers"})
            assert status == 404 and "isn't public" in out["error"]

            out = await owner.call("page.public", {"page": "hub/workers"})
            assert out["ok"] and out["result"] == {"page": "hub/workers", "public": True, "url": "/public/hub/workers"}
            sidebar = (await owner.call("page.list"))["result"]
            row = next(p for s in sidebar["segments"] for p in s["pages"] if p["page"] == "hub/workers")
            assert row["public"] is True
            got = (await owner.call("page.get", {"page": "hub/workers"}))["result"]
            assert got["public"]["person"] == "person:bake" and got["public_url"] == "/public/hub/workers"

            # the public view: just the page, no chat or sidebar
            status, headers, body = await anon.req("GET", "/public/hub/workers")
            assert status == 200 and b"public.js" in body and b"dock" not in body and b'id="nav"' not in body
            out = (await anon.json("POST", "/web/api/public/open", {"page": "hub/workers"}))[1]
            assert out["ok"] and out["page"]["title"] == "Workers"
            status, headers, body = await anon.req("GET", out["frame"])
            assert status == 200 and b"hub.workers" in body and "sandbox" in headers["Content-Security-Policy"]
            # its calls act as whoever made it public, within the page's grants
            reply = (await anon.json("POST", "/web/api/public/page", {"token": out["token"], "cap": "hub.workers"}))[1]
            assert reply["ok"] and reply["result"]["workers"][0]["hub"] is True
            entry = hub.journal.query(cap="hub.workers", viewer={"owner": True}, limit=1)[0]
            assert entry["person"] == "person:bake"
            denied = (await anon.json("POST", "/web/api/public/page", {"token": out["token"], "cap": "page.archive",
                                                                       "args": {"page": "hub/workers"}}))[1]
            assert not denied["ok"]
            assert (await anon.json("GET", f"/web/api/public/events?token={out['token']}"))[1]["ok"]
            # nothing else opens for them: the site still wants a sign-in
            assert (await anon.json("GET", "/web/api/me"))[0] == 401
            assert (await anon.req("GET", "/"))[0] == 303

            # private again: the link and the tokens it gave out stop
            assert (await owner.call("page.public", {"page": "hub/workers", "public": False}))["ok"]
            assert (await anon.json("POST", "/web/api/public/open", {"page": "hub/workers"}))[0] == 404
            again = await anon.json("POST", "/web/api/public/page", {"token": out["token"], "cap": "hub.workers"})
            assert again[0] == 404
            assert json.loads((hub.data / "pages.json").read_text())["public"] == {}
        finally:
            await hub.stop()
    run(go())


def test_the_sidebar_has_a_menu_per_page_and_no_archive_button():
    html = (STATIC / "shell.html").read_text()
    assert "archive-btn" not in html
    shell = (STATIC / "shell.js").read_text()
    assert "archive-btn" not in shell
    menu = shell[shell.index("function pageMenu("):shell.index("function mark(")]
    assert '"page.archive"' in menu and '"page.public"' in menu and "Copy link" in menu and "/public/" in shell
    link = shell[shell.index("function link("):shell.index("function pageMenu(")]
    assert "\\u22ef" in link                                      # the ... beside each page


def test_the_chat_scrolls_to_the_newest_message():
    shell = (Path(__file__).resolve().parents[1] / "rook/plugins/agents/shell/dock.js").read_text()
    opened = shell[shell.index("function dockOpen("):shell.index("function scrollChat(")]
    assert "scrollChat()" in opened
    add = shell[shell.index("function addRow("):shell.index("function agentName(")]
    assert "scrollChat()" in add
    start = shell[shell.index("async function startDock("):]
    assert re.search(r"dockOpen\(open\);\s*scrollChat\(\);", start)


def test_the_workers_page_is_the_04_one(site):
    page = WORKERS.read_text()
    for part in ('id="k-total"', 'id="k-online"', 'id="k-stale"', 'id="verbar"', 'id="bars"',
                 '<option value="os">Operating system</option>', '<option value="band">Band</option>',
                 "worker-group-heading", "worker-band", "batt", "seenbar", 'data-layout="grid"', 'data-layout="scene"', 'id="fleet-scene"', 'data-rook-lib="rook-scene"', "var(--overlay)"):
        assert part in page, part

    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            token = (await c.open("hub/workers"))["token"]
            out = (await c.page(token, "hub.workers"))["result"]
            assert out["now"] > 0 and out["bands"] == {hub.band: "home"}
            assert out["workers"][0]["band"] == hub.band
        finally:
            await hub.stop()
    run(go())
