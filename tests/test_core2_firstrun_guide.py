"""The guided first run (Bake, Nov 9 2026): two panes - the work pane the
house agent drives and the chat with Rookie - clickable choices, the vault
for anything secret, one live Rookie that travels, the network and
other-machines steps in plain words, and the 0.4 brand."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from rook.core2.hub.doors.web import caps as web_caps
from rook.core2.hub.doors.web.site import SHELL_CSP
from tests.test_core2_dashboard import Client, run, site  # noqa: F401 - the fixture

STATIC = Path(web_caps.__file__).with_name("static")
REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "rook/plugins/agents/skills/first-run.md"


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    return node


PARSE = r"""
const W = require(process.argv[2]);
console.log(JSON.stringify(W.parse(process.argv[3])));
"""


def parse(tmp_path, text):
    script = tmp_path / "parse.js"
    script.write_text(PARSE)
    r = subprocess.run([_node(), str(script), str(STATIC / "chat-widgets.js"), text],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_agent_can_put_choices_and_work_pane_lines_in_a_message(tmp_path):
    text = ("How do you want to open this site?\n"
            "[[choices: Just this computer | Other devices at home | From anywhere]]\n"
            "[[open: vault | new=host_password | for=sudo on box]]\n"
            "[[open: page hub/workers]]\n"
            "  [[point: vault.value]]  \n"
            "Type it on the left.\n"
            "[[nonsense: x]]")
    assert parse(tmp_path, text) == [
        {"kind": "text", "text": "How do you want to open this site?"},
        {"kind": "choices", "items": ["Just this computer", "Other devices at home", "From anywhere"]},
        {"kind": "open", "view": "vault", "arg": "", "args": {"new": "host_password", "for": "sudo on box"}},
        {"kind": "open", "view": "page", "arg": "hub/workers", "args": {}},
        {"kind": "point", "target": "vault.value"},
        {"kind": "text", "text": "Type it on the left.\n[[nonsense: x]]"},     # unknown lines stay words
    ]
    # agents don't always give a block its own line: it counts anywhere
    assert parse(tmp_path, "Which style? [[choices: Pixel | Voxel]] or type one.") == [
        {"kind": "text", "text": "Which style?"}, {"kind": "choices", "items": ["Pixel", "Voxel"]},
        {"kind": "text", "text": "or type one."}]
    assert parse(tmp_path, "empty [[choices: ]] stays") == [{"kind": "text", "text": "empty [[choices: ]] stays"}]


def test_one_live_rookie_per_page():
    """Bake, Nov 9: "only one instance of it on any page at a time". The shell
    mounts exactly one (the chat's, which travels); the tab icon copies his
    canvas and the pickers draw still thumbnails (Rookie.bake); the account
    page mounts none."""
    shell = (STATIC / "shell.js").read_text()
    assert len(re.findall(r"\bRookie\.mount\(", shell)) == 1
    assert "Rookie.bake(" in shell
    for f in STATIC.glob("*.js"):
        if f.name == "rookie.js":
            continue
        code = f.read_text()
        assert not re.search(r"\bRookie\.favicon\(", code), f.name              # it mounts a hidden second one
        if f.name != "shell.js":
            assert not re.search(r"\bRookie\.mount\(", code), f.name
    # the one Rookie travels: a smooth path both ways, never placed at a new spot without one
    travel = shell[shell.index("function travel("):shell.index("function point(")]
    assert "transition" in travel and "cubic-bezier" in travel
    point = shell[shell.index("function point("):shell.index("\n}\n", shell.index("function point("))]
    assert point.count("travel(") == 2                                          # there and back


def test_the_vault_view_saves_secrets_and_lists_only_names(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            out = (await c.json("POST", "/web/api/secret", {"name": "host_password", "value": "hunter2",
                                                            "description": "sudo on box"}))[1]
            assert out == {"ok": True, "name": "host_password"}
            assert hub.vault.get("host_password") == "hunter2"
            listed = (await c.json("GET", "/web/api/secrets"))[1]
            assert [s["name"] for s in listed["secrets"]] == ["host_password"]
            assert "hunter2" not in json.dumps(listed)                         # names, never values
        finally:
            await hub.stop()
    run(go())


def test_the_first_run_skill_guides_and_makes_the_site_reachable():
    text = SKILL.read_text()
    guide = text[text.index("## How you guide"):text.index("## When you need root")]
    for line in ("[[choices:", "[[open:", "[[point:", "account.name", "vault.value", "network.home"):
        assert line in guide
    root = text[text.index("## When you need root"):text.index("1. **Sound.**")]
    assert "Never ask for a password in the chat" in root
    assert "new=host_password" in root and '"sudo", "-S"' in root and "stdin" in root
    assert "{{" not in text                         # a vault reference here would be filled in
    net = text[text.index("7. **Reaching this site.**"):text.index("8. **Other machines.**")]
    assert "[[choices: Just this computer | Other devices at home | From anywhere]]" in net
    assert "Directive" in net and "actually reachable" in net
    for tool in ("ufw", "firewall-cmd", "nft", "iptables"):
        assert tool in net
    assert "relay" in net and "udp" in net.lower() and "not localhost" in net and "/health" in net
    machines = text[text.index("8. **Other machines.**"):text.index("9. **")]
    assert machines.index("Rook worker") < machines.index("[[choices:")        # explain first, then ask
    assert "install.sh" in machines and "Android" in machines and "[[open: page hub/workers]]" in machines
    account = text[text.index("5. **Their name and a password.**"):text.index("6. **")]
    assert "[[open: account]]" in account


def test_the_site_wears_the_04_brand():
    css = (STATIC / "shell.css").read_text()
    for token in ("--bg: #101310", "--accent: #d8ad6d", "--good: #a4bc92", "'DM Sans'", "'IBM Plex Mono'"):
        assert token in css
    assert "fonts.googleapis.com" in SHELL_CSP and "fonts.gstatic.com" in SHELL_CSP
    for page in ("shell.html", "account.html", "login.html"):
        html = (STATIC / page).read_text()
        assert "fonts.googleapis.com/css2?family=DM+Sans" in html and 'class="mark"' in html
    assert '<section id="work"' in (STATIC / "shell.html").read_text()        # the work pane
