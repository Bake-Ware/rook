"""The 0.4 Knowledge page on 0.5 (Bake, Nov 9: "Let's bring over the trust map
and knowledge pages too"): rook/web/knowledge.js's view - folder tree, search,
review, a page with breadcrumbs, links both ways, sources and history; edit,
new, move, archive - as the knowledge plugin's sandboxed page, on its caps."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_core2_dashboard import Client
from tests.test_core2_worksurface import serve

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "rook" / "plugins" / "knowledge"
PAGE = PLUGIN / "pages" / "knowledge.html"


def _script() -> str:
    html = PAGE.read_text()
    return html[html.index("<script>") + len("<script>"):html.rindex("</script>")]


def _declared() -> dict:
    return yaml.safe_load((PLUGIN / "plugin.yaml").read_text())["pages"]["knowledge"]


def test_the_page_is_the_04_view_in_the_rook_style():
    html = PAGE.read_text()
    for part in ("kn-layout", "kn-side", "kn-tree-row", "kn-tog", "kn-crumbs", "kn-review", "kn-reviewbar",
                 "kn-sections", "What links here", "Subpages", "Sources", "History", "New subpage", "Move",
                 "Archive", "Search the wiki", "Show archived pages", "Review"):
        assert part in html, part
    assert "var(--overlay)" in html and "var(--mono)" in html and ":root" not in html   # page.css's tokens, untouched
    js = _script()
    # a sandboxed page: no network, no modals, no storage, no History API state calls
    for banned in ("fetch(", "XMLHttpRequest", "prompt(", "confirm(", "alert(", "localStorage", "pushState",
                   "innerHTML"):
        assert banned not in js, banned


def test_the_page_calls_only_the_caps_it_declares():
    declared = set(_declared()["caps"])
    used = set(re.findall(r'rook\.call\("([a-z.]+)"', _script()))
    assert used and used <= declared, used - declared
    assert used == declared                       # nothing declared it doesn't use
    assert _declared()["events"] == ["knowledge.*"]


def test_the_script_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    out = subprocess.run([node, "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
                         input=_script(), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_the_page_works_on_the_hub(tmp_path):
    async def body(hub, tokens):
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        opened = await c.open("knowledge/knowledge")
        assert opened["ok"] and opened["page"]["title"] == "Knowledge"
        assert set(opened["page"]["caps"]) == set(_declared()["caps"])
        status, _, frame = await c.req("GET", opened["frame"])
        assert status == 200 and b"kn-layout" in frame
        token = opened["token"]

        async def call(cap, **args):
            out = await c.page(token, cap, args, why="test")
            assert out["ok"], (cap, out)
            return out["result"]

        # new page, then a subpage under it (+ and New subpage)
        folder = await call("knowledge.create", title="Castling", body="The 0.5 rewrite.", kind="summary")
        child = await call("knowledge.create", title="First run", parent=folder["slug"],
                           body="See [[castling]] and [[nowhere]].")
        rows = await call("knowledge.list", limit=500, offset=0, archived=False)
        assert {r["slug"] for r in rows} == {"castling", "first-run"}
        assert next(r for r in rows if r["slug"] == "first-run")["parent"] == folder["id"]
        got = await call("knowledge.get", id="castling")
        assert [x["slug"] for x in got["children"]] == ["first-run"]
        assert [x["slug"] for x in got["backlinks"]] == ["first-run"]
        assert (await call("knowledge.get", id="first-run"))["missing"] == ["nowhere"]
        assert [h["slug"] for h in await call("knowledge.search", query="rewrite", limit=200)] == ["castling"]

        # review: verify, dispute with a note, back to unverified; who and when are kept
        out = await call("knowledge.review", id=child["id"], revision=child["revision"], verdict="verified")
        assert out["verification"] == "verified" and out["reviewed_by"] == "person:bake" and out["reviewed_at"] > 0
        out = await call("knowledge.review", id="first-run", verdict="disputed", note="Step 3 is wrong.")
        assert out["verification"] == "disputed" and out["review_note"] == "Step 3 is wrong."
        out = await call("knowledge.review", id="first-run", verdict="unverified")
        assert out["verification"] == "unverified"

        # edit and move to the top (parent "")
        out = await call("knowledge.update", id=child["id"], revision=out["revision"], title="First run, 0.5",
                         body="Changed.", kind="procedure", parent="")
        assert out["parent"] is None and out["title"] == "First run, 0.5" and out["kind"] == "procedure"
        hist = await call("knowledge.history", id=child["id"])
        assert len(hist) == out["revision"] and {h["op"] for h in hist} >= {"create", "update"}

        # archive, see it under Old, restore
        out = await call("knowledge.archive", id=child["id"], revision=out["revision"])
        assert [r["slug"] for r in await call("knowledge.list", limit=500, offset=0, archived=True)] == ["first-run"]
        assert [r["slug"] for r in await call("knowledge.list", limit=500, offset=0, archived=False)] == ["castling"]
        await call("knowledge.archive", id=child["id"], revision=out["revision"], restore=True)
        assert {r["slug"] for r in await call("knowledge.list", limit=500)} == {"castling", "first-run"}

        # nothing it didn't declare
        denied = await c.page(token, "knowledge.link", {"id": "castling", "kind": "url", "ref": "x"}, why="test")
        assert not denied["ok"]

    serve(tmp_path, body, packages=("workbench",))
