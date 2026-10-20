"""The dashboard on a phone: every page at 390x844 without horizontal scroll.

Runs a real hub in-process (dashboard + MCP, rook.hub.serve) on a scratch data
dir, seeds a project, tasks, knowledge pages and a job with long titles and
paths, signs in, and opens each view in headless Chromium with a phone
viewport. Needs Playwright and its Chromium (`pip install playwright &&
python -m playwright install chromium-headless-shell`); skipped otherwise.
Set ROOK_REQUIRE_BROWSER=1 to fail instead of skipping when the browser is missing.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import urllib.request

import pytest

# ROOK_REQUIRE_BROWSER=1 (e.g. a CI job with a browser): a missing browser fails instead of skipping.
REQUIRE = os.environ.get("ROOK_REQUIRE_BROWSER") == "1"
if REQUIRE:
    import playwright.sync_api as pw
else:
    pw = pytest.importorskip("playwright.sync_api")

PSK = "alpha-bravo-charlie-delta-echo-foxtrot"
TOKEN = "static-token-for-mobile-tests-0123456789"
PASSWORD = "mobile-test-password"
PHONE = {"width": 390, "height": 844}
LONG = "/home/someone/projects/a-really-long-folder-name/with/nested/paths/and-more.md"

# (name, hash). Every view in the sidebar, plus the Work views and a task.
PAGES = [
    ("overview", "#overview"), ("approvals", "#approvals"), ("start", "#start"), ("workers", "#workers"),
    ("bands", "#bands"), ("huddles", "#huddles"), ("work", "#work"), ("work-board", "#work?v=board"),
    ("work-list", "#work?v=list"), ("work-timeline", "#work?v=timeline"), ("work-huddle", "#work?v=huddle"),
    ("work-project", "#work?p=phone-pass"), ("work-task", "#work?t=check-every-page-on-a-phone"),
    ("knowledge", "#knowledge"), ("knowledge-page", "#knowledge?p=where-logs-live"),
    ("jobs", "#jobs"), ("sessions", "#sessions"), ("install", "#install"), ("settings", "#settings"),
    ("plugins", "#plugins"), ("ccmod", "#ccmod"), ("updates", "#updates"), ("account", "#account"),
    ("tokens", "#tokens"), ("guidance", "#guidance"), ("vault", "#vault"), ("home", "#home"),
    ("roles", "#roles"), ("voice", "#voice"), ("models", "#models"), ("memory", "#memory"), ("help", "#help"),
]

# Wider than the viewport, and not inside an element that scrolls or clips sideways.
OVERFLOW_JS = r"""() => {
  const W = document.documentElement.clientWidth, out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || r.right <= W + 1) continue;
    if (el.closest('.rook-art,.app-sidebar,#toast')) continue;
    let p = el.parentElement, inner = false;
    while (p && p !== document.body) {
      if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(p).overflowX) && p.getBoundingClientRect().right <= W + 1) { inner = true; break; }
      p = p.parentElement;
    }
    if (inner) continue;
    out.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
             (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).join('.') : '') +
             ' right=' + Math.round(r.right));
  }
  return {width: W, scrollWidth: document.documentElement.scrollWidth, wide: out.slice(0, 8)};
}"""


def _free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Relay(asyncio.DatagramProtocol):
    """Stands in for telesthete-hub (no workers on this band)."""


def _mcp_calls(base: str, calls: list[tuple[str, dict]]) -> list[dict]:
    """Call MCP tools over plain HTTP with the static token."""
    def post(body, sid=None):
        hdr = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
        if sid:
            hdr["mcp-session-id"] = sid
        req = urllib.request.Request(base + "/mcp", data=json.dumps(body).encode(), headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.headers.get("mcp-session-id"), r.read().decode()

    sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                              "clientInfo": {"name": "pytest-mobile", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    out = []
    for i, (tool, args) in enumerate(calls, start=2):
        _, text = post({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                        "params": {"name": tool, "arguments": args}}, sid)
        lines = [ln[5:] for ln in text.splitlines() if ln.startswith("data:")] or [text]
        msg = json.loads(lines[-1])
        body = msg["result"]["content"][0]["text"]
        try:
            out.append(json.loads(body))
        except ValueError:
            raise AssertionError(f"{tool}: {body[:300]}") from None
    return out


def _seed(base: str) -> None:
    rid = iter(range(1000))
    req = lambda: f"mobile-seed-{next(rid)}"  # noqa: E731
    (concept,) = _mcp_calls(base, [("rook_concept", {"action": "create", "data": {"title": "Home lab"},
                                                      "request_id": req()})])
    (project,) = _mcp_calls(base, [("rook_project", {"action": "create", "request_id": req(), "data": {
        "title": "Phone pass", "slug": "phone-pass", "body": "Seeded.", "parent": concept["result"]["id"]}})])
    pid = project["result"]["id"]
    titles = ["Check every page on a phone", "Make dialogs full-screen sheets",
              "Very long task title that keeps going to check how cards wrap: " + LONG]
    calls = [("rook_task", {"action": "create", "request_id": req(), "data": {
        "title": t, "body": "Body " + LONG, "parent": pid,
        "attrs": {"criteria": ["no sideways scroll"], "tags": ["ui", "a-rather-long-label-name"],
                  "start": "2026-10-19", "due": "2026-10-25", "workers": ["worker-with-a-long-name-01"]}}})
             for t in titles]
    calls += [
        ("rook_knowledge", {"action": "create", "request_id": req(), "data": {
            "title": "Where logs live", "slug": "where-logs-live",
            "body": "Logs: `" + LONG + "`\n\n| host | path |\n|---|---|\n| worker-1 | " + LONG + " |",
            "attrs": {"knowledge_kind": "procedure", "tags": ["ops"]}}}),
        ("rook_jobs", {"action": "create", "data": {
            "name": "nightly-health", "description": "Seeded", "entry": "s1",
            "triggers": [{"kind": "cron", "expr": "0 3 * * *"}],
            "steps": {"s1": {"kind": "noop"}}}}),
    ]
    for reply in _mcp_calls(base, calls):
        assert reply.get("ok", True), reply


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    from rook.hub.serve import Hub, parse_args
    from rook.remote.bootstrap import prepare

    data = tmp_path_factory.mktemp("mobile-hub")
    saved = {k: os.environ.get(k) for k in ("ROOK_DATA_DIR", "HOME", "ROOK_UPDATE_KEY", "ROOK_KNOWLEDGE")}
    os.environ["ROOK_KNOWLEDGE"] = "1"   # tasks, projects and the wiki
    # Playwright finds its browsers under $HOME unless told; HOME moves to scratch below.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/.cache/ms-playwright"))
    os.environ["ROOK_DATA_DIR"] = str(data / "data")
    os.environ["HOME"] = str(data / "home")
    os.environ["ROOK_UPDATE_KEY"] = str(data / "update-key")
    port, mcp_port, relay_port = _free_port(), _free_port(), _free_port(socket.SOCK_DGRAM)
    argv = ["--bind", "127.0.0.1", "--port", str(port), "--mcp-bind", "off", "--hub-port", str(relay_port),
            "--web-pass", PASSWORD, "--psk", PSK, "--static-token", TOKEN, "--domain", f"http://127.0.0.1:{port}"]
    loop = asyncio.new_event_loop()
    started = threading.Event()
    box: dict = {}

    async def boot():
        relay, _ = await loop.create_datagram_endpoint(_Relay, local_addr=("127.0.0.1", relay_port))
        ap, args = parse_args(argv)
        prepare(ap, args)
        h = Hub(args, argv)
        await h.start()
        box.update(hub=h, relay=relay)

    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(boot())
        except BaseException as e:  # surfaced below
            box["error"] = e
        started.set()
        if "error" not in box:
            loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    started.wait(60)
    if "error" in box:
        raise box["error"]
    base = f"http://127.0.0.1:{port}"
    try:
        _seed(base)
        yield base
    finally:
        async def stop():
            await box["hub"].stop()
            box["relay"].close()
        asyncio.run_coroutine_threadsafe(stop(), loop).result(30)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="module")
def browser(hub):
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser installed
            if REQUIRE:
                raise
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        yield b
        b.close()


def _signed_in(browser, hub, **ctx):
    page = browser.new_context(**ctx).new_page()
    page.set_default_timeout(15000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(hub + "/login")
    page.fill("input[name=pass]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.errors = errors
    return page


@pytest.fixture(scope="module")
def phone(browser, hub):
    page = _signed_in(browser, hub, viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True)
    yield page
    page.context.close()


def _open(page, hub, hsh):
    page.errors.clear()
    page.goto(hub + "/" + hsh)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)


@pytest.mark.parametrize("name,hsh", PAGES, ids=[p[0] for p in PAGES])
def test_no_horizontal_scroll_at_390px(phone, hub, name, hsh):
    _open(phone, hub, hsh)
    r = phone.evaluate(OVERFLOW_JS)
    assert r["scrollWidth"] <= r["width"], f"{name}: page scrolls sideways ({r['scrollWidth']} > {r['width']}): {r['wide']}"
    assert not r["wide"], f"{name}: wider than the screen: {r['wide']}"
    assert not phone.errors, f"{name}: JS errors: {phone.errors}"


def test_login_page_fits_a_phone(browser, hub):
    page = browser.new_page(viewport=PHONE, is_mobile=True, has_touch=True)
    page.goto(hub + "/login")
    r = page.evaluate(OVERFLOW_JS)
    size = page.evaluate("getComputedStyle(document.querySelector('input[name=pass]')).fontSize")
    page.context.close()
    assert r["scrollWidth"] <= r["width"] and not r["wide"], r
    assert float(size.rstrip("px")) >= 16, size


def test_phone_shell_header_tab_bar_and_drawer(phone, hub):
    _open(phone, hub, "#workers")
    tabs = phone.evaluate("""() => [...document.querySelectorAll('.tabbar > a, .tabbar > button')]
        .map(e => { const r = e.getBoundingClientRect(); return [e.textContent.trim(), r.width, r.height, r.bottom]; })""")
    assert len(tabs) == 5 and all(w >= 44 and h >= 44 for _, w, h, _ in tabs), tabs
    assert all(abs(b - PHONE["height"]) <= 1 for *_, b in tabs), tabs          # pinned to the bottom
    # Inputs don't make iOS zoom in: 16px or more.
    sizes = phone.evaluate("""() => [...document.querySelectorAll('input:not([type=checkbox]):not([type=radio]), select, textarea')]
        .filter(e => e.offsetParent).map(e => parseFloat(getComputedStyle(e).fontSize))""")
    assert sizes and min(sizes) >= 16, sizes
    # The header stays on top and is compact.
    phone.evaluate("window.scrollTo(0, 400)")
    head = phone.evaluate("(() => { const r = document.querySelector('.topbar').getBoundingClientRect(); return [r.top, r.height]; })()")
    assert head[0] == 0, head
    # The page description sits behind (i).
    assert phone.is_hidden("#view-description")
    phone.click("#view-info")
    assert phone.is_visible("#view-description")
    # The sidebar is a drawer: closed, opened by the menu button, closed by a link.
    assert phone.evaluate("document.querySelector('.app-sidebar').getBoundingClientRect().right") <= 0
    phone.click("#nav-toggle")
    phone.wait_for_timeout(300)
    assert phone.evaluate("document.querySelector('.app-sidebar').getBoundingClientRect().left") >= 0
    links = phone.evaluate("""() => [...document.querySelectorAll('.app-sidebar nav a')].filter(a => a.offsetParent)
        .map(a => a.getBoundingClientRect().height)""")
    assert links and min(links) >= 44, links
    phone.click("#tab-jobs")
    phone.wait_for_timeout(300)
    assert phone.evaluate("location.hash") == "#jobs"
    assert phone.evaluate("document.body.classList.contains('nav-open')") is False


def test_dialogs_are_full_screen_sheets(phone, hub):
    _open(phone, hub, "#knowledge")
    phone.evaluate("""() => { const d = document.createElement('dialog'); d.id = 'sheet-probe';
        d.textContent = 'probe'; document.body.append(d); d.showModal(); }""")
    box = phone.evaluate("(() => { const r = document.getElementById('sheet-probe').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
    phone.evaluate("document.getElementById('sheet-probe').close()")
    assert box[0] == 0 and box[1] == 0 and box[2] == PHONE["width"] and box[3] == PHONE["height"], box


def test_desktop_keeps_the_sidebar(browser, hub):
    page = _signed_in(browser, hub, viewport={"width": 1280, "height": 800})
    page.goto(hub + "/#overview")
    page.wait_for_timeout(500)
    sidebar = page.evaluate("document.querySelector('.app-sidebar').getBoundingClientRect().left")
    tabbar = page.is_visible(".tabbar")
    r = page.evaluate(OVERFLOW_JS)
    page.context.close()
    assert sidebar == 0 and not tabbar
    assert r["scrollWidth"] <= r["width"], r
