"""The Claude Code mod: marketplace layout, rook_install_claude_code, scrubbed fixtures."""
import json
import re
from pathlib import Path

import pytest

from rook.band_mcp import claude_code
from rook.band_mcp.server import build_server
from rook.hub.authz import hub_cap_for_tool

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "integrations" / "claude-code"
STATIC = "static-token-0123456789abcdef"
LATEST = claude_code.manifest()["version"]


def _listing(*rows) -> str:
    return json.dumps(list(rows), indent=2)


def test_marketplace_lists_the_mod_at_its_version():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    plugin = json.loads((MOD / ".claude-plugin" / "plugin.json").read_text())
    assert market["name"] == claude_code.MARKETPLACE
    (entry,) = [p for p in market["plugins"] if p["name"] == claude_code.PLUGIN]
    assert (ROOT / entry["source"]).resolve() == MOD.resolve()
    # A release bumps both; Claude Code updates a git plugin by its version.
    assert entry["version"] == plugin["version"] == LATEST
    for path in claude_code.SPARSE:  # the sparse checkout holds everything the entry needs
        assert (ROOT / path).is_dir()
    assert (MOD / "hooks" / "hooks.json").is_file()


def test_mod_files_name_no_real_hosts_paths_or_ids():
    for p in MOD.rglob("*"):
        if not p.is_file() or ".claude-plugin/types" in p.as_posix():
            continue
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"/home/(?!user\b)[a-z]", text), p
        assert not re.search(r"\b(?:192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+", text), p
        assert not re.search(r"rmcp-|\b[0-9a-f]{32,}\b", text), p


def test_status_without_a_listing_gives_install_steps_and_how_to_check():
    out = claude_code.status()
    assert out["status"] == "unknown" and out["latest"] == LATEST
    assert out["steps"] == [claude_code.LIST, claude_code.ADD, claude_code.INSTALL]
    assert claude_code.ADD == ("claude plugin marketplace add Bake-Ware/rook "
                               "--sparse .claude-plugin integrations/claude-code")
    assert claude_code.INSTALL == "claude plugin install rook@rook"
    assert "installed=" in out["notes"][0]


def test_status_not_installed():
    other = {"id": "clangd-lsp@claude-plugins-official", "version": "1.0.0"}
    out = claude_code.status(_listing(other))
    assert out["status"] == "not_installed"
    assert out["steps"] == [claude_code.ADD, claude_code.INSTALL]
    assert "/reload-plugins" in out["notes"][0]


def test_status_outdated_current_and_newer():
    old = claude_code.status(_listing({"id": "rook@rook", "version": "0.0.1", "enabled": True}))
    assert old["status"] == "outdated" and old["installed"] == "0.0.1"
    assert old["steps"] == ["claude plugin marketplace update rook", "claude plugin update rook@rook"]

    same = claude_code.status(_listing({"id": "rook@rook", "version": LATEST}))
    assert same["status"] == "current" and same["steps"] == []

    ahead = claude_code.status(_listing({"id": "rook@rook", "version": "999.0.0"}))
    assert ahead["status"] == "newer" and ahead["steps"] == []

    assert claude_code.status("0.0.1")["status"] == "outdated"  # a bare version
    assert claude_code.status("v" + LATEST)["status"] == "current"


def test_status_flags_a_second_copy_and_a_disabled_install():
    out = claude_code.status(_listing(
        {"id": "rook@skills-dir", "version": "0.1.0", "installPath": "/home/user/.claude/skills/rook"},
        {"id": "rook@rook", "version": LATEST, "enabled": False}))
    notes = " ".join(out["notes"])
    assert out["status"] == "current"
    assert "rook@skills-dir at /home/user/.claude/skills/rook" in notes and "Ask the user" in notes
    assert "claude plugin enable rook@rook" in notes

    only_local = claude_code.status(_listing({"id": "rook@skills-dir", "version": "0.1.0"}))
    assert only_local["status"] == "not_installed"


def test_status_reads_a_listing_with_noise_and_ignores_garbage():
    noisy = "Loading plugins…\n" + _listing({"id": "rook@rook", "version": "0.0.1"}) + "\n"
    assert claude_code.status(noisy)["status"] == "outdated"
    assert claude_code.status("not json at all")["status"] == "unknown"
    assert claude_code.status('{"id": "rook@rook"}')["status"] == "unknown"


def test_tool_is_a_read_on_the_hub():
    assert hub_cap_for_tool("rook_install_claude_code", {}) == "hub.info"


class FakeBand:
    workers: dict = {}

    async def call(self, *a, **k):
        return {"ok": True, "result": {}}


@pytest.mark.asyncio
async def test_hub_mcp_serves_the_tool(tmp_path):
    mcp, _ = build_server(FakeBand(), persist_path=str(tmp_path / "tokens.json"),
                          static_token=STATIC, journal_path=str(tmp_path / "journal.db"))
    tools = {t.name: t for t in await mcp.list_tools()}
    assert "claude plugin list --json" in tools["rook_install_claude_code"].description

    def text(result):
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    fresh = text(await mcp.call_tool("rook_install_claude_code", {}))
    assert fresh["ok"] and fresh["status"] == "unknown" and fresh["plugin"] == "rook@rook"
    old = text(await mcp.call_tool("rook_install_claude_code",
                                   {"installed": _listing({"id": "rook@rook", "version": "0.0.1"})}))
    assert old["status"] == "outdated" and old["latest"] == LATEST
    parsed = text(await mcp.call_tool("rook_install_claude_code",
                                      {"installed": [{"id": "rook@rook", "version": LATEST}]}))
    assert parsed["status"] == "current"
    bare = text(await mcp.call_tool("rook_install_claude_code", {"installed": "0.0.1"}))
    assert bare["status"] == "outdated"
