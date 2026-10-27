"""The house agent's chat dock (home-dock.js) and Settings > Restart, in a real
browser against a real hub run in-process (rook.hub.serve), like
test_mobile_layout.py. The model endpoint is a fake that answers at once.
Skipped without Playwright's Chromium unless ROOK_REQUIRE_BROWSER=1.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading

import pytest

from tests.test_mobile_layout import OVERFLOW_JS, PASSWORD, PSK, REQUIRE, TOKEN, _free_port, _Relay, pw

PHONE = {"width": 390, "height": 844}
SHOTS = os.environ.get("ROOK_DOCK_SHOTS")   # a directory: save screenshots there
REPLY = "The lights are on."


class _LLM:
    """OpenAI-compatible endpoint (the home plugin's _http hook)."""
    calls = 0

    async def __call__(self, method, url, body, headers, timeout):
        if url.endswith("/models"):
            return 200, {"data": [{"id": "small-model"}]}
        _LLM.calls += 1
        return 200, {"model": body["model"], "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": REPLY}}]}


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    import rook.hub.plugins.settings as settings_plugin
    from rook.hub.serve import Hub, parse_args
    from rook.remote.bootstrap import prepare

    data = tmp_path_factory.mktemp("dock-hub")
    saved = {k: os.environ.get(k) for k in ("ROOK_DATA_DIR", "HOME", "ROOK_UPDATE_KEY")}
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/.cache/ms-playwright"))
    os.environ["ROOK_DATA_DIR"] = str(data / "data")
    os.environ["HOME"] = str(data / "home")
    os.environ["ROOK_UPDATE_KEY"] = str(data / "update-key")
    gate = settings_plugin._admin_gate
    settings_plugin._admin_gate = lambda what: None
    port, relay_port = _free_port(), _free_port(socket.SOCK_DGRAM)
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
        node = h.mcp.mcp._rook_hub
        node.plugin("home")._http = _LLM()
        box.update(hub=h, relay=relay, node=node)

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

    def configure(**values):
        async def go():
            for k, v in values.items():
                box["node"].settings.set(f"home.{k}", v, actor="human:op")
        asyncio.run_coroutine_threadsafe(go(), loop).result(10)
    configure.box, configure.loop = box, loop     # for tests that swap the model

    try:
        yield f"http://127.0.0.1:{port}", configure
    finally:
        async def stop():
            await box["hub"].stop()
            box["relay"].close()
        asyncio.run_coroutine_threadsafe(stop(), loop).result(30)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        settings_plugin._admin_gate = gate
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


def _signed_in(browser, base, **ctx):
    page = browser.new_context(**ctx).new_page()
    page.set_default_timeout(15000)
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(base + "/login")
    page.fill("input[name=pass]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    return page


def _shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def _open(page, base, hsh):
    page.goto(base + "/" + hsh)
    page.reload()   # a hash-only goto is not a page load
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)


def test_the_dock_follows_the_operator_across_pages(browser, hub):
    base, configure = hub
    page = _signed_in(browser, base, viewport={"width": 1280, "height": 900})
    # Not set up: no dock at all.
    _open(page, base, "#overview")
    assert page.locator(".hd").count() == 0
    configure(enabled=True, base_url="http://llm.example:1234/v1", model="small-model")
    _open(page, base, "#overview")
    launcher = page.locator(".hd-launch")
    launcher.wait_for()
    assert launcher.inner_text() == "@home" and page.locator(".hd-panel").is_hidden()

    # Open, talk; the reply lands in the dock.
    launcher.click()
    assert page.evaluate("document.activeElement.matches('.hd-panel textarea')")
    page.fill(".hd-panel textarea", "are the lights on?")
    page.press(".hd-panel textarea", "Enter")
    page.locator(".hd-panel .hc-msg:not(.me) .hc-text", has_text=REPLY).wait_for()
    _shot(page, "dock-desktop-open")

    # Another page: the same dock, still open, nothing reloaded.
    page.evaluate("showView('workers')")
    page.wait_for_timeout(300)
    assert page.locator(".hd-panel").is_visible()
    assert page.locator(".hd-panel .hc-msg").count() == 2
    # Intelligence sources > Home agent has this chat itself: the dock steps aside.
    page.evaluate("showView('home')")
    page.wait_for_timeout(300)
    assert page.locator(".hd").is_hidden()
    page.evaluate("showView('overview')")
    page.wait_for_timeout(300)
    assert page.locator(".hd-panel").is_visible()

    # A reload: still open, with the conversation.
    _open(page, base, "#jobs")
    page.locator(".hd-panel .hc-text", has_text=REPLY).wait_for()
    # Closed stays closed.
    page.click(".hd-close")
    assert page.locator(".hd-launch").is_visible() and page.locator(".hd-panel").is_hidden()
    _open(page, base, "#overview")
    page.locator(".hd-launch").wait_for()
    assert page.locator(".hd-panel").is_hidden()
    _shot(page, "dock-desktop-closed")
    assert _LLM.calls == 1
    assert page.errors == []
    page.context.close()

    # A phone: above the tab bar, no sideways scroll.
    phone = _signed_in(browser, base, viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True)
    _open(phone, base, "#overview")
    phone.locator(".hd-launch").click()
    phone.locator(".hd-panel .hc-text", has_text=REPLY).wait_for()
    _shot(phone, "dock-phone-open")
    r = phone.evaluate(OVERFLOW_JS)
    assert r["scrollWidth"] <= r["width"] + 1 and not r["wide"], r
    panel = phone.locator(".hd-panel").bounding_box()
    tabbar = phone.locator("nav.tabbar").bounding_box()
    assert panel["y"] >= 0 and panel["y"] + panel["height"] <= tabbar["y"] + 1, (panel, tabbar)
    assert panel["width"] >= PHONE["width"] - 20
    assert phone.errors == []
    phone.context.close()


class _PageLLM:
    """A model that uses the page tools: it opens Jobs, then answers."""
    def __init__(self):
        self.bodies = []

    async def __call__(self, method, url, body, headers, timeout):
        if url.endswith("/models"):
            return 200, {"data": [{"id": "small-model"}]}
        self.bodies.append(body)
        if any(m.get("role") == "tool" for m in body["messages"]):
            msg = {"role": "assistant", "content": "I opened Jobs for you."}
        else:
            msg = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "page_go", "arguments": json.dumps({"page": "jobs"})}}]}
        return 200, {"model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                                                          "message": msg}]}


def test_the_house_agent_sees_the_page_and_acts_on_it(browser, hub):
    """The dock sends the page, the selection and what is on screen; the agent's
    page_go runs in the browser through the chat's poll and comes back."""
    base, configure = hub
    llm = _PageLLM()
    node = configure.box["node"]
    configure.loop.call_soon_threadsafe(setattr, node.plugin("home"), "_http", llm)
    configure(enabled=True, base_url="http://llm.example:1234/v1", model="small-model")
    page = _signed_in(browser, base, viewport={"width": 1280, "height": 900})
    _open(page, base, "#workers")
    page.locator(".hd-launch").click()
    # Select the page title, then ask from the dock.
    page.evaluate("""() => { const r = document.createRange();
        r.selectNodeContents(document.getElementById('view-title'));
        getSelection().removeAllRanges(); getSelection().addRange(r); }""")
    page.fill(".hd-panel textarea", "open my jobs")
    page.press(".hd-panel textarea", "Enter")
    page.locator(".hd-panel .hc-text", has_text="I opened Jobs for you.").wait_for()
    assert page.evaluate("document.body.dataset.view") == "jobs"
    assert page.locator(".hd-panel").is_visible()       # the chat stayed open

    first = llm.bodies[0]
    assert "page_go" in {t["function"]["name"] for t in first.get("tools") or []}
    said = "\n".join(str(m.get("content") or "") for m in first["messages"])
    assert "Their page:\nPage: Workers (#workers)" in said and "Selected text: Workers" in said
    # The tool's answer to the model: done, with the page it landed on.
    tool = json.loads(next(m for m in llm.bodies[1]["messages"] if m.get("role") == "tool")["content"])
    assert tool["ok"] and tool["did"] == "opened Jobs" and "Page: Jobs (#jobs)" in tool["page"]
    # Journaled as the house agent.
    rows = node.journal.query(cap_prefix="home.page")
    assert len(rows) == 1 and rows[0]["ok"] and rows[0]["identity"] == "agent:home", rows
    assert page.errors == []
    page.context.close()


def test_the_front_door_gets_the_page_too(browser, hub):
    """Live hubs route the dock through the front door (home.front_door on): the
    same page context and page tools reach that turn."""
    base, configure = hub
    llm = _PageLLM()
    node = configure.box["node"]
    configure.loop.call_soon_threadsafe(setattr, node.plugin("home"), "_http", llm)
    configure(enabled=True, base_url="http://llm.example:1234/v1", model="small-model",
              front_door=True)
    try:
        page = _signed_in(browser, base, viewport={"width": 1280, "height": 900})
        _open(page, base, "#overview")
        page.locator(".hd-launch").click()
        page.fill(".hd-panel textarea", "show me my jobs")
        page.press(".hd-panel textarea", "Enter")
        page.locator(".hd-panel .hc-text", has_text="I opened Jobs for you.").wait_for()
        assert page.evaluate("document.body.dataset.view") == "jobs"
        first = llm.bodies[0]
        names = {t["function"]["name"] for t in first.get("tools") or []}
        assert {"page_go", "page_click", "do_action"} <= names     # the front door's own tools too
        said = "\n".join(str(m.get("content") or "") for m in first["messages"])
        assert "Their page:\nPage: Overview (#overview)" in said
        assert page.errors == []
        page.context.close()
    finally:
        configure(front_door=False)


def test_the_page_refuses_what_the_person_must_do(browser, hub):
    """page-agent.js on its own: no clicks or typing on pages that gate things,
    no delete/approve-style buttons, no links off the dashboard."""
    base, _ = hub
    page = _signed_in(browser, base, viewport={"width": 1280, "height": 900})
    _open(page, base, "#overview")
    out = page.evaluate("""async () => {
        const m = await import('/account/settings/assets/page-agent.js');
        const box = document.getElementById('view-overview');
        const add = (html) => { const d = document.createElement('div'); d.innerHTML = html;
                                box.prepend(d); return d.firstChild; };
        let pressed = 0;
        add('<button type="button" id="ok-btn">Refresh list</button>').onclick = () => pressed++;
        add('<button type="button" id="bad-btn">Delete everything</button>').onclick = () => pressed += 100;
        add('<a id="off-link" href="/logout">Leave</a>');
        add('<label>Name <input id="name-in"></label>');
        add('<input id="pw-in" type="password" value="hunter2" aria-label="Password">');
        add('<pre data-secret>rk_SHOWN_ONCE_123</pre>');
        add('<strong class="pairing-code">PAIR-CODE-9</strong>');
        const snap = m.snapshot();
        const ref = id => snap.items.find(i => i.label === id || i.label === ({
            'ok-btn': 'Refresh list', 'bad-btn': 'Delete everything', 'off-link': 'Leave',
            'name-in': 'Name', 'pw-in': 'Password'})[id]).ref;
        const r = {};
        r.ok = await m.run({do: 'click', ref: ref('ok-btn')});
        r.bad = await m.run({do: 'click', ref: ref('bad-btn')});
        r.off = await m.run({do: 'click', ref: ref('off-link')});
        r.fill = await m.run({do: 'fill', ref: ref('name-in'), value: 'Ada'});
        r.pw = await m.run({do: 'fill', ref: ref('pw-in'), value: 'x'});
        r.point = await m.run({do: 'point', text: 'Refresh list'});
        r.pressed = pressed; r.name = document.getElementById('name-in').value;
        r.pwShown = JSON.stringify(snap).includes('hunter2');
        r.secretShown = /rk_SHOWN_ONCE_123|PAIR-CODE-9/.test(JSON.stringify(snap));
        location.hash = '#vault'; await new Promise(res => setTimeout(res, 800));
        const v = m.snapshot();
        r.locked = v.locked;
        r.vault = v.items.length ? await m.run({do: 'click', ref: v.items[0].ref}) : {ok: false, result: 'gates things'};
        return r; }""")
    assert out["ok"]["ok"] and out["pressed"] == 1, out
    assert not out["bad"]["ok"] and "for the person" in out["bad"]["result"]
    assert not out["off"]["ok"] and "leaves the dashboard" in out["off"]["result"]
    assert out["fill"]["ok"] and out["name"] == "Ada"
    assert not out["pw"]["ok"] and not out["pwShown"] and not out["secretShown"]
    assert out["point"]["ok"]
    assert out["locked"] and not out["vault"]["ok"] and "gates things" in out["vault"]["result"]
    assert page.errors == []
    page.context.close()


def test_settings_restart_button_top_right(browser, hub, monkeypatch):
    from rook.core import restart
    base, _ = hub
    stopped = []
    unregister = restart.on_restart(lambda: stopped.append(1))
    monkeypatch.setattr(restart, "supervised", lambda: None)
    monkeypatch.setattr(restart, "_requested", False)
    try:
        page = _signed_in(browser, base, viewport={"width": 1280, "height": 900})
        _open(page, base, "#settings")
        button = page.locator(".st-page .mg-bar .st-restart").first
        button.wait_for()
        bar = page.locator(".st-page .mg-bar").first.bounding_box()
        box = button.bounding_box()
        assert abs((box["x"] + box["width"]) - (bar["x"] + bar["width"])) <= 2, (box, bar)
        assert box["y"] - bar["y"] < 20                       # on the title row
        button.click()
        dialog = page.locator("dialog.mg-dialog", has_text="Restart the hub?")
        dialog.wait_for()
        _shot(page, "restart-confirm")
        dialog.locator("button", has_text="Cancel").click()
        assert not stopped and not restart.requested()
        button.click()
        page.locator("dialog.mg-dialog button.primary", has_text="Restart").click()
        page.locator(".st-page .mg-status", has_text="Restarting").wait_for()
        _shot(page, "restart-waiting")
        page.wait_for_timeout(500)
        assert stopped == [1] and restart.requested()
        assert page.errors == []
        page.context.close()
    finally:
        unregister()
