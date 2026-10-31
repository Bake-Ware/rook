"""The Rook 0.5 manual (docs/manual-0.5) and the plugin manuals: pages are
listed and link where they say, manifest examples validate, every plugin has
a manual, and what people type is documented (tools/manual05_check.py)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import manual05_check as mc  # noqa: E402


def test_pages_are_listed_and_their_links_resolve():
    assert mc.page_problems() == []


def test_manifest_examples_validate():
    names = {b.split("plugin:", 1)[1].split()[0] for _, b in mc.manifest_blocks()}
    assert {"hello", "deluge", "pikvm", "uptime", "homework"} <= names
    assert mc.example_problems() == []


def test_every_plugin_has_a_manual_listed_in_plugins_md():
    found = mc.plugins()
    assert len(found) >= 30 and {"shell", "agents", "approval", "pages"} <= {n for n, _, _ in found}
    assert mc.plugin_problems() == []


def test_config_keys_commands_routes_tools_and_presets_are_documented():
    inv = mc.inventory()
    whats = [w for w, _ in inv["reference.md"]]
    assert whats.count("hub.yaml key") >= 10 and whats.count("worker.yaml key") >= 8
    assert whats.count("HTTP route") >= 15 and "hub command" in whats and "worker command" in whats
    assert len(inv["mcp.md"]) == 7 and len(inv["people.md"]) == 4
    assert mc.coverage_problems() == []


def test_the_checks_catch_problems(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Manual\n\n[A](a.md) [Gone](gone.md)\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "# A\n\n## Real heading\n\n[ok](#real-heading) [bad](#nope) [missing](b.md) `rook/no/such.py`\n\n"
        "```yaml\nmanifest: 1\nplugin: broken\nversion: 1.0.0\ncaps:\n  x: { doc: X. }\n```\n",
        encoding="utf-8")
    (tmp_path / "loose.md").write_text("no title\n", encoding="utf-8")
    monkeypatch.setattr(mc, "MANUAL", tmp_path)
    pages = "\n".join(mc.page_problems())
    for needle in ("README.md: lists gone.md", "loose.md: doesn't start", "loose.md: not listed",
                   "#nope: no heading", "b.md: no such file", "`rook/no/such.py` doesn't exist"):
        assert needle in pages
    assert "#real-heading" not in pages
    assert any("example broken" in p for p in mc.example_problems())
