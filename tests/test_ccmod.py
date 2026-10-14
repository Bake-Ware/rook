"""Claude Code rules (docs/design/ccmod.md): the rule format and policy, the
settings.json merge, the worker's push/hook/idle side and the hub's push,
preview and actions.

Every test uses scratch folders for HOME, the worker state and the Claude
Code config; nothing touches a real settings file.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from rook.hub.plugins.ccmod import model
from rook.hub.plugins.ccmod.model import Policy, RuleError
from rook.worker import ccmod_hook as hook
from rook.worker import ccmod_settings as cs
from rook.worker.plugins import ccmod as wp

PY = "/usr/bin/python3"
SCRIPT = "/home/user/.rook-band-worker/ccmod/rook-ccmod-hook.py"
USER = {"model": "opus",
        "permissions": {"allow": ["Bash(git status)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash",
                                  "hooks": [{"type": "command", "command": "echo mine"}]}],
                  "Stop": []},
        "env": {"A": "1"}}


def canonical(data) -> str:
    return json.dumps(data, indent=2) + "\n"


# -- the rule format and policy --------------------------------------------------------

def rule(**kw):
    base = {"name": "No force delete", "trigger": {"event": "PreToolUse", "matcher": "Bash",
                                                   "contains": "rm -rf"},
            "scope": {"kind": "all"},
            "actions": [{"type": "block", "message": "No."},
                        {"type": "post", "room_title": "Claude Code"}]}
    base.update(kw)
    return base


def test_validate_normalizes_and_computes_risk():
    r = model.validate(rule(), Policy())
    assert r["id"] == "no-force-delete" and r["enabled"] and r["risk"] == "write"
    assert r["trigger"] == {"event": "PreToolUse", "matcher": "Bash", "contains": "rm -rf"}
    assert r["actions"][1]["text"] == model.DEFAULT_POST
    assert not model.needs_admin(r)
    # Fields that do not apply to an event are dropped, not kept silently.
    s = model.validate(rule(trigger={"event": "Stop", "matcher": "Bash", "minutes": 4},
                            actions=[{"type": "inject", "text": "Update your task."}]), Policy())
    assert s["trigger"] == {"event": "Stop"}
    assert "Claude finishes its turn" in model.describe(s)


@pytest.mark.parametrize("bad, words", [
    ({"name": ""}, "name is required"),
    ({"trigger": {"event": "Nope"}}, "trigger"),
    ({"trigger": {"event": "PreToolUse", "matcher": "("}}, "not a valid pattern"),
    ({"trigger": {"event": "Idle"}, "actions": [{"type": "notify"}]}, "minutes"),
    ({"trigger": {"event": "Stop"}, "actions": [{"type": "block"}]}, "only a tool call"),
    ({"trigger": {"event": "Idle", "minutes": 5}, "actions": [{"type": "inject", "text": "x"}]},
     "only works on a Claude Code event"),
    ({"trigger": {"event": "rook.worker_offline"}, "scope": {"kind": "project", "target": "x"},
      "actions": [{"type": "notify"}]}, "band or a worker"),
    ({"scope": {"kind": "worker"}}, "target is required"),
    ({"actions": []}, "at least one action"),
    ({"actions": [{"type": "post", "room": "r1", "room_title": "x"}]}, "pick one room"),
    ({"actions": [{"type": "cap", "cap": "notify.send", "args": []}]}, "allow-list"),
])
def test_validate_refuses_with_plain_messages(bad, words):
    with pytest.raises(RuleError) as e:
        model.validate(rule(**bad), Policy())
    assert words in str(e.value)


def test_cap_actions_are_allow_listed_and_risk_tiered():
    tiers = {"notify.send": "write", "shell.exec": "exec"}
    pol = Policy(["notify.send", "shell.*"], allow_exec=False, tier_of=tiers.get)
    ok = model.validate(rule(actions=[{"type": "cap", "cap": "notify.send", "args": {"text": "hi"}}]), pol)
    assert ok["risk"] == "write" and ok["actions"][0]["worker"] == "same"
    assert model.needs_admin(ok)  # any capability rule is for administrators
    with pytest.raises(RuleError, match="can run code"):
        model.validate(rule(actions=[{"type": "cap", "cap": "shell.exec", "args": {"argv": ["ls"]}}]), pol)
    pol.allow_exec = True
    ex = model.validate(rule(actions=[{"type": "cap", "cap": "shell.exec", "args": {"argv": ["ls"]}}]), pol)
    assert ex["risk"] == "exec" and ex["actions"][0]["tier"] == "exec"
    with pytest.raises(RuleError, match="allow-list"):
        model.validate(rule(actions=[{"type": "cap", "cap": "file.write"}]), pol)


def test_tier_from_roster_is_conservative():
    roster = {"a": {"tiers": {"x.do": "write"}}, "b": {"tiers": {"x.do": "exec"}}}
    assert model.tier_from_roster(roster, "x.do") == "exec"
    assert model.tier_from_roster({}, "never.heard") == "exec"   # unknown: the highest risk
    assert model.allowed("notify.send", ["notify.*"]) and not model.allowed("notifyx.a", ["notify.*"])


def test_worker_config_scopes_and_keeps_remote_details_on_the_hub():
    rules = [model.validate(rule(), Policy()),
             model.validate(rule(name="Band only", scope={"kind": "band", "target": "home"},
                                 actions=[{"type": "notify", "text": "x"}]), Policy()),
             model.validate(rule(name="Off", actions=[{"type": "notify"}]), Policy()),
             model.validate(rule(name="Gone", trigger={"event": "rook.worker_offline"},
                                 actions=[{"type": "notify"}]), Policy()),
             model.validate(rule(name="Proj", scope={"kind": "project", "target": "myapp"},
                                 actions=[{"type": "inject", "text": "hi"}],
                                 trigger={"event": "SessionStart"}), Policy())]
    rules[2]["enabled"] = False
    cfg = model.worker_config(rules, {"name": "w1", "band": "lab", "worker_id": "id1"}, 7)
    assert [r["id"] for r in cfg["rules"]] == ["no-force-delete", "proj"]
    first = cfg["rules"][0]
    assert first["forward"] and first["local"] == [{"type": "block", "text": "", "message": "No."}]
    assert "room_title" not in json.dumps(cfg)          # the hub keeps where it posts
    assert cfg["rules"][1]["scope"] == {"kind": "project", "target": "myapp"}
    assert cfg["sha"] == model.worker_config(rules, {"name": "w1", "band": "lab"}, 9)["sha"]
    home = model.worker_config(rules, {"name": "w2", "band": "home"}, 7)
    assert "band-only" in [r["id"] for r in home["rules"]]
    assert wp.clean_config(cfg)["rules"][0]["trigger"]["matcher"] == "Bash"


@pytest.mark.parametrize("bad", [None, {"v": 2}, {"v": 1, "rules": "x"},
                                 {"v": 1, "rules": [{"id": "Bad Id"}]},
                                 {"v": 1, "rules": [{"id": "a", "trigger": {"event": "Boom"}}]},
                                 {"v": 1, "rules": [{"id": "a", "trigger": {"event": "PreToolUse",
                                                                           "matcher": "("}}]}])
def test_worker_refuses_malformed_pushes(bad):
    with pytest.raises(ValueError):
        wp.clean_config(bad)


# -- settings.json merge -------------------------------------------------------------------

EVENTS = {"PreToolUse": "Bash", "Stop": None, "SessionStart": None}


def merged(data, events=EVENTS, status=False):
    return cs.merge(cs.unmerge(data, None), events, PY, SCRIPT, status)


def test_merge_appends_after_user_hooks_and_round_trips():
    new, created, notes = merged(USER)
    pre = new["hooks"]["PreToolUse"]
    assert pre[0] == USER["hooks"]["PreToolUse"][0]                  # theirs first, untouched
    assert pre[1]["matcher"] == "Bash" and cs.is_ours(pre[1]["hooks"][0])
    assert pre[1]["hooks"][0]["command"] == f'"{PY}" -I -S "{SCRIPT}" PreToolUse || exit 0'
    assert "matcher" not in new["hooks"]["Stop"][0]
    assert list(new)[:4] == list(USER)                                # key order kept
    assert created == ["hooks.SessionStart"] and notes == []
    back = cs.unmerge(new, created)
    assert back == USER and canonical(back) == canonical(USER)       # "Stop": [] they wrote stays
    # Idempotent: merging again from the merged file gives the same file.
    again, created2, _ = cs.merge(cs.unmerge(new, created), EVENTS, PY, SCRIPT)
    assert again == new and created2 == created


def test_unmerge_keeps_user_hooks_moved_into_rooks_group():
    new, created, _ = merged(USER)
    new["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "echo also mine"})
    back = cs.unmerge(new, created)
    assert back["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "echo also mine"}]}]


def test_unmerge_without_a_manifest_drops_what_rook_emptied():
    new, _created, _ = merged({"model": "x"})
    assert set(new["hooks"]) == set(EVENTS)
    assert cs.unmerge(new, None) == {"model": "x"}


def test_statusline_never_replaces_the_users_own():
    theirs = {"statusLine": {"type": "command", "command": "~/bin/my-status"}}
    new, created, notes = merged(theirs, {}, status=True)
    assert new == theirs and created == [] and "Kept your own status line" in notes[0]
    ours, created, _ = merged({}, {}, status=True)
    assert cs.is_ours(ours["statusLine"]) and created == ["statusLine"]
    assert cs.unmerge(ours, created) == {}


def test_plan_keeps_style_refuses_bad_json_and_warns_on_disabled_hooks(tmp_path):
    p = tmp_path / "settings.json"
    p.write_bytes(json.dumps(USER, indent="\t").replace("\n", "\r\n").encode())
    out = cs.plan(p, EVENTS, PY, SCRIPT, False)
    assert out["action"] == "update" and "\r\n\t" in out["new"] and out["diff"].startswith("---")
    p.write_text('{"model": "x",}')
    with pytest.raises(cs.SettingsError, match="not valid JSON"):
        cs.plan(p, EVENTS, PY, SCRIPT, False)
    p.write_text('{"disableAllHooks": true}\n')
    assert "switched off" in cs.plan(p, EVENTS, PY, SCRIPT, False)["notes"][0]
    assert cs.plan(tmp_path / "none.json", {}, PY, SCRIPT, False)["action"] == "absent"


def test_windows_paths_use_forward_slashes():
    cmd = cs.command(r"C:\Program Files\Python\python.exe", r"C:\Users\u\.rook\h.py", "Stop")
    assert cmd == '"C:/Program Files/Python/python.exe" -I -S "C:/Users/u/.rook/h.py" Stop || exit 0'


@pytest.mark.skipif(os.name != "posix", reason="runs the command through /bin/sh")
def test_hook_command_exits_0_when_the_script_or_python_is_gone(tmp_path):
    # Python exits 2 when it cannot open its script, and Claude Code reads 2 as
    # "block": every tool call denied, every prompt erased, Stop never ending.
    assert subprocess.run([sys.executable, "-I", "-S", str(tmp_path / "gone.py")],
                          capture_output=True).returncode == 2
    for py, script in ((sys.executable, tmp_path / "gone.py"), (tmp_path / "no-python", tmp_path / "x.py")):
        cmd = cs.command(str(py), str(script), "PreToolUse")
        run = subprocess.run(cmd, shell=True, capture_output=True, text=True, input="{}", timeout=30)
        assert run.returncode == 0 and run.stdout == ""


# -- the worker plugin -----------------------------------------------------------------------

@pytest.fixture
def box(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="rcc"))   # short: the shim socket path is limited
    for d in ("h", "w", "claude", "other"):
        (base / d).mkdir()
    monkeypatch.setenv("HOME", str(base / "h"))
    monkeypatch.setenv("ROOK_WORKER_HOME", str(base / "w"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base / "claude"))
    monkeypatch.delenv("ROOK_CCMOD_CLAUDE_DIRS", raising=False)
    monkeypatch.delenv("ROOK_AGENT", raising=False)
    yield base
    shutil.rmtree(base, ignore_errors=True)


def config(*rules, rev=3):
    return {"v": 1, "rev": rev, "sha": "s%d" % rev, "worker": "w1", "band": "lab",
            "rules": list(rules) or [
                {"id": "no-rm", "trigger": {"event": "PreToolUse", "matcher": "Bash",
                                            "contains": "rm -rf"},
                 "local": [{"type": "block", "message": "No rm -rf on {worker}."}], "forward": True},
                {"id": "ctx", "trigger": {"event": "SessionStart"},
                 "local": [{"type": "inject", "text": "Project {project}."},
                           {"type": "statusline", "text": "rook · {project}"}]},
                {"id": "idle", "trigger": {"event": "Idle", "minutes": 5}, "forward": True}]}


@pytest.mark.asyncio
async def test_apply_dry_run_write_repeat_and_remove(box):
    settings = box / "claude" / "settings.json"
    settings.write_text(canonical(USER))
    plugin = wp.ClaudeCodeRules()
    dry = await plugin.apply(config(), dry_run=True)
    assert dry["dry_run"] and dry["installations"][0]["action"] == "update"
    assert "+" in dry["installations"][0]["diff"] and settings.read_text() == canonical(USER)
    assert not plugin.rules_path.exists()
    assert dry["events"] == ["PreToolUse", "SessionEnd", "SessionStart", "Stop", "UserPromptSubmit"]

    done = await plugin.apply(config())
    row = done["installations"][0]
    assert row["action"] == "update" and Path(row["backup"]).read_text() == canonical(USER)
    assert plugin.script_path.exists() and plugin.rules_path.exists()
    data = json.loads(settings.read_text())
    assert data["hooks"]["PreToolUse"][0] == USER["hooks"]["PreToolUse"][0]
    assert cs.is_ours(data["statusLine"])
    st = plugin.status()
    assert st["rev"] == 3 and st["installations"][0]["managed"]

    again = await plugin.apply(config())
    assert again["installations"][0]["action"] == "unchanged"
    assert again["rules_file"]["action"] == "unchanged"

    gone = await plugin.remove()
    assert gone["installations"][0]["action"] == "remove"
    assert settings.read_text() == canonical(USER)                 # byte for byte
    assert not plugin.rules_path.exists() and not plugin.script_path.exists()
    assert "script_kept" not in gone
    assert not plugin.status()["installations"][0]["managed"]
    assert not (wp.base_dir() / "originals").exists() or not os.listdir(wp.base_dir() / "originals")


@pytest.mark.asyncio
async def test_removal_restores_a_hand_formatted_file_byte_for_byte(box):
    settings = box / "claude" / "settings.json"
    mine = '{ "model": "opus",\n  "env": {"A": "caf\\u00e9"},   "permissions": {"allow": ["x", "y"]}\n}\n'
    settings.write_text(mine)
    plugin = wp.ClaudeCodeRules()
    await plugin.apply(config())
    assert settings.read_text() != mine                          # rewritten as JSON
    await plugin.apply(config(rev=4, *config()["rules"][:2]))    # rules change, file untouched by hand
    dry = await plugin.remove(dry_run=True)
    assert settings.read_text() != mine and dry["installations"][0]["action"] == "remove"
    gone = await plugin.remove()
    assert gone["installations"][0]["restored_original"] and settings.read_text() == mine


@pytest.mark.asyncio
async def test_removal_after_a_hand_edit_unmerges_instead(box):
    settings = box / "claude" / "settings.json"
    settings.write_text('{"model": "opus"}')
    plugin = wp.ClaudeCodeRules()
    await plugin.apply(config())
    data = json.loads(settings.read_text())
    data["model"] = "sonnet"                                     # the person (or /model) changed it
    settings.write_text(json.dumps(data, indent=2) + "\n")
    gone = await plugin.remove()
    assert "restored_original" not in gone["installations"][0]
    assert json.loads(settings.read_text()) == {"model": "sonnet"}


@pytest.mark.asyncio
async def test_removal_keeps_the_script_while_a_file_still_runs_it(box, monkeypatch):
    # One installation cannot be changed: its hooks still name the script, so
    # the script must stay (else Claude Code would run a missing file).
    monkeypatch.setenv("ROOK_CCMOD_CLAUDE_DIRS", str(box / "other"))
    plugin = wp.ClaudeCodeRules()
    await plugin.apply(config())
    other = box / "other" / "settings.json"
    other.write_text(other.read_text().rstrip().rstrip("}") + ', "broken": }')
    gone = await plugin.remove()
    rows = {r["path"]: r for r in gone["installations"]}
    assert rows[str(other)]["action"] == "error"
    assert plugin.script_path.exists() and str(other) in gone["script_kept"]
    assert not plugin.rules_path.exists()          # the hook does nothing without its rules
    cmd = json.loads(other.read_text().replace(', "broken": }', "}"))["hooks"]["Stop"][0]["hooks"][0]["command"]
    run = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, input="{}")
    assert run.returncode == 0 and run.stdout == ""
    # Once the file is fixed, removal finishes and the script goes.
    other.write_text(other.read_text().replace(', "broken": }', "}"))
    done = await plugin.remove()
    assert "script_kept" not in done and not plugin.script_path.exists()
    assert not other.exists() and not plugin.manifest_path.exists()


@pytest.mark.asyncio
async def test_apply_plans_again_when_the_file_changed_meanwhile(box, monkeypatch):
    settings = box / "claude" / "settings.json"
    settings.write_text('{"model": "opus"}\n')
    plugin = wp.ClaudeCodeRules()
    real_plan = plugin._plan
    calls = []

    def racing(path, *a):
        out = real_plan(path, *a)
        if not calls:                    # Claude Code writes /model right after the first read
            settings.write_text('{"model": "sonnet"}\n')
        calls.append(path)
        return out

    monkeypatch.setattr(plugin, "_plan", racing)
    await plugin.apply(config())
    data = json.loads(settings.read_text())
    assert len(calls) == 2 and data["model"] == "sonnet" and "hooks" in data


@pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX owners")
def test_atomic_write_sets_mode_and_owner_on_the_descriptor(tmp_path, monkeypatch):
    from rook.core.db import atomic_write_text
    target = tmp_path / "settings.json"
    seen = []

    def by_path(*a, **kw):
        raise AssertionError(f"mode or owner set by path: {a}")

    monkeypatch.setattr(os, "chmod", by_path)
    monkeypatch.setattr(os, "chown", by_path)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: seen.append((type(fd), uid, gid)))
    st = tmp_path.stat()
    atomic_write_text(target, "{}\n", 0o640, owner=(st.st_uid + 7, st.st_gid + 7))
    assert seen == [(int, st.st_uid + 7, st.st_gid + 7)]
    assert target.stat().st_mode & 0o777 == 0o640 and target.read_text() == "{}\n"
    seen.clear()
    atomic_write_text(target, "{}\n", 0o600, owner=(os.getuid(), os.getgid()))
    assert seen == []                                     # already theirs: nothing to do


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX owners")
async def test_settings_keep_their_owner_and_a_swapped_symlink_is_not_chowned(box, monkeypatch):
    # A root worker writing into user U's folder: the file goes back to U, set
    # on the temp file before the rename. U swapping settings.json for a
    # symlink to a root file right after the rename must not get that file.
    settings = box / "claude" / "settings.json"
    settings.write_text('{"model": "opus"}\n')
    victim = box / "victim"
    victim.write_text("root:x:0:0\n")
    real_settings = os.path.realpath(settings)
    st = os.lstat(settings)
    events = []
    real_lstat, real_replace = os.lstat, os.replace

    def lstat(path, *a, **kw):            # U owns the file, not the worker
        r = real_lstat(path, *a, **kw)
        if os.fspath(path) == real_settings and not __import__("stat").S_ISLNK(r.st_mode):
            return os.stat_result((r.st_mode, r.st_ino, r.st_dev, r.st_nlink, st.st_uid + 7,
                                   st.st_gid + 7, r.st_size, int(r.st_atime), int(r.st_mtime),
                                   int(r.st_ctime)))
        return r

    def replace(src, dst):
        real_replace(src, dst)
        events.append(("replace", os.fspath(dst)))
        if os.fspath(dst) == real_settings:      # U's swap, the moment after the rename
            os.unlink(dst)
            os.symlink(victim, dst)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: events.append(("fchown", uid, gid)))
    for name in ("chown", "lchown"):
        monkeypatch.setattr(os, name, lambda *a, **kw: events.append(("by-path",) + a))
    await wp.ClaudeCodeRules().apply(config())
    assert ("fchown", st.st_uid + 7, st.st_gid + 7) in events
    mine = [e for e in events if e[0] != "replace" or e[1] == real_settings]
    assert mine.index(("fchown", st.st_uid + 7, st.st_gid + 7)) < mine.index(("replace", real_settings))
    assert not [e for e in events if e[0] == "by-path"]
    assert victim.read_text() == "root:x:0:0\n"


def _theirs(monkeypatch, *paths, uid=4242):
    """Make ``paths`` (and what is inside them) look owned by another user."""
    real = wp._stat_uid
    roots = [os.path.realpath(x) for x in paths]
    monkeypatch.setattr(wp, "_stat_uid", lambda q: uid if any(
        os.path.realpath(q) == r or os.path.realpath(q).startswith(r + os.sep) for r in roots) else real(q))


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="POSIX symlinks")
async def test_root_worker_refuses_a_symlink_to_another_users_file(box, monkeypatch):
    settings = box / "claude" / "settings.json"
    victim = box / "victim.json"
    victim.write_text('{"model": "root-only"}\n')
    settings.symlink_to(victim)
    _theirs(monkeypatch, victim)
    monkeypatch.setattr(wp, "_is_root", lambda: True)
    plugin = wp.ClaudeCodeRules()
    res = await plugin.apply(config())
    row = res["installations"][0]
    assert row["action"] == "error" and "another user" in row["error"]
    assert victim.read_text() == '{"model": "root-only"}\n' and settings.is_symlink()
    gone = await plugin.remove()
    assert gone["installations"][0]["action"] == "error"
    assert victim.read_text() == '{"model": "root-only"}\n'
    # A link to the same user's own file (dotfiles) is fine, as before.
    monkeypatch.setattr(wp, "_stat_uid", lambda q: os.stat(q).st_uid)
    assert (await plugin.apply(config()))["installations"][0]["action"] == "update"
    assert "hooks" in json.loads(victim.read_text())


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="POSIX symlinks")
async def test_root_worker_refuses_a_claude_folder_linked_to_another_user(box, monkeypatch):
    theirs = box / "theirs"
    theirs.mkdir()
    (theirs / "settings.json").write_text("{}\n")
    (box / "linked").symlink_to(theirs, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(box / "linked"))
    _theirs(monkeypatch, theirs)
    monkeypatch.setattr(wp, "_is_root", lambda: True)
    res = await wp.ClaudeCodeRules().apply(config())
    assert res["installations"][0]["action"] == "error"
    assert (theirs / "settings.json").read_text() == "{}\n"


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="POSIX symlinks")
async def test_non_root_worker_still_writes_through_symlinks(box, monkeypatch):
    settings = box / "claude" / "settings.json"
    target = box / "dotfiles.json"
    target.write_text("{}\n")
    settings.symlink_to(target)
    _theirs(monkeypatch, target)
    monkeypatch.setattr(wp, "_is_root", lambda: False)
    assert wp.foreign_link(settings) is None
    res = await wp.ClaudeCodeRules().apply(config())
    assert res["installations"][0]["action"] == "update" and settings.is_symlink()
    assert "hooks" in json.loads(target.read_text())


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="POSIX symlinks")
async def test_a_symlink_swapped_in_before_the_write_is_refused(box, monkeypatch):
    # Between resolving settings.json and reading its mode and owner, it is
    # swapped for a symlink: its 0777 and its owner must not be copied, and
    # nothing is written through it.
    settings = box / "claude" / "settings.json"
    settings.write_text("{}\n")
    os.chmod(settings, 0o600)
    victim = box / "victim.json"
    victim.write_text('{"model": "x"}\n')
    os.chmod(victim, 0o600)
    calls = []

    def check(path):
        calls.append(path)
        if len(calls) == 2:              # the check just before the write
            os.unlink(settings)
            os.symlink(victim, settings)
        return None

    monkeypatch.setattr(wp, "foreign_link", check)
    res = await wp.ClaudeCodeRules().apply(config())
    row = res["installations"][0]
    assert row["action"] == "error" and "turned into a symlink" in row["error"]
    assert victim.read_text() == '{"model": "x"}\n' and victim.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_in_place_fallback_does_not_follow_a_symlink(tmp_path):
    from rook.core import db
    victim = tmp_path / "victim"
    victim.write_text("root:x:0:0\n")
    link = tmp_path / "settings.json"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        db._write_in_place(link, "{}\n", 0o600, "utf-8")
    assert victim.read_text() == "root:x:0:0\n"


@pytest.mark.asyncio
async def test_new_settings_file_goes_to_the_folders_owner(box, monkeypatch):
    calls = []
    real = wp._owner
    monkeypatch.setattr(wp, "_owner", lambda before: calls.append(before.st_ino) or real(before))
    monkeypatch.setenv("ROOK_CCMOD_CLAUDE_DIRS", str(box / "other"))
    await wp.ClaudeCodeRules().apply(config())
    assert calls == [(box / "claude").stat().st_ino, (box / "other").stat().st_ino]


@pytest.mark.asyncio
async def test_removal_finishes_when_rooks_hooks_were_taken_out_by_hand(box):
    settings = box / "claude" / "settings.json"
    settings.write_text('{"model": "opus"}\n')
    plugin = wp.ClaudeCodeRules()
    await plugin.apply(config())
    settings.write_text('{"model": "opus", "env": {"B": "2"}}\n')     # Rook's hooks deleted by hand
    gone = await plugin.remove()
    assert gone["installations"][0]["action"] == "absent" and "script_kept" not in gone
    assert not plugin.script_path.exists() and not plugin.manifest_path.exists()
    assert settings.read_text() == '{"model": "opus", "env": {"B": "2"}}\n'
    assert "script_kept" not in await plugin.remove()


@pytest.mark.asyncio
async def test_apply_creates_and_deletes_a_settings_file_and_follows_extra_dirs(box, monkeypatch):
    monkeypatch.setenv("ROOK_CCMOD_CLAUDE_DIRS", str(box / "other"))
    plugin = wp.ClaudeCodeRules()
    res = await plugin.apply(config())
    assert [r["action"] for r in res["installations"]] == ["create", "create"]
    assert (box / "other" / "settings.json").exists()
    # The extra folder is dropped from the configuration: Rook comes out of it.
    monkeypatch.delenv("ROOK_CCMOD_CLAUDE_DIRS")
    (box / "other" / "settings.json").write_text(
        (box / "other" / "settings.json").read_text().replace('"hooks"', '"model": "x", "hooks"', 1))
    res = await plugin.apply(config())
    assert [r["action"] for r in res["installations"]] == ["unchanged", "remove"]
    assert json.loads((box / "other" / "settings.json").read_text()) == {"model": "x"}
    await plugin.remove()
    assert not (box / "claude" / "settings.json").exists()        # Rook made it, Rook removes it


@pytest.mark.asyncio
async def test_a_broken_settings_file_is_reported_not_rewritten(box):
    settings = box / "claude" / "settings.json"
    settings.write_text("{oops")
    res = await wp.ClaudeCodeRules().apply(config())
    assert res["installations"][0]["action"] == "error" and settings.read_text() == "{oops"


def run_hook(base: Path, event: str, data: dict, env=None) -> str:
    cfg = json.loads((base / "rules.json").read_text())
    return hook.run(event, json.dumps(data), cfg, str(base), env or {})


@pytest.mark.asyncio
async def test_hook_replies_block_inject_statusline_and_forwards(box):
    plugin = wp.ClaudeCodeRules()
    await plugin.apply(config())
    base = wp.base_dir()
    proj = box / "h" / "myapp"
    (proj / ".git").mkdir(parents=True)
    out = json.loads(run_hook(base, "PreToolUse", {"session_id": "s1", "cwd": str(proj / "src"),
                                                    "tool_name": "Bash",
                                                    "tool_input": {"command": "rm -rf build"}}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "No rm -rf on w1."
    assert run_hook(base, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}}) == ""
    start = json.loads(run_hook(base, "SessionStart", {"session_id": "s1", "cwd": str(proj)}))
    assert start["hookSpecificOutput"]["additionalContext"] == "Project myapp."
    assert hook.run("statusline", json.dumps({"session_id": "s1"}), {}, str(base)) == "rook · myapp"
    # Without the worker's socket the events wait in the outbox.
    for msg in plugin._drain_outbox():
        plugin.ingest(msg)
    got = await plugin.events()
    assert [(e["event"], e["matched"], e["project"]) for e in got["events"]] == [
        ("PreToolUse", ["no-rm"], "myapp")]
    assert run_hook(base, "Bogus", {}) == "" and hook.run("PreToolUse", "not json", {}, str(base)) == ""


def test_outbox_is_capped_and_stale_events_are_dropped(box, monkeypatch):
    base = wp.base_dir()
    box_dir = base / "outbox"
    cfg = {"outbox": str(box_dir)}
    monkeypatch.setattr(hook, "MAX_OUTBOX", 3)
    for i in range(5):
        hook.forward(cfg, {"op": "hook", "event": "Stop", "ts": __import__("time").time(), "i": i}, str(base))
    assert len(os.listdir(box_dir)) == 3                     # the rest dropped, not queued forever
    old = {"op": "hook", "event": "Stop", "ts": 1000.0}
    (box_dir / "1-1.json").write_text(json.dumps(old))
    plugin = wp.ClaudeCodeRules()
    got = plugin._drain_outbox()
    assert len(got) == 3 and all(g["ts"] != 1000.0 for g in got)
    assert os.listdir(box_dir) == []


def test_large_tool_input_still_meets_contains_rules():
    cfg = {"worker": "w", "rules": [
        {"id": "a", "trigger": {"event": "PreToolUse", "matcher": "Write", "contains": "SECRET"},
         "local": [{"type": "block", "message": "No."}]}]}
    big = {"session_id": "s", "cwd": "/x", "hook_event_name": "PreToolUse", "tool_name": "Write",
           "tool_input": {"file_path": "/x/a", "content": "y" * (hook.MAX_STDIN + 10) + " SECRET"}}
    raw = json.dumps(big)
    cut = raw[:hook.MAX_STDIN + 1]                               # what main() reads at most
    assert "SECRET" not in cut
    assert hook.run("PreToolUse", cut, cfg, tempfile.mkdtemp()) == ""
    big["tool_input"]["content"] = "SECRET " + "y" * (hook.MAX_STDIN + 10)
    cut = json.dumps(big)[:hook.MAX_STDIN + 1]
    out = json.loads(hook.run("PreToolUse", cut, cfg, tempfile.mkdtemp()))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook.partial(cut)["tool_name"] == "Write" and hook.partial(cut)["session_id"] == "s"
    assert hook.MAX_STDIN >= 8 << 20


def test_hook_stop_inject_runs_once_and_prompts_can_be_blocked():
    cfg = {"worker": "w", "rules": [
        {"id": "a", "trigger": {"event": "Stop"}, "local": [{"type": "inject", "text": "Update the task."}]},
        {"id": "b", "trigger": {"event": "UserPromptSubmit", "contains": "deploy"},
         "local": [{"type": "block", "message": "Not from here."}]},
        {"id": "c", "trigger": {"event": "UserPromptSubmit"}, "scope": {"kind": "agent", "target": "ana"},
         "local": [{"type": "inject", "text": "Hi {agent}."}]}]}
    d = tempfile.mkdtemp()
    assert json.loads(hook.run("Stop", "{}", cfg, d)) == {"decision": "block", "reason": "Update the task."}
    assert hook.run("Stop", json.dumps({"stop_hook_active": True}), cfg, d) == ""
    blocked = json.loads(hook.run("UserPromptSubmit", json.dumps({"prompt": "please DEPLOY"}), cfg, d))
    assert blocked == {"decision": "block", "reason": "Not from here."}
    assert hook.run("UserPromptSubmit", json.dumps({"prompt": "hi"}), cfg, d) == ""
    agent = json.loads(hook.run("UserPromptSubmit", json.dumps({"prompt": "hi"}), cfg, d, {"ROOK_AGENT": "ana"}))
    assert agent["hookSpecificOutput"]["additionalContext"] == "Hi ana."


def test_project_scope_by_name_or_path():
    ctx = {"cwd": "/srv/apps/myapp/src", "project": "myapp"}
    assert hook.in_scope({"kind": "project", "target": "myapp"}, ctx)
    assert hook.in_scope({"kind": "project", "target": "apps"}, ctx)
    assert hook.in_scope({"kind": "project", "target": "/srv/apps"}, ctx)
    assert not hook.in_scope({"kind": "project", "target": "/srv/app"}, ctx)
    assert not hook.in_scope({"kind": "project", "target": "other"}, ctx)


@pytest.mark.asyncio
async def test_installed_script_runs_standalone_and_never_fails(box):
    plugin = wp.ClaudeCodeRules()
    await plugin.apply(config())
    cmd = json.loads((box / "claude" / "settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    run = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=str(box),
                         input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}))
    assert run.returncode == 0 and "permissionDecision" in run.stdout
    bad = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, input="\x00garbage")
    assert bad.returncode == 0 and bad.stdout == "" and bad.stderr == ""
    plugin.rules_path.write_text("{broken")
    broken = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, input="{}")
    assert broken.returncode == 0 and broken.stdout == ""


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="the shim socket is POSIX-only")
async def test_events_arrive_over_the_shim_socket(box):
    from rook.worker.plugins.session_shim import SessionShimPlugin
    shim = SessionShimPlugin()
    plugin = wp.ClaudeCodeRules()
    plugin.bind_worker(SimpleNamespace(plugins=[shim, plugin]))
    try:
        await plugin.apply(config())
        cfg = json.loads(plugin.rules_path.read_text())
        assert cfg["socket"] and Path(cfg["socket"]).exists()
        waiter = asyncio.create_task(plugin.events(wait=5))
        await asyncio.sleep(0.05)
        await asyncio.to_thread(hook.forward, cfg, {"op": "hook", "event": "PreToolUse",
                                                    "tool": "Bash", "matched": ["no-rm"]},
                                str(wp.base_dir()))
        got = await asyncio.wait_for(waiter, 5)
        assert [e["matched"] for e in got["events"]] == [["no-rm"]]
        assert not (wp.base_dir() / "outbox").exists() or not os.listdir(wp.base_dir() / "outbox")
    finally:
        await shim.stop()


@pytest.mark.asyncio
async def test_ingest_trusts_only_known_rules_and_idle_fires_once(box):
    plugin = wp.ClaudeCodeRules()
    await plugin.apply(config())
    plugin.ingest({"op": "hook", "event": "PreToolUse", "matched": ["ctx", "nope", "no-rm"]})
    plugin.ingest({"op": "nope", "event": "PreToolUse", "matched": ["no-rm"]})
    plugin.ingest({"op": "hook", "event": "Stop", "session": "s1", "activity": True, "cwd": "/x/p",
                   "project": "p", "ts": 1000.0})
    first = await plugin.events()
    assert [e["matched"] for e in first["events"]] == [["no-rm"]]   # forward rules only
    assert plugin.check_idle(1000 + 4 * 60) == 0
    assert plugin.check_idle(1000 + 5 * 60) == 1
    assert plugin.check_idle(1000 + 9 * 60) == 0                    # once per idle stretch
    later = await plugin.events(after=first["cursor"], boot=first["boot"])
    assert [(e["event"], e["minutes"], e["project"]) for e in later["events"]] == [("Idle", "5", "p")]
    plugin.ingest({"op": "hook", "event": "UserPromptSubmit", "session": "s1", "activity": True})
    plugin.ingest({"op": "hook", "event": "Stop", "session": "s1", "activity": True, "ts": 2000.0})
    assert plugin.check_idle(2000 + 6 * 60) == 1
    # A new boot (or a hub that never asked) starts from the beginning.
    assert len((await plugin.events(after=99, boot="other"))["events"]) == 3


@pytest.mark.asyncio
async def test_events_long_poll_wakes_on_a_new_event(box):
    plugin = wp.ClaudeCodeRules()
    await plugin.apply(config())
    t = asyncio.create_task(plugin.events(wait=5, boot=plugin._boot))
    await asyncio.sleep(0.05)
    plugin.ingest({"op": "hook", "event": "PreToolUse", "matched": ["no-rm"]})
    got = await asyncio.wait_for(t, 2)
    assert got["cursor"] == 1 and len(got["events"]) == 1
    empty = await plugin.events(after=1, boot=plugin._boot, wait=0.05)
    assert empty["events"] == [] and empty["cursor"] == 1


# -- the hub plugin ---------------------------------------------------------------------------

class FakeClient:
    """The band as the hub sees it: a roster and workers that answer."""

    def __init__(self, workers: dict, plugins: dict) -> None:
        self.workers = workers
        self.plugins = plugins      # worker id -> worker-side ClaudeCodeRules (or None)
        self.calls: list = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None, principal=None):
        self.calls.append((cap, target, args, identity))
        p = self.plugins.get(target)
        if p is None or "ccmod.apply" not in self.workers[target].get("caps", ()):
            return {"ok": False, "error": f"unknown cap {cap}"}
        try:
            fn = {"ccmod.apply": p.apply, "ccmod.events": p.events, "ccmod.remove": p.remove}[cap]
            return {"ok": True, "result": await fn(**(args or {}))}
        except ValueError as e:
            return {"ok": False, "error": str(e)}


@pytest.fixture
def hub(tmp_path, box):
    import time as _t
    from rook.hub.node import HubNode
    from rook.hub.settings_store import SettingsStore
    node = HubNode(str(tmp_path), entry_points=False,
                   settings_store=SettingsStore(tmp_path / "settings.db"))
    new = wp.ClaudeCodeRules()
    now = _t.time()
    workers = {"w-new": {"name": "lab-pc", "band": "lab", "caps": ["ccmod.apply", "ccmod.events"],
                         "last_seen": now},
               "w-old": {"name": "old-pi", "band": "lab", "caps": ["shell.exec"], "last_seen": now}}
    node.client = FakeClient(workers, {"w-new": new})
    return node, node.plugin("ccmod"), new


def post_rule(name="Waiting", **kw):
    r = {"name": name, "trigger": {"event": "PreToolUse", "matcher": "Bash"}, "scope": {"kind": "all"},
         "actions": [{"type": "post", "room_title": "Claude Code", "text": "{worker} ran {tool} in {project}"},
                     {"type": "block", "message": "Not today."}]}
    r.update(kw)
    return r


@pytest.mark.asyncio
async def test_push_skips_old_workers_and_tracks_currency(hub, box):
    node, plugin, worker = hub
    plugin.save_rule(post_rule(), "human:op")
    res = await plugin.push(actor="human:op")
    by = {r["worker"]: r for r in res["results"]}
    assert by["old-pi"]["skipped"] and "cannot take" in by["old-pi"]["error"]
    assert by["lab-pc"]["ok"] and by["lab-pc"]["rules"] == 1
    assert [c for c in node.client.calls if c[1] == "w-old"] == []   # never sent to an old worker
    assert (box / "claude" / "settings.json").exists()
    rows = {r["name"]: r for r in plugin.status_rows()}
    assert rows["lab-pc"]["current"] and rows["old-pi"]["supported"] is False
    plugin.save_rule(post_rule("Second", trigger={"event": "Stop"},
                               actions=[{"type": "notify", "text": "x"}]), "human:op")
    assert not {r["name"]: r for r in plugin.status_rows()}["lab-pc"]["current"]
    prev = await plugin.preview("lab-pc")
    assert prev["ok"] and prev["installations"][0]["action"] == "update"
    assert "Stop" in prev["installations"][0]["diff"]
    old = await plugin.preview("old-pi")
    assert not old["ok"] and not old["supported"]
    removed = await plugin.push("lab-pc", "human:op", remove=True)
    assert removed["results"][0]["ok"] and not (box / "claude" / "settings.json").exists()


@pytest.mark.asyncio
async def test_old_worker_ignores_an_unexpected_push():
    # A worker without the plugin answers a stray ccmod.apply with an error; nothing else.
    from rook.worker.core import Worker  # noqa: F401 - the worker imports cleanly without it
    client = FakeClient({"w": {"name": "w", "caps": []}}, {"w": None})
    reply = await client.call("ccmod.apply", {"config": {"v": 1, "rules": []}}, "w")
    assert reply == {"ok": False, "error": "unknown cap ccmod.apply"}


@pytest.mark.asyncio
async def test_events_run_remote_actions_rechecked_on_the_hub(hub):
    node, plugin, worker = hub
    plugin.save_rule(post_rule(), "human:op")
    plugin.save_rule(post_rule("Quiet", trigger={"event": "Stop"},
                               actions=[{"type": "post", "room_title": "Claude Code", "text": "stop"}]),
                     "human:op")
    entry = {**node.client.workers["w-new"], "worker_id": "w-new"}
    rows = await plugin.handle_event("w-new", entry, {"event": "PreToolUse", "tool": "Bash",
                                                      "project": "myapp", "matched": ["waiting"]})
    assert [(r["action"], r["ok"]) for r in rows] == [("post", True)]
    # The worker's word is not enough: a rule whose trigger does not match does not run.
    assert await plugin.handle_event("w-new", entry, {"event": "PreToolUse", "tool": "Edit",
                                                      "matched": ["waiting", "quiet"]}) == []
    chat = node.plugin("chat")._store
    room = plugin.store.room_for("Claude Code")
    msgs = chat.read(room, "user:operator")["messages"]
    assert msgs[-1]["sender"] == "claude-code@lab-pc" and msgs[-1]["text"] == "lab-pc ran Bash in myapp"
    assert "user:operator" in chat.rooms_for("user:operator")["rooms"][0]["participants"]
    # A second event reuses the room; the rate limit then drops the excess.
    plugin.settings.refresh({"rate_per_minute": 2})
    await plugin.handle_event("w-new", entry, {"event": "PreToolUse", "tool": "Bash", "matched": ["waiting"]})
    limited = await plugin.handle_event("w-new", entry, {"event": "PreToolUse", "tool": "Bash",
                                                         "matched": ["waiting"]})
    assert limited[0]["detail"] == "rate limit: skipped"
    assert len(chat.rooms_for("user:operator")["rooms"]) == 1
    assert plugin.store.activity()[0]["detail"] == "rate limit: skipped"


@pytest.mark.asyncio
async def test_hub_takes_the_hooks_word_for_text_and_paths(hub):
    node, plugin, worker = hub
    entry = {**node.client.workers["w-new"], "worker_id": "w-new"}
    post = [{"type": "post", "room_title": "Claude Code", "text": "{prompt}"}]
    plugin.save_rule({"name": "Deploy", "trigger": {"event": "UserPromptSubmit", "contains": "deploy"},
                      "scope": {"kind": "all"}, "actions": post}, "human:op")
    plugin.save_rule({"name": "Home app", "trigger": {"event": "Stop"},
                      "scope": {"kind": "project", "target": "~/src/app"}, "actions": post}, "human:op")
    plugin.save_rule({"name": "Win app", "trigger": {"event": "Stop"},
                      "scope": {"kind": "project", "target": "C:\\src\\app"}, "actions": post}, "human:op")
    plugin.save_rule({"name": "Named", "trigger": {"event": "Stop"},
                      "scope": {"kind": "project", "target": "other"}, "actions": post}, "human:op")
    # The needle is past the excerpt the hub receives; the hook saw it.
    rows = await plugin.handle_event("w-new", entry, {"event": "UserPromptSubmit", "prompt": "x" * 300 + "...",
                                                      "matched": ["deploy"]})
    assert [r["rule"] for r in rows] == ["deploy"]
    # Path scopes are the worker's paths (its home, its kind of path).
    rows = await plugin.handle_event("w-new", entry, {"event": "Stop", "cwd": "/home/u/src/app",
                                                      "project": "app",
                                                      "matched": ["home-app", "win-app", "named"]})
    assert sorted(r["rule"] for r in rows) == ["home-app", "win-app"]    # a project name it checks
    # Not matched by the hook: not run, whatever the hub would think.
    assert await plugin.handle_event("w-new", entry, {"event": "Stop", "project": "other"}) == []
    from rook.hub.plugins.ccmod import hub_scope
    assert hub_scope({"kind": "project", "target": "myapp"}) == {"kind": "project", "target": "myapp"}
    assert hub_scope({"kind": "project", "target": "/srv/x"})["kind"] == "all"


@pytest.mark.asyncio
async def test_cap_actions_recheck_policy_when_they_run(hub):
    node, plugin, worker = hub
    plugin.settings.refresh({"cap_allowlist": ["info.ping"]})
    plugin.save_rule(post_rule("Ping", trigger={"event": "Stop"},
                               actions=[{"type": "cap", "cap": "info.ping", "args": {}}]), "human:op")
    plugin.settings.refresh({"cap_allowlist": []})              # taken off the list since
    entry = {**node.client.workers["w-new"], "worker_id": "w-new"}
    rows = await plugin.handle_event("w-new", entry, {"event": "Stop", "matched": ["ping"]})
    assert rows[0]["action"] == "cap" and not rows[0]["ok"] and "allow-list" in rows[0]["detail"]
    assert not [c for c in node.client.calls if c[0] == "info.ping"]


@pytest.mark.asyncio
async def test_worker_offline_is_a_rook_event(hub):
    node, plugin, worker = hub
    plugin.save_rule({"name": "Gone", "trigger": {"event": "rook.worker_offline"},
                      "scope": {"kind": "worker", "target": "lab-pc"},
                      "actions": [{"type": "post", "room_title": "Fleet", "text": "{worker} went offline"}]},
                     "human:op")
    plugin._watch_presence()                       # baseline
    node.client.workers["w-new"]["last_seen"] = 0
    plugin._watch_presence()
    for _ in range(50):
        if plugin.store.activity():
            break
        await asyncio.sleep(0.02)
    assert plugin.store.activity()[0]["rule"] == "gone" and plugin.store.activity()[0]["ok"]


@pytest.mark.asyncio
async def test_poller_follows_a_worker_and_runs_actions(hub):
    node, plugin, worker = hub
    plugin.save_rule(post_rule(), "human:op")
    await plugin.push("lab-pc", "human:op")
    assert set(plugin._pollers) == {"w-new"}
    await asyncio.sleep(0.05)
    worker.ingest({"op": "hook", "event": "PreToolUse", "tool": "Bash", "matched": ["waiting"]})
    for _ in range(100):
        if plugin.store.activity():
            break
        await asyncio.sleep(0.02)
    assert plugin.store.activity()[0]["action"] == "post"
    await plugin.stop()


async def _wait_for(cond, tries=150):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


@pytest.mark.asyncio
async def test_a_restarted_hub_does_not_replay_events(hub, tmp_path):
    node, plugin, worker = hub
    plugin.save_rule(post_rule(), "human:op")
    await plugin.push("lab-pc", "human:op")
    assert plugin.store.pushes()["w-new"]["actor"] == "human:op"      # who pushed
    for _ in range(3):
        worker.ingest({"op": "hook", "event": "PreToolUse", "tool": "Bash", "matched": ["waiting"]})
    assert await _wait_for(lambda: len(plugin.store.activity()) == 3)
    assert plugin.store.cursor("w-new") == (worker._boot, 3)
    await plugin.stop()
    # A new hub process: same store file, fresh plugin state.
    from rook.hub.plugins.ccmod import ClaudeCodeRules
    again = ClaudeCodeRules()
    again._store = model.Store(plugin.store.path)
    again._node = node
    again._sync_pollers()
    assert set(again._pollers) == {"w-new"}
    worker.ingest({"op": "hook", "event": "PreToolUse", "tool": "Bash", "matched": ["waiting"]})
    assert await _wait_for(lambda: len(again.store.activity()) == 4)
    await asyncio.sleep(0.1)
    assert len(again.store.activity()) == 4                            # 3 old ones not run again
    await again.stop()


@pytest.mark.asyncio
async def test_background_actions_do_not_act_as_the_pusher(hub):
    from rook.hub.authz import current_principal
    from rook.hub.policy import Principal
    node, plugin, worker = hub
    plugin.settings.refresh({"cap_allowlist": ["ccmod.remove"], "allow_exec": True})
    plugin.save_rule(post_rule("Ping", trigger={"event": "PreToolUse", "matcher": "Bash"},
                               actions=[{"type": "cap", "cap": "ccmod.remove", "args": {"dry_run": True}}]),
                     "human:op")
    seen = []
    real = node.client.call

    async def call(cap, args=None, target=None, timeout=15.0, identity=None, principal=None):
        if cap == "ccmod.remove":
            seen.append((identity, current_principal.get()))
        return await real(cap, args, target, timeout, identity, principal)

    node.client.call = call
    tok = current_principal.set(Principal("token:op", "token", "operator"))
    try:
        await plugin.push("lab-pc", "token:op")          # starts the poller inside the pusher's call
    finally:
        current_principal.reset(tok)
    worker.ingest({"op": "hook", "event": "PreToolUse", "tool": "Bash", "matched": ["ping"]})
    assert await _wait_for(lambda: seen)
    assert seen[0] == ("system:ccmod:ping", None)
    from rook.hub.authz import Authorizer
    assert Authorizer.principal(None, seen[0][0]).id == "system:ccmod:ping"
    await plugin.stop()


@pytest.mark.asyncio
async def test_caps_tiers_admin_gate_and_band_ceiling(hub):
    from rook.hub.authz import current_principal
    from rook.hub.policy import Principal
    node, plugin, _ = hub
    meta = node.host.registry.meta
    assert {c: meta(f"ccmod.{c}").risk for c in ("list", "get", "history", "status", "activity",
                                                  "preview", "set", "delete", "push")} == {
        "list": "read", "get": "read", "history": "read", "status": "read", "activity": "read",
        "preview": "admin", "set": "admin", "delete": "admin", "push": "admin"}
    assert wp.ClaudeCodeRules.events._rook_cap_meta.risk == "admin"   # prompt excerpts: the hub's poller only
    plugin.settings.refresh({"cap_allowlist": ["info.ping"]})
    capped = post_rule("Ping", trigger={"event": "Stop"},
                       actions=[{"type": "cap", "cap": "info.ping", "args": {}}])
    tok = current_principal.set(Principal("token:agent_x", "token", "agent"))
    try:
        # A rule's text reaches Claude in people's sessions, and its posts can
        # carry their prompts: writing any rule is for administrators.
        for args in ({"rule": post_rule()}, {"rule": capped}):
            res = await node.dispatch("ccmod.set", args, "agent:x")
            assert not res["ok"] and "denied" in res["error"]
        assert not (await node.dispatch("ccmod.delete", {"id": "x"}, "agent:x"))["ok"]
        assert not (await node.dispatch("ccmod.preview", {"worker": "lab-pc"}, "agent:x"))["ok"]
        assert not (await node.dispatch("ccmod.push", {}))["ok"]
        assert plugin.store.rules() == []
    finally:
        current_principal.reset(tok)
    tok = current_principal.set(Principal("token:op", "token", "operator"))
    try:
        assert (await node.dispatch("ccmod.set", {"rule": capped}, "agent:op"))["ok"]
        assert (await node.dispatch("ccmod.preview", {"worker": "lab-pc"}, "agent:op"))["ok"]
    finally:
        current_principal.reset(tok)
    assert not (await node.dispatch("ccmod.set", {"rule": post_rule("Band")}, source="band"))["ok"]
    assert (await node.dispatch("ccmod.list", {}, source="band"))["ok"]
    hist = (await node.dispatch("ccmod.history", {"id": "ping"}))["result"]
    assert hist[0]["actor"] == "agent:op"


@pytest.mark.asyncio
async def test_account_api_is_operator_only_and_edits_rules(hub):
    from starlette.applications import Starlette
    from rook.hub.plugins.ccmod.web import routes
    node, plugin, _ = hub
    accounts = SimpleNamespace(session=lambda c: {"id": c, "username": c, "csrf": "k",
                                                  "admin": c == "op"} if c else None)
    app = Starlette(routes=routes(lambda: plugin, accounts))
    op, member = {"Cookie": "rook_account=op"}, {"Cookie": "rook_account=m1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        api = "/ccmod/account-api"
        assert (await c.get(api, headers=member)).status_code == 403
        dry = await c.post(api, headers=op, json={"csrf": "k", "action": "save", "rule": post_rule(),
                                                  "dry_run": True})
        assert dry.status_code == 200 and dry.json()["summary"].startswith("When Claude is about to use Bash")
        assert plugin.store.rules() == []
        saved = (await c.post(api, headers=op, json={"csrf": "k", "action": "save", "rule": post_rule()})).json()
        assert saved["rule"]["rev"] == 1 and saved["rule"]["actor"] == "human:op"
        dup = await c.post(api, headers=op, json={"csrf": "k", "action": "save", "rule": post_rule()})
        assert dup.status_code == 400 and "already exists" in dup.json()["error"]
        stale = await c.post(api, headers=op, json={"csrf": "k", "action": "save", "rev": 0,
                                                    "rule": {**post_rule(), "id": "waiting"}})
        assert stale.status_code == 400 and "reload" in stale.json()["error"]
        off = await c.post(api, headers=op, json={"csrf": "k", "action": "toggle", "id": "waiting",
                                                  "enabled": False})
        assert off.json()["rule"]["enabled"] is False
        page = (await c.get(api, headers=op)).json()
        assert page["rules"][0]["summary"] and {w["name"] for w in page["workers"]} == {"lab-pc", "old-pi"}
        assert any(e["id"] == "Idle" and e["minutes"] for e in page["choices"]["events"])
        prev = (await c.get(api, headers=op, params={"view": "preview", "worker": "lab-pc"})).json()
        assert prev["ok"] and prev["installations"][0]["action"] in ("create", "absent")
        pushed = (await c.post(api, headers=op, json={"csrf": "k", "action": "push"})).json()
        assert {r["worker"] for r in pushed["results"]} == {"lab-pc", "old-pi"}
        gone = await c.post(api, headers=op, json={"csrf": "k", "action": "delete", "id": "waiting"})
        assert gone.status_code == 200 and plugin.store.rules() == []
    await plugin.stop()


def test_dashboard_wiring():
    root = Path(__file__).resolve().parent.parent
    html = (root / "rook" / "web" / "index.html").read_text(encoding="utf-8")
    assert "import('/account/ccmod/assets/ccmod.js')" in html and 'id="view-ccmod"' in html
    # Colours come from the theme tokens (Paper is light), never literals.
    js = (root / "rook" / "web" / "ccmod.js").read_text(encoding="utf-8")
    css = js[js.index("const CSS=`"):js.index("`;", js.index("const CSS=`"))]
    assert not __import__("re").search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|var\(--green\)", css)
    from rook.remote.knowledge_web import CcmodWeb
    assert CcmodWeb.ASSETS == ("ccmod.js",) and (root / "rook" / "web" / "ccmod.js").is_file()
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", str(root / "rook" / "web" / "ccmod.js")]).returncode == 0
    # The hook stays standalone: it is copied out of the bundle and run with -I -S.
    src = (root / "rook" / "worker" / "ccmod_hook.py").read_text(encoding="utf-8")
    assert "from rook" not in src and "import rook" not in src and "from ." not in src
    assert sys.version_info >= (3, 8)
