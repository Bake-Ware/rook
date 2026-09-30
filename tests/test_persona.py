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

from rook.hub.plugins.persona import model
from rook.hub.plugins.persona.model import PersonaError, PersonaStore
from rook.worker.plugins import persona as wp

PROFILE = {"id": "steady", "name": "Example", "owner": "the operator",
           "voice": "Calm and direct.", "rules": ["Say what you verified."],
           "do": ["Lead with the answer."], "dont": ["Pad replies."],
           "formatting": "Short paragraphs.",
           "addenda": {"claude": "Prefer the task list for multi-step work.",
                       "codex": "Keep diffs small."}}
USER_CONTENT = "# My notes\n\nKeep this exactly.\n\n- item one\n- item two\n"


def _migrate(conn):
    from rook.core.migrations import apply
    apply(conn, Path(model.__file__).parent / "migrations", "persona")


@pytest.fixture
def store(tmp_path):
    return PersonaStore(tmp_path / "persona.db", _migrate)


# -- profiles, versions, resolution -------------------------------------------

def test_validate_normalizes_and_rejects():
    doc = model.validate({**PROFILE, "rules": "- one\n\n* two\n"})
    assert doc["rules"] == ["one", "two"]
    assert set(doc["addenda"]) == {"claude-code", "codex"}          # alias normalized
    for bad in ({"id": "Bad Id", "name": "x"}, {"id": "ok"}, {"id": "ok", "name": "x", "evil": 1},
                {"id": "ok", "name": "x" * 61}, {"id": "ok", "name": "x", "rules": ["r"] * 21},
                {"id": "ok", "name": "x", "addenda": {"Not a family!": "x"}}):
        with pytest.raises(PersonaError):
            model.validate(bad)


def test_render_picks_the_harness_addendum_only():
    doc = model.validate(PROFILE)
    text = model.render(doc, "claude")
    assert text.startswith("## Persona: Example\nYou are Example, working for the operator.")
    assert "Prefer the task list" in text and "Keep diffs small" not in text
    assert "Don't:\n- Pad replies." in text
    assert "Prefer the task list" not in model.render(doc)
    assert model.render(None) == ""


def test_compact_fits_the_budget_at_a_line_boundary():
    doc = model.validate({"id": "long", "name": "N", "rules": [f"rule {i} " + "x" * 200 for i in range(20)]})
    full = model.render(doc)
    small = model.compact(full, 600)
    assert len(full) > 600 >= len(small)
    assert small.endswith("worker=rook for the rest.)")
    assert model.compact("short") == "short"


def test_versions_history_and_stale_edits(store):
    r1 = store.save(PROFILE, "human:op", "first")
    assert r1["rev"] == 1 and r1["changed"] == ["created"]
    assert store.save(PROFILE, "human:op")["unchanged"]            # same doc: no new rev
    r2 = store.save({**PROFILE, "voice": "Warm."}, "agent:ops", expect_rev=1)
    assert r2["rev"] == 2 and r2["changed"] == ["voice"]
    with pytest.raises(PersonaError):
        store.save({**PROFILE, "voice": "Other."}, "agent:ops", expect_rev=1)
    dry = store.save({**PROFILE, "voice": "Dry."}, "agent:ops", dry_run=True)
    assert dry["dry_run"] and store.profile("steady")["voice"] == "Warm."
    hist = store.history("steady")
    assert [h["rev"] for h in hist] == [2, 1] and hist[0]["actor"] == "agent:ops"
    assert hist[1]["doc"]["voice"] == "Calm and direct."


def test_resolution_order_and_delete_guard(store):
    for pid, name in (("base", "Base"), ("fam", "Fam"), ("mine", "Mine"), ("band", "Band")):
        store.save({"id": pid, "name": name}, "human:op")
    assert store.resolve() == (None, {})
    store.assign("default", "ignored", "base", "human:op")
    store.assign("band", "b1", "band", "human:op")
    store.assign("family", "claude", "fam", "human:op")               # alias -> claude-code
    store.assign("user", "agent_1", "mine", "human:op")
    name = lambda *a, **k: store.resolve(*a, **k)[0]["name"]
    assert name() == "Base"
    assert name(band="b1") == "Band"
    assert name(fam="claude-code", band="b1") == "Fam"
    assert name(["agent_1"], "claude-code", "b1") == "Mine"
    doc, src = store.resolve(["nobody"], "codex")
    assert doc["name"] == "Base" and src == {"scope": "default", "target": "", "profile": "base"}
    with pytest.raises(PersonaError):
        store.delete("base", "human:op")                              # still assigned
    store.assign("default", "", "", "human:op")                       # unassign
    assert store.delete("base", "human:op")["deleted"]
    with pytest.raises(PersonaError):
        store.assign("family", "", "fam", "human:op")
    with pytest.raises(PersonaError):
        store.assign("user", "u", "missing", "human:op")
    assert store.history("default:")[0]["doc"] is None


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

@pytest.fixture
def node(tmp_path):
    from rook.hub.node import HubNode
    from rook.hub.settings_store import SettingsStore
    return HubNode(str(tmp_path), entry_points=False,
                   settings_store=SettingsStore(tmp_path / "settings.db"))


@pytest.mark.asyncio
async def test_hub_caps_tiers_and_admin_gate(node):
    from rook.hub.authz import current_principal
    from rook.hub.policy import Principal
    meta = node.host.registry.meta
    assert {c: meta(f"persona.{c}").risk for c in ("get", "list", "render", "history", "set",
                                                    "assign", "delete")} == {
        "get": "read", "list": "read", "render": "read", "history": "read",
        "set": "admin", "assign": "admin", "delete": "admin"}
    # An agent-role token is refused whatever the policy mode.
    tok = current_principal.set(Principal("token:agent_x", "token", "agent"))
    try:
        res = await node.dispatch("persona.set", {"profile": PROFILE}, "agent:x")
        assert not res["ok"] and "denied" in res["error"]
        dry = await node.dispatch("persona.set", {"profile": PROFILE, "dry_run": True})
        assert dry["ok"] and dry["result"]["dry_run"]                 # a preview needs no admin
    finally:
        current_principal.reset(tok)
    tok = current_principal.set(Principal("token:op", "token", "operator"))
    try:
        assert (await node.dispatch("persona.set", {"profile": PROFILE}, "agent:op"))["ok"]
        assert (await node.dispatch("persona.assign", {"scope": "family", "target": "codex",
                                                       "profile": "steady"}))["ok"]
    finally:
        current_principal.reset(tok)
    # Workers fetch over the band: read caps pass, writes don't.
    got = await node.dispatch("persona.render", {"harness": "codex"}, "worker:w", source="band")
    assert got["ok"] and "Keep diffs small." in got["result"]["text"]
    assert got["result"]["sha"] == model.fingerprint(got["result"]["text"])
    none = await node.dispatch("persona.render", {"harness": "hermes"}, source="band")
    assert none["result"] == {"text": "", "profile": None}
    denied = await node.dispatch("persona.assign", {"scope": "default", "profile": "steady"},
                                 source="band")
    assert not denied["ok"]
    listed = (await node.dispatch("persona.list", {}))["result"]
    assert listed["profiles"][0]["id"] == "steady" and listed["assignments"][0]["scope"] == "family"
    one = (await node.dispatch("persona.get", {"family": "codex"}))["result"]
    assert one["source"]["scope"] == "family" and one["profile"]["rev"] == 1
    hist = (await node.dispatch("persona.history", {"id": "steady"}))["result"]
    assert hist[0]["actor"] == "agent:op"


def test_voice_settings_mapping(tmp_path, node):
    plugin = node.plugin("persona")
    plugin.store.save(PROFILE, "human:op")
    plugin.store.save({"id": "u2", "name": "Other", "owner": "Someone"}, "human:op")
    plugin.store.assign("family", "voice", "steady", "human:op")
    plugin.store.assign("user", "u2", "u2", "human:op")
    svc = node.settings
    svc.set("core.settings.service_readers", {"voice": ["agent_v"]})
    reader = {"kind": "agent", "label": "voice", "agent_id": "agent_v", "verified": True}
    got = svc.fetch("voice", reader)
    assert got["values"]["assistant_name"] == "Example" and got["values"]["owner"] == "the operator"
    assert got["users"]["u2"] == {"assistant_name": "Other", "owner": "Someone"}
    svc.set("voice.assistant_name", "Explicit")                      # an explicit value wins
    assert svc.fetch("voice", reader)["values"]["assistant_name"] == "Explicit"
    assert svc.fetch("voice", reader)["values"]["owner"] == "the operator"


def test_skill_overlay_note(node, monkeypatch):
    from rook.band_mcp import skill
    monkeypatch.delenv("ROOK_SKILL_SITE_PAGE", raising=False)
    plugin = node.plugin("persona")
    assert skill.site_notes(None, plugin) is None                     # nothing assigned
    plugin.store.save(PROFILE, "human:op")
    plugin.store.assign("default", "", "steady", "human:op")
    note = skill.site_notes(None, plugin)
    assert note.startswith("# Persona") and "You are Example" in note
    site = Path(node._state_dir) / "site.md"
    site.write_text("# Our band\n\nworker-a builds.\n")
    monkeypatch.setenv("ROOK_SKILL_SITE_FILE", str(site))
    both = skill.site_notes(None, plugin)
    assert both.startswith("# Our band") and "\n\n# Persona" in both


@pytest.mark.asyncio
async def test_settings_page_api(node):
    from starlette.applications import Starlette
    from rook.hub.settings_web import routes
    accounts = SimpleNamespace(session=lambda c: {"id": c, "username": c, "csrf": "k",
                                                  "admin": c == "op"} if c else None)
    app = Starlette(routes=routes(lambda: node.settings, accounts))
    op, member = {"Cookie": "rook_account=op"}, {"Cookie": "rook_account=m1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        api = "/settings/account-api"
        assert (await c.get(api, headers=member, params={"view": "persona"})).status_code == 403
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "persona_save",
                                                "profile": PROFILE, "rev": 0})
        assert r.status_code == 200 and r.json()["rev"] == 1
        r = await c.post(api, headers=member, json={"csrf": "k", "action": "persona_assign",
                                                    "scope": "default", "profile": "steady"})
        assert r.status_code == 403
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "persona_assign",
                                                "scope": "default", "profile": "steady"})
        assert r.status_code == 200
        bad = await c.post(api, headers=op, json={"csrf": "k", "action": "persona_save",
                                                  "profile": {"id": "Bad Id"}})
        assert bad.status_code == 400
        page = (await c.get(api, headers=op, params={"view": "persona"})).json()
        assert page["profiles"][0]["text"].startswith("## Persona: Example")
        assert page["assignments"][0]["profile"] == "steady"
        assert page["history"][0]["actor"] == "human:op"


# -- MCP initialize instructions -------------------------------------------------------------

STATIC = "static-token-0123456789abcdef"


@asynccontextmanager
async def mcp_hub(tmp_path, monkeypatch):
    from rook.band_mcp.server import build_server
    monkeypatch.setenv("ROOK_KNOWLEDGE", "0")

    class Band:
        workers: dict = {}

        async def call(self, *a, **k):
            return {"ok": True, "result": {}}
    mcp, store = build_server(Band(), public_url="https://mcp.example.com",
                              persist_path=str(tmp_path / "tokens.json"), static_token=STATIC,
                              journal_path=str(tmp_path / "journal.db"))
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Accept": "application/json, text/event-stream"}) as http:
        async def initialize(**headers):
            h = {"Authorization": "Bearer " + STATIC, **headers}
            r = await http.post("/mcp", headers=h, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"}}})
            body = r.json() if r.headers["content-type"].startswith("application/json") else \
                json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith("data:")))
            return body["result"].get("instructions")
        yield SimpleNamespace(mcp=mcp, initialize=initialize,
                              plugin=mcp._rook_hub.plugin("persona"),
                              server_text=mcp._rook_guidance[0].get("server"))


@pytest.mark.asyncio
async def test_initialize_instructions_compose_server_slot_and_persona(tmp_path, monkeypatch):
    async with mcp_hub(tmp_path, monkeypatch) as env:
        # Nothing assigned: exactly the server slot (the token budget is unchanged).
        assert await env.initialize() == env.server_text
        env.plugin.store.save(PROFILE, "human:op")
        env.plugin.store.save({"id": "tok", "name": "Token persona"}, "human:op")
        env.plugin.store.assign("default", "", "steady", "human:op")
        text = await env.initialize()
        assert text.startswith(env.server_text + "\n\n## Persona: Example")
        assert "Prefer the task list" not in text and "Keep diffs small" not in text
        # Family from the client: explicit header, else the User-Agent.
        env.plugin.store.assign("family", "codex", "steady", "human:op")
        assert "Keep diffs small." in await env.initialize(**{"X-Rook-Client": "codex"})
        assert "Prefer the task list" in await env.initialize(**{"User-Agent": "claude-code/2.1"})
        # User scope: the token's label.
        env.plugin.store.assign("user", "static", "tok", "human:op")
        assert "## Persona: Token persona" in await env.initialize()
        # The persona part stays within its budget.
        env.plugin.store.save({"id": "tok", "name": "Big", "rules": ["x" * 290] * 20}, "human:op")
        big = await env.initialize()
        assert len(big) - len(env.server_text) <= model.MCP_BUDGET + 2


@pytest.mark.asyncio
async def test_initialize_survives_a_broken_persona_plugin(tmp_path, monkeypatch):
    async with mcp_hub(tmp_path, monkeypatch) as env:
        def boom(*a, **k):
            raise RuntimeError("store gone")
        monkeypatch.setattr(env.plugin, "instructions_for", boom)
        assert await env.initialize() == env.server_text


def test_compose_instructions():
    from rook.band_mcp.guidance import compose_instructions
    assert compose_instructions("server", "") == "server"
    assert compose_instructions("server", None, " persona \n") == "server\n\npersona"
    assert compose_instructions("", "") is None
