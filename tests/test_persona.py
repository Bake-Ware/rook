"""The persona plugin: profiles and scoped resolution on the hub, the managed
marker block in harness files (idempotent, never clobbering), MCP initialize
instructions, work launch arguments, the skill overlay and the voice settings
mapping. Persona text here is neutral example content."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from rook.worker.plugins import persona as wp

PROFILE = {"id": "steady", "name": "Example", "owner": "the operator",
           "voice": "Calm and direct.", "rules": ["Say what you verified."],
           "do": ["Lead with the answer."], "dont": ["Pad replies."],
           "formatting": "Short paragraphs.",
           "addenda": {"claude": "Prefer the task list for multi-step work.",
                       "codex": "Keep diffs small."}}
USER_CONTENT = "# My notes\n\nKeep this exactly.\n\n- item one\n- item two\n"


# -- profiles, versions, resolution -------------------------------------------


# -- the managed marker block ----------------------------------------------------

SAMPLES = {
    "plain": USER_CONTENT,
    "empty": "",
    "one-line": "line\n",
    "crlf": "# Notes\r\n\r\nKeep CRLF.\r\n",
    "with-comments": "<!-- a user comment -->\n# Title\n\n<!-- rook:other -->\ntext\n",
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_upsert_strip_round_trip_and_idempotence(name):
    original = SAMPLES[name]
    nl = "\r\n" if "\r\n" in original else "\n"
    block = wp.make_block("## Persona: Example\nBe brief.\n", "steady", 1, nl)
    once = wp.upsert(original, block)
    assert once.startswith(original)                                   # nothing before it changed
    assert wp.upsert(once, block) == once                              # idempotent
    assert wp.strip(once) == original                                  # exact round trip
    assert wp.strip(original) == original                              # nothing to remove
    if nl == "\r\n":
        assert "\n" not in once.replace("\r\n", "")                    # line endings kept


def test_update_replaces_only_the_block_even_when_moved():
    old = wp.make_block("old text", "steady", 1)
    text = "top\n\n" + old + "\n\nbottom stays\n"
    new = wp.make_block("new text", "steady", 2)
    out = wp.upsert(text, new)
    assert out == "top\n\n" + new + "\n\nbottom stays\n"
    assert wp.block_attrs(out) == {"profile": "steady", "rev": "2", "sha": wp.fingerprint("new text")}


def test_damaged_markers_are_refused():
    begin = wp.make_block("x").splitlines()[0]
    for text in (begin + "\nno end\n", "x\n" + wp.END + "\n",
                 wp.make_block("a") + "\n" + wp.make_block("b") + "\n",
                 wp.END + "\n" + begin + "\n"):
        with pytest.raises(wp.MarkerError):
            wp.upsert(text, wp.make_block("new"))
        with pytest.raises(wp.MarkerError):
            wp.strip(text)


class FakeWorker:
    def __init__(self, reply=None):
        self.reply = reply
        self.calls = []

    async def request(self, cap, args=None, target=None, timeout=10.0):
        self.calls.append((cap, args))
        if self.reply is None:
            raise TimeoutError()
        return self.reply


@pytest.fixture
def harness_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return tmp_path


@pytest.mark.asyncio
async def test_apply_cap_dry_run_write_repeat_and_remove(harness_home):
    target = harness_home / "claude" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text(USER_CONTENT)
    os.chmod(target, 0o600)
    worker = FakeWorker({"ok": True, "result": {"text": "## Persona: Example\nBe brief.\n",
                                                "profile": "steady", "rev": 3}})
    p = wp.PersonaFiles()
    p.bind_worker(worker)
    dry = await p.apply("claude", dry_run=True)
    assert dry["action"] == "insert"
    assert "+<!-- rook:persona:begin profile=steady rev=3" in dry["diff"]
    assert target.read_text() == USER_CONTENT                         # dry run wrote nothing
    assert worker.calls == [("persona.render", {"harness": "claude-code"})]
    done = await p.apply("claude-code")
    assert done["action"] == "insert" and target.read_text().startswith(USER_CONTENT)
    assert oct(target.stat().st_mode & 0o777) == "0o600"               # mode kept
    again = await p.apply("claude-code")
    assert again["action"] == "unchanged" and "diff" not in again
    status = p.status("claude-code")[0]
    assert status["block"] and status["rev"] == "3" and status["profile"] == "steady"
    gone = await p.apply("claude-code", remove=True)
    assert gone["action"] == "remove" and target.read_text() == USER_CONTENT
    assert (await p.apply("claude-code", remove=True))["action"] == "absent"


@pytest.mark.asyncio
async def test_apply_creates_missing_files_and_removes_them_cleanly(harness_home):
    p = wp.PersonaFiles()
    res = await p.apply("hermes", content="Be kind.")
    soul = harness_home / "hermes" / "SOUL.md"
    assert res["action"] == "create" and soul.read_text().startswith(wp.BEGIN)
    res = await p.apply("hermes", remove=True)
    assert res["deleted_empty_file"] and not soul.exists()


@pytest.mark.asyncio
async def test_apply_follows_symlinks_and_limits_paths(harness_home, tmp_path):
    real = tmp_path / "dotfiles" / "agents.md"
    real.parent.mkdir()
    real.write_text("mine\n")
    link = tmp_path / "proj" / "AGENTS.md"
    link.parent.mkdir()
    link.symlink_to(real)
    p = wp.PersonaFiles()
    await p.apply("codex", path=str(link), content="Be kind.")
    assert link.is_symlink() and real.read_text().startswith("mine\n\n" + wp.BEGIN)
    for bad in ("relative/AGENTS.md", str(tmp_path / "notes.md"), str(tmp_path / ".bashrc")):
        with pytest.raises(ValueError):
            await p.apply("codex", path=bad, content="x")
    with pytest.raises(ValueError):
        await p.apply("vim", content="x")
    with pytest.raises(ValueError):                                   # hub silent, no content
        await p.apply("codex")
    damaged = tmp_path / "bad" / "CLAUDE.md"
    damaged.parent.mkdir()
    damaged.write_text(wp.END + "\n")
    with pytest.raises(wp.MarkerError):
        await p.apply("claude-code", path=str(damaged), content="x")
    assert damaged.read_text() == wp.END + "\n"


@pytest.mark.asyncio
async def test_fetch_backs_off_after_a_silent_hub(monkeypatch):
    monkeypatch.setattr(wp, "_silent_until", 0.0)
    silent = FakeWorker(None)
    assert await wp.fetch_persona(silent, "claude") is None
    assert await wp.fetch_persona(silent, "claude") is None
    assert len(silent.calls) == 1                                     # second launch didn't wait
    assert await wp.fetch_persona(silent, "claude", backoff=False) is None
    assert len(silent.calls) == 2                                     # explicit apply still asks
    refused = FakeWorker({"ok": False, "error": "no profile"})
    assert await wp.fetch_persona(refused, "codex", "x", backoff=False) is None
    assert refused.calls == [("persona.render", {"harness": "codex", "profile": "x"})]


# -- work launch templates ----------------------------------------------------------

def test_launch_persona_args():
    from rook.worker.plugins.terminals import build_argv, persona_args
    text = "## Persona: Example\nBe brief.\n"
    assert build_argv("claude", "/bin/claude", persona=text)[-2:] == [
        "--append-system-prompt", text.strip()]
    codex = build_argv("codex", "/bin/codex", persona=text)
    assert codex[-2] == "-c" and codex[-1] == "developer_instructions=" + json.dumps(text.strip())
    assert build_argv("hermes", "/bin/hermes", persona=text) == ["/bin/hermes"]
    assert persona_args("claude", "") == [] and persona_args("claude", "x" * 9000) == []


@pytest.mark.asyncio
async def test_terminal_launch_fetches_the_persona(monkeypatch, tmp_path):
    from rook.worker.plugins import terminals
    monkeypatch.setattr(wp, "_silent_until", 0.0)
    monkeypatch.setenv("ROOK_WORK_TERM_DIR", str(tmp_path / "terms"))
    monkeypatch.setattr(terminals, "_binary", lambda h: "/bin/true")
    seen = {}

    async def fake_spawn(self, t, argv, cwd, env):
        seen.update(argv=argv, env=dict(env))
        t.pid, t.proc = 1, None
    monkeypatch.setattr(terminals.TerminalsPlugin, "_spawn", fake_spawn)
    plugin = terminals.TerminalsPlugin()
    worker = FakeWorker({"ok": True, "result": {"text": "Be brief.\n", "profile": "steady"}})
    plugin.bind_worker(worker)
    await plugin.open(harness="claude", cwd=str(tmp_path), persona="steady")
    assert worker.calls == [("persona.render", {"harness": "claude", "profile": "steady"})]
    assert seen["argv"][-2:] == ["--append-system-prompt", "Be brief."]
    assert Path(seen["env"]["ROOK_PERSONA_FILE"]).read_text() == "Be brief.\n"
    assert seen["env"]["ROOK_PERSONA"] == "steady"


# -- hub caps --------------------------------------------------------------------------


# -- MCP initialize instructions -------------------------------------------------------------

STATIC = "static-token-0123456789abcdef"


