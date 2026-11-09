"""Bake, Nov 9: "The like workers background is missing. I liked the 3d rook
from the old version. ... make sure the default styles are using the old rook
style." And: "Also for the house agent chat, I want to have like an X at the
top right, and move the rookie avatar to the bottom right corner when
closed. Remove the chat button from the like top nav on pages. Clicking
rookie opens the chat sidebar. I want rookie to make little chat bubbles too
with short like "Right here" "Enter username and password" "The dashboard
task is done" etc"."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from rook.core2.hub.doors.web import caps as web_caps
from rook.core2.hub.doors.web import site as web_site
from tests.test_core2_dashboard import Client, run, site  # noqa: F401 - the fixture

STATIC = Path(web_caps.__file__).with_name("static")
REPO = Path(__file__).resolve().parents[1]


def _root_tokens(css: str) -> dict:
    block = re.search(r":root\s*\{([^}]*)\}", css).group(1)
    return dict((k.strip(), v.strip()) for k, v in (d.split(":", 1) for d in block.split(";") if ":" in d)
                if k.strip().startswith("--"))


def test_pages_default_to_the_04_rook_style():
    page = (STATIC / "page.css").read_text()
    theme = (REPO / "rook/web/theme.css").read_text()
    assert _root_tokens(page) == _root_tokens(theme)          # rook-dark, token for token
    assert "color-scheme:dark" in page and "prefers-color-scheme" not in page
    assert "body{margin:0;padding:16px 24px 40px;background:transparent" in page   # the drawing shows behind
    assert "border-radius:0" in page and "var(--sans)" in page
    # every page and component on the site leans on those tokens, no light theme of its own
    for f in [*REPO.glob("rook/core2/hub/doors/web/builtin/*/pages/*.html"), *REPO.glob("rook/plugins/*/pages/*.html")]:
        text = f.read_text()
        assert "prefers-color-scheme" not in text and "#fff" not in text.lower().replace("#ffff", ""), f
        assert not re.search(r"--bg:\s*#(?!101310)", text), f


def test_every_page_gets_the_brand_fonts_inline():
    out = web_site.inject("<html><head></head><body></body></html>", {"page": "x"})
    assert out.count("data:font/woff2;base64,") == 4 and "/web/static/fonts/" not in out
    assert out.index("@font-face") < out.index("--accent:#d8ad6d")
    web = web_site.Web.__new__(web_site.Web)               # _static/_file use no state
    status, headers, body = web._static("fonts/dm-sans-latin.woff2")
    assert status == 200 and headers["Content-Type"] == "font/woff2" and body[:4] == b"wOF2"
    for bad in ("fonts/../shell.js", "../x.woff2", "fonts/a/b.woff2", "x.woff2"):
        with pytest.raises(web_site._Error):
            web._static(bad)


def test_a_page_can_ask_for_the_3d_rook_library():
    out = web_site.inject('<html><head></head><body><script type="module" data-rook-lib="rook-scene"></script>'
                          '<script type="module" data-rook-lib="nope"></script></body></html>', {})
    assert 'window.RookLib=window.RookLib||{})["rook-scene"]={"mountFleet":' in out and '"mountRook":' in out
    assert "dispatchEvent(new CustomEvent('rooklib'" in out and "<!-- no library nope -->" in out
    assert not re.search(r"\bexport\s*\{", out)             # an inline module that exports nothing
    lib = out[out.index('<script type="module">'):]
    assert lib.count("</script>") >= 1 and "</script" not in lib[len('<script type="module">'):lib.index("</script>")]


def test_the_shell_draws_the_04_rook_behind_every_page():
    html = (STATIC / "shell.html").read_text()
    assert 'class="rook-art" id="rook-art"' in html and 'class="rook-sketch"' in html and "FIG. 01 / CASTELLUM" in html
    assert 'id="art-toggle"' in html and '<script type="module" src="/web/static/art.js"></script>' in html
    assert " style=" not in html                              # SHELL_CSP: no inline styles
    assert "rook-scene.js" in (STATIC / "art.js").read_text() and "mountRook" in (STATIC / "art.js").read_text()
    assert (STATIC / "rook-scene.js").read_bytes() == (REPO / "rook/web/rook-scene.js").read_bytes()
    css = (STATIC / "shell.css").read_text()
    assert ".rook-art { position: fixed; z-index: 0;" in css and "#stage iframe { flex: 1; border: 0; width: 100%; height: 100%; background: transparent" in css


def test_the_chat_has_an_x_and_no_chat_button():
    html = (STATIC / "shell.html").read_text()
    head = html[html.index('<div id="dock-head">'):html.index("</div>", html.index('id="dock-name"'))]
    assert head.rstrip().endswith('<button id="dock-close" aria-label="Close the chat" title="Close the chat">&times;</button>')
    bar = html[html.index('<header id="bar">'):html.index("</header>")]
    assert '<button id="dock-btn" class="offscreen"' in bar        # never shown; the desktop app clicks it
    assert ".offscreen { display: none !important; }" in (STATIC / "shell.css").read_text()
    js = (STATIC / "shell.js").read_text()
    assert '$("#dock-btn").onclick = () => dockOpen(true);' in js
    assert '$("#dock-skip").hidden = !on;' in js                  # setup: "Skip to the hub" beside the X


def test_rookie_waits_in_the_corner_and_opens_the_chat():
    js = (STATIC / "shell.js").read_text()
    open_ = js[js.index("function dockOpen("):js.index("function scrollChat(")]
    assert "liftOff()" in open_ and "goHome()" in open_
    assert '$("#rookie-park").getBoundingClientRect()' in js and 'h.classList.add("parked")' in js
    assert 'rookie.holder.classList.contains("parked") && rookie.holder.contains(e.target)) dockOpen(true)' in js
    assert "document.body.append(h);" in js                       # out of the chat, so he stays drawn when it closes
    html = (STATIC / "shell.html").read_text()
    assert html.count("Rookie.mount") == 0 and js.count("Rookie.mount(") == 1      # still one Rookie
    css = (STATIC / "shell.css").read_text()
    assert "#rookie-park { position: fixed; right: 18px; bottom: 16px;" in css and ".rookie.parked { pointer-events: auto; cursor: pointer; }" in css


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    return node


def test_rookies_speech_bubbles(tmp_path):
    js = (STATIC / "shell.js").read_text()
    funcs = "".join(js[js.index(f"function {n}("):js.index("\n}\n", js.index(f"function {n}(")) + 3]
                    for n in ("shortLine", "sayFor"))
    script = tmp_path / "t.js"
    script.write_text(funcs + """
const out = {
  short: shortLine("Done! I set up **the dashboard** for you. [[open: page hub/workers]]"),
  long: shortLine("This reply goes on for many many words without any stop at all here"),
  task: sayFor({phase: "end", cap: "task.update", ok: true, args: {id: "t1", patch: {state: "done"}}}),
  page: sayFor({phase: "end", cap: "page.create", ok: true, args: {}}),
  held: sayFor({phase: "end", cap: "shell.exec", decision: "held"}),
  start: sayFor({phase: "start", cap: "page.create"}),
  read: sayFor({phase: "end", cap: "knowledge.search", ok: true, args: {}}),
};
console.log(JSON.stringify(out));
""")
    got = json.loads(subprocess.run([_node(), str(script)], capture_output=True, text=True, check=True).stdout)
    assert got == {"short": "Done!", "long": "This reply goes on for many many…",
                   "task": "That task is done", "page": "Your new page is ready", "held": "I need your OK",
                   "start": "", "read": ""}
    bubble = js[js.index("function bubble("):js.index("function retire(")]
    assert 'part.kind === "say"' in bubble and "speak(shortLine(text), seq)" in bubble and "chatClosed()" in bubble
    point = js[js.index("function point("):js.index("function thinking(")]
    assert 'speak(said || "Right here")' in point
    say = js[js.index("$(\"#rookie-say\").onclick"):]
    assert "dockOpen(true)" in say and "[data-seq=" in say
    skill = (REPO / "rook/plugins/agents/skills/first-run.md").read_text()
    assert "[[say: a few words]]" in skill and "[[point: TARGET | a few words]]" in skill


def test_the_workers_page_has_the_3d_fleet(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            frame = (await c.open("hub/workers"))["frame"]
            status, headers, body = await c.req("GET", frame)
            text = body.decode()
            assert status == 200 and 'window.RookLib=window.RookLib||{})["rook-scene"]' in text
            assert "data:font/woff2;base64," in text and "lib.mountFleet(host" in text
            assert "script-src 'unsafe-inline'" in headers["Content-Security-Policy"]
        finally:
            await hub.stop()
    run(go())
