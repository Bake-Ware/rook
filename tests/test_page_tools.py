"""The house agent's page tools (rook/hub/plugins/home/page.py): what the
dashboard chat says about the person's page, and actions handed to their
browser through the chat's poll. The browser half is in
tests/test_home_dock_browser.py."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from rook.hub.plugins.home import page
from rook.hub.plugins.home.page import PageBridge

SNAP = {"view": "jobs", "title": "Jobs", "hash": "#jobs", "selection": "nightly backup",
        "visible": "Jobs · nightly backup · next run 02:00",
        "items": [{"ref": "b1", "kind": "button", "label": "Run now"},
                  {"ref": "i2", "kind": "input", "label": "Filter", "value": "back"},
                  {"ref": "x", "kind": "button", "label": "bad ref"},
                  {"ref": "b3", "kind": "rocket", "label": "bad kind"}],
        "pages": [{"id": "jobs", "title": "Jobs", "section": "Work"}]}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Home:
    def __init__(self, on=True):
        self.settings = {"page_tools": on}
        self.rows = []

    def journal(self, cap, args, reply, thread=None):
        self.rows.append((cap, args, reply, thread))


def test_a_snapshot_is_bounded_and_typed():
    assert page.clean({"view": "../etc"}) is None and page.clean("jobs") is None
    big = {**SNAP, "visible": "x" * 10_000, "selection": "y" * 9_000,
           "items": [{"ref": f"b{i}", "kind": "button", "label": "z" * 500} for i in range(200)]}
    ctx = page.clean(big)
    assert len(ctx["visible"]) == page.MAX_VISIBLE and len(ctx["selection"]) == page.MAX_SELECTION
    assert len(ctx["items"]) == page.MAX_ITEMS and len(ctx["items"][0]["label"]) == page.MAX_LABEL
    ctx = page.clean(SNAP)
    assert [i["ref"] for i in ctx["items"]] == ["b1", "i2"]   # bad refs and kinds dropped
    text = page.describe(ctx)
    assert "Page: Jobs (#jobs)" in text and "Selected text: nightly backup" in text
    assert '[b1] button "Run now"' in text and "[i2] input \"Filter\" = 'back'" in text
    assert "jobs (Jobs)" in text


def test_tools_only_while_the_page_is_known_and_fresh():
    clock, home = Clock(), Home()
    bridge = PageBridge(home, clock=clock)
    assert bridge.tools_for("r1") == [] and bridge.prompt_text("r1") == ""
    assert not bridge.note("r1", {"view": "Bad View"})
    assert bridge.note("r1", SNAP)
    assert {t["function"]["name"] for t in bridge.tools_for("r1")} == set(page.NAMES)
    assert bridge.tools_for("r2") == []                  # another room's chat
    assert bridge.prompt_text("r1").startswith(page.RULES)
    assert "Their page:\nPage: Jobs" in bridge.prompt_text("r1")
    clock.t += page.FRESH_S + 1
    assert bridge.tools_for("r1") == [] and bridge.prompt_text("r1") == ""
    # Switched off: nothing offered even with a fresh page.
    off = PageBridge(Home(on=False), clock=clock)
    off.note("r1", SNAP)
    assert off.tools_for("r1") == [] and off.prompt_text("r1") == ""


@pytest.mark.asyncio
async def test_an_action_goes_to_the_browser_and_its_answer_comes_back():
    home = Home()
    bridge = PageBridge(home)
    bridge.note("r1", SNAP)

    async def browser():
        for _ in range(100):
            todo = bridge.take("r1")
            if todo:
                break
            await asyncio.sleep(0.01)
        assert todo == [{"id": todo[0]["id"], "do": "fill", "ref": "i2", "value": "s3cret plan"}]
        assert bridge.take("r1") == []                   # handed out once
        after = {**SNAP, "items": [{"ref": "i2", "kind": "input", "label": "Filter",
                                    "value": "s3cret plan"}]}
        assert bridge.done("r1", todo[0]["id"], True, "filled Filter", after)
        assert not bridge.done("r1", todo[0]["id"], True, "again")

    task = asyncio.create_task(browser())
    out = json.loads(await bridge.act("r1", "page_fill", {"ref": "i2", "value": "s3cret plan"}))
    await task
    assert out["ok"] and out["did"] == "filled Filter" and "'s3cret plan'" in out["page"]
    # Journaled as the house agent; what was typed stays on the page.
    cap, args, reply, thread = home.rows[-1]
    assert cap == "home.page" and args["tool"] == "page_fill" and args["value_chars"] == 11
    assert "s3cret" not in json.dumps(args) and reply["ok"] and thread == "r1"


@pytest.mark.asyncio
async def test_a_page_that_does_not_answer_times_out_and_leaves_nothing_queued():
    bridge = PageBridge(Home())
    bridge.note("r1", SNAP)
    out = json.loads(await bridge.act("r1", "page_click", {"ref": "b1"}, timeout=0.05))
    assert not out["ok"] and "did not answer" in out["error"]
    assert bridge.take("r1") == [] and bridge._waiting == {}


@pytest.mark.asyncio
async def test_bad_page_tool_calls_are_refused_before_the_browser():
    bridge = PageBridge(Home())
    unknown = json.loads(await bridge.act("r1", "page_go", {"page": "jobs"}))
    assert "not known" in unknown["error"]               # no page from that room
    bridge.note("r1", SNAP)
    for name, args, why in (("page_go", {"page": "../x"}, "page id"),
                            ("page_click", {"ref": "rm -rf"}, "ref"),
                            ("page_fill", {"value": "x"}, "ref"),
                            ("page_point", {}, "ref or some text"),
                            ("page_explode", {}, "unknown tool")):
        out = json.loads(await bridge.act("r1", name, args, timeout=0.05))
        assert not out["ok"] and why in out["error"], (name, out)
    assert bridge.take("r1") == []


def test_the_persistent_lane_offers_the_page_tools():
    """A dashboard chat turn with page tools must still fit the house agent's
    main lane (sessions.accepts: its tools are a subset of main_tools)."""
    from rook.hub.plugins.home.sessions import Sessions
    me = SimpleNamespace(home=SimpleNamespace(_memory=lambda: None))
    names = {(t.get("function") or t)["name"] for t in Sessions.main_tools(me)}
    assert page.NAMES <= names
