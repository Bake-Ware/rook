"""What the house agent may do on the dashboard page (rook/web/page-rules.js),
checked with node so it runs in CI without a browser (the browser side is in
tests/test_home_dock_browser.py). Skipped without node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[1] / "rook" / "web" / "page-rules.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


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
                  {"go": True, "page": "../etc"}])
    assert got[0] is None and got[1] is None
    assert all(got[2:]), got


def test_the_hub_and_the_page_agree_on_which_queries_are_fine():
    from rook.hub.plugins.home import page
    text = RULES.read_text()
    js = text[text.index("QUERY_OK = new Set(["):].split("]", 1)[0]
    assert {s.strip().strip("'") for s in js.split("[", 1)[1].split(",")} == set(page.QUERY_OK)
