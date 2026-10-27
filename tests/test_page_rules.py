"""What the house agent may do on the dashboard page (rook/web/page-rules.js),
checked with node so it runs in CI without a browser (the browser side is in
tests/test_home_dock_browser.py). Skipped without node."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[1] / "rook" / "web" / "page-rules.js"
NODE = shutil.which("node")

# Skipped without node on a laptop; in CI (GitHub sets CI=true) a missing node
# fails instead, so a green CI run means these rules really ran.
pytestmark = pytest.mark.skipif(NODE is None and not os.environ.get("CI"),
                                reason="node is not installed")


def test_node_is_there_in_ci():
    assert NODE, "node is missing: the page rules (rook/web/page-rules.js) did not run"


def _judge(cases: list[dict]) -> list:
    script = (f"const r = await import({json.dumps(RULES.as_uri())});"
              "const cases = JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(cases.map(c => c.go ? r.goRefusal(c.page, c.query) : "
              "(c.fill ? r.fillRefusal : r.pressRefusal)(r.describeControl(c)))));")
    out = subprocess.run([NODE, "--input-type=module", "-e", script, json.dumps(cases)],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def press(view="overview", kind="button", label="", names=None, **kw):
    return {"view": view, "kind": kind, "label": label, "names": names or label, **kw}


def test_presses_only_show_or_find_things_on_everyday_pages():
    allowed = [press(label="Refresh list"), press(label="Show details"), press(label="Next"),
               press(kind="tab", label="Editor"), press(kind="tab", label="Runs"),
               press(kind="summary", label="More"), press(kind="link", label="Jobs", href="#jobs"),
               press(kind="link", label="Open room", href="#huddles?room=abc"),
               press(kind="link", label="Home", href="#"),
               press(kind="link", label="Board", href="#work?v=board&project=p1"),
               press("knowledge", label="Search"), press("huddles", label="Open")]
    refused = [
        press(label="Run now"), press(label="Save"), press(label="Start session"),
        press(label="Deleting…"), press(label="Approved"), press(label="Sign-out"),
        press(label="🗑", names="🗑 Delete this job"),           # by its aria-label
        press(label="Pause"), press(label="Archive"), press(label="Dismiss"),
        press(label="Cancel"), press(label="Send"), press(label="Looks fine", inDialog=True),
        press(kind="link", label="Leave", href="/logout"), press(label="Do it"),
        press("jobs", label="Show details"),                     # Jobs is open-and-point only
        press(label="Next", disabled=True),
        press(kind="tab", label="Editor", inDialog=True),
        press("huddles", label="Show me later", inCard=True),     # a card's choice, agent-written
        press("huddles", kind="tab", label="Next", inCard=True),
        press(kind="link", label="Show", href="#account?invite=abc"),   # where it goes counts
        press(kind="link", label="Show", href="#bands?worker=w1"),
        press(kind="link", label="Show", href="#work?v=huddle&hp=p1"),
        press(kind="link", label="Show", href="#work?v=%68uddle&hb=_"),
    ] + [press(v, label="Show details") for v in (
        "trust", "approvals", "decisions", "vault", "tokens", "people", "account", "bands",
        "settings", "plugins", "updates", "workers", "install", "ccmod", "sessions", "guidance",
        "home", "roles", "models", "voice", "jobs")]
    got = _judge(allowed + refused)
    assert got[:len(allowed)] == [None] * len(allowed), list(zip(allowed, got))
    for case, why in zip(refused, got[len(allowed):]):
        assert why, case


def test_typing_only_into_text_fields_and_never_one_that_saves_on_change():
    fill = lambda **kw: {"fill": True, **press(**kw)}
    allowed = [fill(kind="input", type="text", label="Filter"),
               fill(kind="input", type="search", label="Search"), fill(kind="textarea", label="Notes"),
               fill(view="memory", kind="input", type="", label="Find")]
    refused = [fill(kind="select", label="State"), fill(kind="checkbox", label="Enable job"),
               fill(kind="input", type="password", label="Password"),
               fill(kind="input", type="file", label="Upload"),
               fill(kind="input", type="text", label="Name", readOnly=True),
               fill(kind="input", type="text", label="Reply", inDialog=True),
               fill(kind="input", type="text", label="Delete confirmation"),
               fill(kind="button", label="Show"),
               fill(view="settings", kind="input", type="text", label="Hub name"),
               fill(view="huddles", kind="textarea", label="Your reply", inCard=True),
               fill(view="jobs", kind="textarea", label="Job JSON")]
    got = _judge(allowed + refused)
    assert got[:len(allowed)] == [None] * len(allowed), list(zip(allowed, got))
    for case, why in zip(refused, got[len(allowed):]):
        assert why, case


def test_page_go_queries_only_pick_what_to_show():
    got = _judge([{"go": True, "page": "huddles", "query": "room=abc"},
                  {"go": True, "page": "trust"},
                  {"go": True, "page": "account", "query": "invite=x"},
                  {"go": True, "page": "bands", "query": "worker=w1"},
                  {"go": True, "page": "work", "query": "v=huddle&hb=_"},
                  {"go": True, "page": "work", "query": "v=%68uddle&hb=_"},     # decoded first
                  {"go": True, "page": "work", "query": "%76=huddle"},
                  {"go": True, "page": "work", "query": "?v=huddle"},
                  {"go": True, "page": "work", "query": "v=board&hb=_"},       # a huddle key
                  {"go": True, "page": "work", "query": "v=HUDDLE"},
                  {"go": True, "page": "../etc"}])
    assert got[0] is None and got[1] is None
    assert all(got[2:]), got
    ok = _judge([{"go": True, "page": "work", "query": "v=board&project=p1"},
                 {"go": True, "page": "work", "query": "?v=list"}])
    assert ok == [None, None], ok


def _js_set(name: str) -> set[str]:
    text = RULES.read_text()
    js = text[text.index(f"{name} = new Set(["):].split("]", 1)[0]
    return {s.strip().strip("'") for s in js.split("[", 1)[1].split(",")}


def test_the_hub_and_the_page_agree_on_which_queries_are_fine():
    """The hub repeats page_go's rule: both copies of the lists stay the same,
    and both decide the same queries the same way."""
    from rook.hub.plugins.home import page
    assert _js_set("QUERY_OK") == set(page.QUERY_OK)
    assert _js_set("HUDDLE_KEYS") == set(page.HUDDLE_KEYS)
    queries = ["v=huddle", "v=%68uddle&hb=_", "%76=huddle", "?v=huddle", "v=board&hb=_",
               "v=HUDDLE", "v=board&project=p1", "?v=list", "", "v=timeline",
               # Parser edges, so a Python/JS difference would show here:
               "hb", "v=board;hb=_", "x;hb=_", "v=hud+dle", "v=+huddle+", "v=board&v=huddle",
               "v=%zz&hb", "v=%zzhuddle", "%20v%20=huddle", "v=board#hb=_", "??v=huddle"]
    got = _judge([{"go": True, "page": "work", "query": q} for q in queries])
    assert [bool(g) for g in got] == [page.opens_huddle(q) for q in queries], list(zip(queries, got))
