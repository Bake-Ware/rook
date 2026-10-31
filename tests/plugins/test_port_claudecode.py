"""claudecode through the worker host: install merges Rook's hooks into a
scratch Claude Code config (rules come from a fake hub's claudecode.list),
the installed hook answers real hook input, remove restores the file. Every
test uses scratch folders; nothing touches a real settings file."""

import json
import subprocess
import sys

import pytest

from port_helpers import CONTRACTS, ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host
from rook.kit.manifest import check, load

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in the hook command")

USER = {"model": "opus",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]},
        "env": {"A": "1"}}
USER_TEXT = json.dumps(USER, indent=4) + "\n"     # their own style: indent 4

RULES = [
    {"id": "r1", "name": "No force delete", "enabled": True, "event": "PreToolUse", "matcher": "Bash",
     "contains": "rm -rf", "scope": "all", "actions": [{"type": "block", "text": "No rm -rf in {project}."}]},
    {"id": "r2", "name": "Status", "enabled": True, "event": "UserPromptSubmit", "scope": "project",
     "target": "rook", "actions": [{"type": "statusline", "text": "rook: {event}"},
                                   {"type": "inject", "text": "Mind the castling branch."}]},
]


@pytest.fixture
def claude(tmp_path, monkeypatch):
    d = tmp_path / "claude"
    d.mkdir()
    (d / "settings.json").write_text(USER_TEXT)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    monkeypatch.delenv("ROOK_CCMOD_CLAUDE_DIRS", raising=False)
    return d


def run(tmp_path, cap, args=None, rules=RULES):
    asked = []

    async def hub(cap_, args_, caller):
        asked.append((cap_, args_, caller.person))
        assert cap_ == "claudecode.list"
        return [r for r in rules if r["enabled"]]

    async def body():
        host = Host(os_name="linux", data_root=tmp_path / "data", dispatch=hub)
        await host.load(PLUGINS / "claudecode")
        try:
            return await host.call(f"claudecode.{cap}", args or {}, ME), asked
        finally:
            await host.close()
    return go(body)


def test_manifest_and_record_caps():
    data, issues = check(PLUGINS / "claudecode", CONTRACTS)
    assert data is not None and [str(i) for i in issues] == []
    caps = load(PLUGINS / "claudecode", CONTRACTS).caps
    assert caps["claudecode.install"]["on"] == "worker"


def test_rules_are_hub_records_and_writing_them_is_admin():
    from rook.core2.hub.plugins import HubPlugins
    from rook.kit.contracts import Contracts
    from rook.kit.records import RecordStore

    async def body():
        hub = HubPlugins(records=RecordStore(), contracts=Contracts(), call=None)
        await hub.load(PLUGINS / "claudecode")
        caps = hub.caps()
        assert {c: caps[c]["risk"] for c in ("claudecode.list", "claudecode.create", "claudecode.update",
                                             "claudecode.delete")} == {
            "claudecode.list": "read", "claudecode.create": "admin", "claudecode.update": "admin",
            "claudecode.delete": "admin"}
        assert "claudecode.install" not in caps                       # a worker cap
        made = await hub.run("claudecode.create", {"name": "x", "event": "Stop",
                                                   "actions": [{"type": "inject", "text": "go on"}]}, ME)
        got = await hub.run("claudecode.list", {"where": {"enabled": True}}, ME)
        assert [r["id"] for r in got] == [made["id"]] and got[0]["scope"] == "all"
        with pytest.raises(Exception, match="bad args"):
            await hub.run("claudecode.create", {"name": "x", "event": "Stop",
                                                "actions": [{"type": "post", "text": "no"}]}, ME)
        await hub.close()
    go(body)


def test_install_merges_beside_the_users_hooks_and_remove_restores_them(tmp_path, claude):
    dry, asked = run(tmp_path, "install", {"dry_run": True})
    assert asked == [("claudecode.list", {"where": {"enabled": True}, "limit": 500}, ME.person)]
    assert dry["dry_run"] and dry["events"] == ["PreToolUse", "UserPromptSubmit"] and dry["statusline"]
    assert (claude / "settings.json").read_text() == USER_TEXT          # nothing written
    assert "rook-ccmod-hook.py" in dry["installations"][0]["diff"]

    out, _ = run(tmp_path, "install")
    assert out["installations"][0]["action"] == "update" and out["installations"][0]["backup"]
    now = json.loads((claude / "settings.json").read_text())
    assert now["model"] == "opus" and now["env"] == {"A": "1"}
    groups = now["hooks"]["PreToolUse"]
    assert groups[0] == USER["hooks"]["PreToolUse"][0]                 # theirs first, untouched
    assert groups[1]["matcher"] == "Bash" and "rook-ccmod-hook.py" in groups[1]["hooks"][0]["command"]
    assert now["statusLine"]["command"].endswith("statusline || exit 0")
    script = tmp_path / "data" / "claudecode" / "rook-ccmod-hook.py"
    assert script.is_file() and (script.stat().st_mode & 0o777) == 0o700
    st, _ = run(tmp_path, "status")
    assert st["rules"] == 2 and st["installations"][0]["managed"]
    assert st["installations"][0]["events"] == ["PreToolUse", "UserPromptSubmit"]

    again, _ = run(tmp_path, "install")
    assert again["installations"][0]["action"] == "unchanged"

    gone, _ = run(tmp_path, "remove")
    assert gone["installations"][0].get("restored_original")
    assert (claude / "settings.json").read_text() == USER_TEXT          # byte for byte
    assert not script.exists() and not (tmp_path / "data" / "claudecode" / "rules.json").exists()


def test_no_enabled_rules_takes_rook_out(tmp_path, claude):
    run(tmp_path, "install")
    out, _ = run(tmp_path, "install", rules=[{**r, "enabled": False} for r in RULES])
    assert out["rules"] == 0 and (claude / "settings.json").read_text() == USER_TEXT


def test_a_broken_settings_file_is_refused_not_rewritten(tmp_path, claude):
    (claude / "settings.json").write_text("{not json")
    out, _ = run(tmp_path, "install")
    row = out["installations"][0]
    assert row["action"] == "error" and "not valid JSON" in row["error"]
    assert (claude / "settings.json").read_text() == "{not json"


def test_the_installed_hook_answers_claude_code(tmp_path, claude):
    run(tmp_path, "install")
    script = tmp_path / "data" / "claudecode" / "rook-ccmod-hook.py"
    project = tmp_path / "src" / "rook"
    (project / ".git").mkdir(parents=True)

    def hook(arg, event):
        p = subprocess.run([sys.executable, "-I", "-S", str(script), arg], input=json.dumps(event),
                           capture_output=True, text=True, timeout=20)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else None

    denied = hook("PreToolUse", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1",
                                 "cwd": str(project), "tool_input": {"command": "rm -rf /"}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denied["hookSpecificOutput"]["permissionDecisionReason"] == "No rm -rf in rook."
    assert hook("PreToolUse", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(project),
                               "tool_input": {"command": "ls"}}) is None
    told = hook("UserPromptSubmit", {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                     "cwd": str(project), "prompt": "hi"})
    assert told["hookSpecificOutput"]["additionalContext"] == "Mind the castling branch."
    p = subprocess.run([sys.executable, "-I", "-S", str(script), "statusline"],
                       input=json.dumps({"session_id": "s1"}), capture_output=True, text=True, timeout=20)
    assert p.stdout.strip() == "rook: UserPromptSubmit"
    # Elsewhere the project rule stays quiet.
    assert hook("UserPromptSubmit", {"hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path),
                                     "prompt": "hi"}) is None


def test_too_many_rules_are_refused(tmp_path, claude):
    many = [{**RULES[0], "id": f"r{i}"} for i in range(201)]
    with pytest.raises(CallError, match="at most 200"):
        run(tmp_path, "install", rules=many)
