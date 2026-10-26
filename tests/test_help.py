"""The layered help cap and its just-in-time notes (skills/rook/help/*.md)."""
import asyncio
import json
import re
from pathlib import Path

import pytest

from rook.band_mcp import help_topics, helpnotes, skill
from rook.core.pluginkit import CapError
from rook.hub.plugins.help import Help

ROOT = Path(__file__).resolve().parent.parent
TABLE = (ROOT / "skills/rook/references/tools.md").read_text()
TOOLS = set(re.findall(r"^\| `(rook_[a-z_]+)` \|", TABLE, re.M))
CAPS = set(re.findall(r"^\| `([a-z][a-z0-9_.\-]*)` \|", TABLE, re.M)) - TOOLS
REPLY_MOMENTS = {"held", "timeout"}


@pytest.fixture
def lib():
    return help_topics.library()


def test_topics_parse_and_core_comes_first(lib):
    assert help_topics.CORE in lib.topics
    names = {t["topic"] for t in lib.menu()}
    assert {"machines", "tasks", "handoffs", "knowledge", "person", "chat", "secrets",
            "jobs", "holds", "admin"} <= names
    for t in lib.topics.values():
        assert t.title and t.pitch and t.body.startswith("# ")


def test_topics_name_real_tools_caps_and_pages(lib):
    # Help that names a tool, cap or page that doesn't exist is worse than none.
    claimed = {}
    for t in lib.topics.values():
        for tool in t.tools:
            assert tool in TOOLS, (t.name, tool)
            assert claimed.setdefault(tool, t.name) == t.name, f"{tool} in {claimed[tool]} and {t.name}"
        for prefix in t.caps:
            assert any(c.startswith(prefix) for c in CAPS), (t.name, prefix)
        for ref in t.refs:
            assert ref in skill.base_files() or ref == skill.SITE_FILE, (t.name, ref)


def test_notes_are_keyed_on_real_moments(lib):
    from rook.hub.plugins.huddle import READS as HR, WRITES as HW
    from rook.hub.plugins.knowledge import READS
    from rook.hub.plugins.knowledge.service import WRITES
    actions = {"rook_task": set(READS) | set(WRITES), "rook_knowledge": set(READS) | set(WRITES),
               "rook_huddle": set(HR) | set(HW)}
    seen = set()
    for t in lib.topics.values():
        for key, text in t.notes.items():
            assert key not in seen, key
            seen.add(key)
            assert text and len(text) <= 400, key
            tool, _, action = key.partition(".")
            if key in REPLY_MOMENTS:
                continue
            assert tool in t.tools, (t.name, key)
            if action:
                assert action in actions[tool], key


def test_skill_is_a_stub_that_lists_every_topic(lib):
    text = skill.base_files()["SKILL.md"]
    assert len(text.splitlines()) <= 80
    assert 'cap="help"' in text
    for name in lib.topics:
        assert f"`help/{name}.md`" in text, name


def test_help_layers(lib):
    h = Help()
    top = h.help()
    assert "session loop" in top["core"] and any(m["topic"] == "tasks" for m in top["topics"])
    assert "help" in top["next"]

    tasks = h.help(topic="tasks")
    assert tasks["title"].startswith("Tasks") and "## Hygiene" in tasks["text"]
    assert "reference" not in tasks and "detail" in tasks["more"]
    deep = h.help(topic="tasks", detail=True)
    assert "references/coordination.md" in deep["reference"]

    assert h.help(topic="rook_jobs")["topic"] == "jobs"           # by tool
    assert h.help(topic="shell.exec")["topic"] == "machines"      # by cap prefix
    assert h.help(topic="Rook_Task")["topic"] == "tasks"
    words = h.help(topic="timed out retry")
    assert words["matches"][0]["topic"] == "machines"
    assert h.help(topic="core")["topic"] == "core"
    with pytest.raises(CapError, match="no help topic"):
        h.help(topic="zzzz qqqq")


def test_hub_plugin_fragments_are_topics_too():
    class P:
        def __init__(self, ns, skill_text):
            self.NAMESPACE, self.NAME, self.SKILL = ns, ns, skill_text

        def caps(self):
            return {f"{self.NAMESPACE}.go"}

    class Host:
        plugins = [P("voice", "### voice\nSpeak on a phone.\n"), P("quiet", "")]

    h = Help()
    h.bind_host(type("Node", (), {"host": Host()})())
    assert h.help()["plugins"] == ["voice"]
    got = h.help(topic="voice")
    assert got["text"].startswith("### voice") and got["caps"] == ["voice.go"]


@pytest.mark.asyncio
async def test_hub_serves_the_cap(tmp_path):
    from tests.plugins_fakes import make_node
    node = make_node(tmp_path)
    await node.start()
    try:
        assert node.host.registry.handler("help") is not None
        out = await node.invoke("help", {"topic": "knowledge"})
        assert out["topic"] == "knowledge"
    finally:
        await node.stop()


def test_notes_once_per_session_most_specific_first(lib):
    notes = helpnotes.HelpNotes()
    ok = json.dumps({"ok": True, "result": {}})
    first = json.loads(notes.annotate("s1", "rook_task", {"action": "claim"}, ok))
    assert first["_help"].startswith("Claimed.") and '"topic": "tasks"' in first["_help"]
    # Each note comes once: next the tool's general one, then nothing.
    again = json.loads(notes.annotate("s1", "rook_task", {"action": "claim"}, ok))
    assert again["_help"].startswith("Claim before working")
    assert "_help" not in json.loads(notes.annotate("s1", "rook_task", {"action": "get"}, ok))
    assert "_help" not in json.loads(notes.annotate("s1", "rook_task", {"action": "claim"}, ok))
    # A new session hears it again; a tool with no note stays quiet.
    assert "_help" in json.loads(notes.annotate("s2", "rook_task", {"action": "claim"}, ok))
    assert notes.annotate("s1", "rook_whoami", {}, ok) == ok


def test_reply_moments_and_non_json(lib):
    notes = helpnotes.HelpNotes()
    held = json.dumps({"ok": False, "held": True, "hold": "h1"})
    assert json.loads(notes.annotate("s", "rook_call", {}, held))["_help"].startswith("Held")
    late = json.dumps({"ok": False, "error": "no reply from worker 'a' within 35s"})
    assert "rook_journal" in json.loads(notes.annotate("s", "rook_call", {}, late))["_help"]
    plain = json.dumps({"ok": True, "result": 1})
    assert notes.annotate("s", "rook_call", {}, plain) == plain       # cap tips cover rook_call
    assert notes.annotate("s", "rook_task", {"action": "deck"}, "plain text") == "plain text"
    assert notes.annotate("s", "rook_task", {"action": "deck"}, "[1, 2]") == "[1, 2]"


def test_a_broken_library_never_breaks_a_reply():
    def boom():
        raise OSError("gone")

    class Tool:
        is_async = True

        async def fn(self, **kw):
            return '{"ok": true}'

    tool = Tool()
    tool.fn = Tool.fn.__get__(tool)

    class Mcp:
        class _tool_manager:
            _tools = {"rook_task": tool}

    helpnotes.install(Mcp(), lambda: "s", helpnotes.HelpNotes(boom))
    assert asyncio.run(tool.fn(action="deck")) == '{"ok": true}'
    helpnotes.install(Mcp(), lambda: "s")                      # wrapping twice is a no-op
    assert getattr(tool.fn, "_rook_helpnotes", False) and tool.fn.__wrapped__.__name__ == "fn"


@pytest.mark.asyncio
async def test_notes_ride_real_tool_replies(tmp_path):
    from rook.band_mcp.server import build_server

    class Band:
        workers = {"w1": {"worker_id": "w1", "name": "box", "band": "x", "caps": ["shell.exec"],
                          "last_seen": 0}}

        async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
            return {"id": "c", "from": target, "ok": False,
                    "error": "no reply from worker 'box' within 35s"}

    mcp, _ = build_server(Band(), public_url="https://mcp.example.com",
                          persist_path=str(tmp_path / "tokens.json"), static_token="s" * 32,
                          journal_path=str(tmp_path / "journal.db"))
    instructions = mcp._mcp_server.instructions
    assert 'cap=help' in instructions and "_help" in instructions

    def text(res):
        res = res[0] if isinstance(res, tuple) else res
        return res[0].text

    args = {"worker": "box", "cap": "shell.exec", "why": "test", "args": {"argv": ["true"]}}
    first = json.loads(text(await mcp.call_tool("rook_call", args)))
    assert "rook_journal" in first["_help"]
    second = json.loads(text(await mcp.call_tool("rook_call", args)))
    assert "_help" not in second
