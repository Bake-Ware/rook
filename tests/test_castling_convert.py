"""castling/tools/convert.py: draft 0.5 manifests from today's plugins."""

from __future__ import annotations

import ast
import collections
import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("castling_convert", REPO / "castling/tools/convert.py")
convert = importlib.util.module_from_spec(_spec)
sys.modules["castling_convert"] = convert
_spec.loader.exec_module(convert)


@pytest.fixture(scope="module")
def drafted():
    caps, plugins = convert.scan()
    decisions = convert.load_decisions()
    mans, rows = convert.build(caps, plugins, decisions)
    convert.apply_overlays(mans)
    return caps, decisions, mans, rows


def test_every_surviving_cap_is_found_in_source(drafted):
    _, _, _, rows = drafted
    missing = [r["cap"] for r in rows if r["decision"] != "bin" and not r["found"]]
    assert missing == []
    assert len(rows) == 457


def test_manifests_pass_the_checks(drafted):
    _, _, mans, _ = drafted
    errs = [e for m in mans.values() for e in convert.lint(m)]
    assert errs == []


def test_every_new_name_lands_and_binned_names_do_not(drafted):
    _, decisions, mans, _ = drafted
    declared = set()
    for name, m in mans.items():
        declared |= {f"{name}.{k}" if k else name for k in m.get("caps", {})}
        declared |= set(m.get("provides", {}))
        for rec, spec in m.get("records", {}).items():
            prefix = name if rec == name else f"{name}.{rec}"
            declared |= {f"{prefix}.{verb}" for verb in spec["caps"]}
    wanted = {n for d in decisions if d["decision"] != "bin" for n in d["new_names"]}
    # Contract caps are declared twice (plugin.contract.verb and the contract name).
    assert wanted - declared == set()
    survivors = {n for d in decisions if d["decision"] != "bin" for n in d["new_names"]}
    binned_only = {d["cap"] for d in decisions if d["decision"] == "bin"} - survivors
    assert not (binned_only & {f"{p}.{k}" for p, m in mans.items() for k in m.get("caps", {})})


def test_decide_keeps_its_drive_loop(drafted):
    """Bake amended call C3: decide keeps run, status and drive."""
    _, _, mans, _ = drafted
    assert {"run", "status", "drive"} <= set(mans["decide"]["caps"])


def test_aliases_follow_the_decisions(drafted):
    """An old name is an alias only on a cap the decisions route it to."""
    _, decisions, mans, _ = drafted
    routes = collections.defaultdict(set)
    for d in decisions:
        for n in d["new_names"]:
            routes[d["cap"]].add(n)
    for name, m in mans.items():
        for key, cap in m.get("caps", {}).items():
            full = key if key.split(".")[0] in convert.CONTRACTS and name != key.split(".")[0] \
                else (f"{name}.{key}" if key else name)
            for a in cap.get("aliases", []):
                assert full in routes[a], (a, full)
        for rec, spec in m.get("records", {}).items():
            prefix = name if rec == name else f"{name}.{rec}"
            for a in spec.get("aliases", []):
                assert any(n.startswith(prefix + ".") for n in routes[a]), (a, prefix)


def test_tags_become_effects_and_destructive_splits(drafted):
    _, _, mans, _ = drafted
    torrent = mans["torrent"]["caps"]
    assert "destructive" in torrent["delete"]["effects"]
    assert "effects" not in torrent["remove"]
    assert "rm --remove_data" in torrent["delete"]["action"]["exec"]["argv"][1]
    for m in mans.values():
        for cap in m.get("caps", {}).values():
            assert "tags" not in cap or not set(cap["tags"]) & convert.EFFECT_TAGS


def test_easy_plugins_are_config(drafted):
    _, _, mans, _ = drafted
    config = {name for name, m in mans.items()
              if any("action" in c for c in m.get("caps", {}).values())}
    assert {"kvm", "torrent", "notification-desktop", "battery", "cec", "screen-desktop",
            "camera"} <= config
    assert mans["kvm"]["provides"]["screen.capture"] == "kvm.screen.capture"
    assert "appliance" in mans["kvm"] and "settings" not in mans["kvm"]


def test_torrent_patterns_refuse_console_breakout(drafted):
    import re
    _, _, mans, _ = drafted
    pat = re.compile(mans["torrent"]["caps"]["remove"]["args"]["torrent_id"]["pattern"])
    for ok in ("ab12", "ab12 cd34", "ubuntu.iso"):
        assert pat.fullmatch(ok), ok
    for bad in ("x; rm *", "x --remove_data", "-x", "a'b", "*", "a\nb"):
        assert not pat.fullmatch(bad), bad


def test_committed_manifests_are_current(drafted, tmp_path):
    """castling/manifests is the converter's output; regenerate after edits."""
    _, _, mans, _ = drafted
    convert.write(mans, tmp_path)
    fresh = {p.parent.name: p.read_text() for p in tmp_path.glob("*/plugin.yaml")}
    committed = {p.parent.name: p.read_text() for p in convert.OUT.glob("*/plugin.yaml")}
    assert fresh == committed, "run python3 castling/tools/convert.py"
    for text in committed.values():
        yaml.safe_load(text)


def test_scan_reads_decorators_args_and_settings(tmp_path):
    src = tmp_path / "rook/worker/plugins/demo.py"
    src.parent.mkdir(parents=True)
    src.write_text(textwrap.dedent('''
        class Demo(Plugin):
            NAMESPACE = "demo"
            SETTINGS = (setting("host", str, default="", scope="worker", help="Where."),
                        setting("key", str, secret=True, scope="hub"))
            def heartbeat(self): ...
            @capability("do.it", risk="write", scope="local", tags=("physical", "media"), timeout=12)
            async def _do(self, name: str, n: int = 3, flag: bool | None = None,
                          api_token: str = "", **_):
                """Do it.

                Args:
                    name: who to do.
                """
    '''))
    for _, root in convert.ROOTS[1:]:
        (tmp_path / root).mkdir(parents=True)
    caps, plugins = convert.scan(tmp_path)
    cap = caps[("worker", "demo", "demo.do.it")]
    assert cap["doc"] == "Do it."
    assert cap["args"] == {"name": {"type": "str", "required": True, "doc": "who to do."},
                           "n": {"type": "int", "default": 3}, "flag": {"type": "bool"},
                           "api_token": {"type": "secret", "default": ""}}
    assert cap["open_args"] and cap["timeout"] == 12 and cap["tags"] == ["physical", "media"]
    p = plugins[("worker", "demo")]
    assert p["settings"] == {"host": {"type": "str", "where": "worker", "doc": "Where."},
                             "key": {"type": "str", "where": "hub", "secret": True}}
    assert p["hooks"] == {"heartbeat"}


def test_merge_overlay_rules():
    base = {"caps": {"x": {"doc": "a", "run": "scripts/x.py:f", "args": {"a": {}, "b": {}}}}}
    convert.merge(base, {"caps": {"x": {"action": {"exec": {"argv": ["true"]}}, "args": {"b": None}}}})
    assert base == {"caps": {"x": {"doc": "a", "action": {"exec": {"argv": ["true"]}}, "args": {"a": {}}}}}


def test_lint_catches_undeclared_templates():
    m = {"manifest": 1, "plugin": "demo", "version": "1.0.0",
         "caps": {"x": {"doc": "d", "risk": "read", "scope": "local",
                        "action": {"exec": {"argv": ["{{args.nope}}", "{{settings.gone}}"]}}}}}
    errs = convert.lint(m)
    assert any("args.nope" in e for e in errs)
    assert any("settings.gone" in e for e in errs)
    assert any("risk exec" in e for e in errs)
