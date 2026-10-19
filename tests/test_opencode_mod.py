"""The OpenCode integration: command files and MCP setup guidance."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "integrations" / "opencode"


def test_commands_have_open_code_frontmatter_and_descriptions():
    commands = sorted((MOD / "commands").glob("*.md"))
    assert {path.stem for path in commands} == {"rook-bands", "rook-deck", "rook-groom"}
    for path in commands:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\ndescription: ")
        assert "\n---\n" in text
        assert "rook" in text


def test_mcp_setup_uses_remote_bearer_auth_without_committing_a_token():
    readme = (MOD / "README.md").read_text(encoding="utf-8")
    config = json.loads(readme.split("```json\n", 1)[1].split("\n```", 1)[0])
    rook = config["mcp"]["rook"]
    assert rook["type"] == "remote"
    assert rook["enabled"] is True
    assert rook["oauth"] is False
    assert rook["headers"]["Authorization"] == "Bearer {env:ROOK_MCP_STATIC_TOKEN}"
    assert "does not mirror OpenCode session transcripts" in readme


def test_root_readme_links_opencode_integration():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "integrations/opencode" in readme
