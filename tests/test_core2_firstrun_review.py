"""Bake's review pass of the first run (Nov 9, "Ok do a pass on this as
fable please"): site notes stay out of the chat, the agent's own name in
the UI, views show when they're done, the look and feel on the left and
instant, no Mac, no decision quotes in the shipped skill, focus after a
reload, "No such page", a favicon without sign-in, a quiet installer, the
Rook version on the Workers page, and the sign-in name in the account view."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from rook.core2 import ROOK_VERSION
from rook.core2.hub.doors.web import caps as web_caps
from tests.test_core2_dashboard import Client, run, site  # noqa: F401 - the fixture

STATIC = Path(web_caps.__file__).with_name("static")
REPO = Path(__file__).resolve().parents[1]
SHELL = (STATIC / "shell.js").read_text()
SKILL = (REPO / "rook/plugins/agents/skills/first-run.md").read_text()


def _fn(name: str) -> str:
    start = SHELL.index(f"function {name}(")
    return SHELL[start:SHELL.index("\n}\n", start) + 3]


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    return node


def test_site_notes_never_show_as_the_persons_words():
    show = _fn("show")
    assert "if (!rest) return;" in show and "rest.startsWith(SHOWN)" in show
    assert 'bubble("aside"' not in show                                    # no banner of the note either
    say = SHELL[SHELL.index("async function say("):SHELL.index("// Setup mode:")]
    assert "`\\n${SHOWN} ${shown}`" in say
    assert '"Saved my account"' in SHELL and "The person answered look and feel" not in SHELL


def test_ui_text_uses_the_agents_name():
    for f in [*STATIC.glob("*.js"), *STATIC.glob("*.html")]:
        if f.name in ("rookie.js", "rook-scene.js"):
            continue
        text = f.read_text()
        for line in text.splitlines():
            code = line.split("//")[0]
            assert not re.search(r"""["'`>][^"'`<]*\bRookie (uses|finds|is|will)\b""", code), (f.name, line)
    assert "agentName()} uses them by name" in SHELL and "(${agentName()} finds it)" in SHELL
    assert 'class="agent-name"' in (STATIC / "account.html").read_text()


def test_a_view_shows_when_its_job_is_done():
    vault = SHELL[SHELL.index("  async vault("):SHELL.index("  async account(")]
    account = SHELL[SHELL.index("  async account("):SHELL.index("  network(")]
    for view, what in ((vault, "vault.done"), (account, "account.done")):
        assert f'doneCard("{what}"' in view and "done(" in view[view.index("onsubmit"):]
    assert "names.includes(wanted)" in vault                                   # a reload: saved already
    assert "if (me.password) return done(" in account


def test_the_look_and_feel_is_on_the_left_and_instant(tmp_path):
    look = SHELL[SHELL.index("  look() {"):SHELL.index("// The welcome view")]
    assert 'id: "look-spot"' in look and "goHome" in look                    # he walks onto the stage
    assert 'if (onStage()) return $("#look-spot").getBoundingClientRect();' in SHELL
    css = (STATIC / "shell.css").read_text()
    assert ".rookie.staged { transform: scale(var(--stage-scale));" in css
    assert len(re.findall(r"\bRookie\.mount\(", SHELL)) == 1                  # still one live Rookie
    answer = SHELL[SHELL.index("async function lookAnswer("):SHELL.index("function lookDone(")]
    assert 'call("page.look"' in answer and "say(" not in answer             # no agent turn per click
    assert "lookStep(out.next)" in answer and "lookDone(out)" in answer
    done = SHELL[SHELL.index("function lookDone("):SHELL.index("function lookStart(")]
    assert "lookDone.sent" in done and "kickoff()" in done                   # the agent hears once
    start = SHELL[SHELL.index("async function startDock("):]
    assert start.index("lookStart()") < start.index("await kickoff()")
    submit = SHELL[SHELL.index('$("#dock-form").onsubmit'):]
    assert "setupBrief(look)" in submit[:submit.index("};")]                # typed instead: the agent hears now
    script = tmp_path / "t.js"
    script.write_text(_fn("lookSaid") + _fn("lookLine") + _fn("lookSoFar") + """
const look = {name: "Juniper", sound: false, rookie: "pixel", theme: "stone", chosen: {}};
console.log(JSON.stringify({said: [lookSaid("sound", true), lookSaid("sound", false), lookSaid("rookie", "none"),
  lookSaid("theme", "amber")], first: lookLine("sound", look), name: lookLine("name", look), so: lookSoFar(look)}));
""")
    got = json.loads(subprocess.run([_node(), str(script)], capture_output=True, text=True, check=True).stdout)
    assert got["said"] == ["Sound on", "No sound", "No avatar", "amber"]
    assert got["first"].startswith("Hi! I'm Juniper, your house agent.") and "sound" in got["first"]
    assert got["name"] == "What should I be called? Type a name, or keep Juniper."
    assert got["so"] == "sound off, style pixel, theme stone, name Juniper"


def test_the_skill_has_no_mac_and_no_decision_quotes():
    assert "Mac" not in SKILL and "macOS" not in SKILL
    assert "(Bake," not in SKILL and "Bake, Nov" not in SKILL
    assert "[[choices: A Linux computer | A Windows PC | An Android phone | Not now]]" in SKILL
    sh = (REPO / "deploy/install/install.sh").read_text()
    assert 'die "this installer is for Linux."' in sh and "macOS" not in sh


def test_the_vault_endpoint_is_the_vault_views():
    site_py = (STATIC.parent / "site.py").read_text()
    assert "[[secret" not in site_py and "The Vault work view's Save" in site_py
    assert 'api("/web/api/secret"' in SHELL                                  # still used: not dead


def test_a_reload_focuses_the_field_again():
    assert "work.focus.focus()" in _fn("dockOpen")
    assert SHELL.count("focusWork(") >= 4


def test_an_address_with_no_page_says_so():
    op = SHELL[SHELL.index("async function open(pid)"):SHELL.index("function closePage(")]
    assert "No such page" in op and "message(`No such page: ${pid}.`, true, true)" in op
    assert "Go to the start page" in _fn("message")


def test_the_favicon_needs_no_sign_in(site):
    async def go():
        hub, _ = await site()
        try:
            status, headers, body = await Client(hub).req("GET", "/favicon.ico")
            assert status == 200 and headers["Content-Type"] == "image/x-icon" and body[:4] == b"\x00\x00\x01\x00"
        finally:
            await hub.stop()
    run(go())
    for page in ("shell.html", "account.html", "login.html", "public.html"):
        assert '<link rel="icon" href="/web/static/favicon.svg"' in (STATIC / page).read_text(), page


def test_the_installer_says_only_what_a_new_user_needs():
    sh = (REPO / "deploy/install/install.sh").read_text()
    assert not re.search(r"""^printf '%s\\n' "\$initout"$""", sh, re.M)      # no developer lines, no API token
    assert 'say "Hub: $URL"' not in sh                                       # the URL once, at the end
    end = sh[sh.index('step "Your recovery key"'):]
    assert "brings this hub back" in end and '"$RECOVERY"' in end and end.index("$RECOVERY") < end.index("open_site")
    out = ("Rook hub x set up.\n\nAPI token for bake's apps:\n  TOK\n\nRecovery key: save it.\n"
           "while it lives, again.\n  AAAA-BBBB\n\nStart it:  python -m x\n")
    awk = re.search(r"RECOVERY=\$\(printf '%s\\n' \"\$initout\" \| awk '([^']+)'\)", sh)[1]
    assert subprocess.run(["awk", awk], input=out, capture_output=True, text=True).stdout.strip() == "AAAA-BBBB"


def test_the_workers_page_shows_the_rook_version(site):
    async def go():
        hub, _ = await site()
        try:
            ws = (await hub.workers_view(None))["workers"]
            assert ws[0]["hub"] and ws[0]["version"] == ROOK_VERSION and ROOK_VERSION.startswith("0.5.")
        finally:
            await hub.stop()
    run(go())
    page = (REPO / "rook/core2/hub/doors/web/builtin/hub/pages/workers.html").read_text()
    assert 'function versionLabel(w) { return w.version ? "v" + w.version : ""; }' in page
    from rook.core2.worker import runtime
    assert runtime._version() == ROOK_VERSION                                 # not the package's 0.1.0


def test_the_account_view_says_the_sign_in_name():
    account = SHELL[SHELL.index("  async account("):SHELL.index("  network(")]
    assert "You'll sign in as: " in account and "userName()" in account
    assert "They sign in as ${user}" in account
    step = SKILL[SKILL.index("5. **Their name and a password.**"):SKILL.index("6. **")]
    assert "user name" in step and "never say they sign in with the name they" in step.replace("\n   ", " ")
