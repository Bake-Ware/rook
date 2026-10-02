"""The persona plugin end to end on an isolated test hub (opt-in).

A profile set and assigned through ``persona.*`` on worker "rook" shows up in
a new MCP session's initialize instructions, and a real worker installs it
with ``persona.apply``: it fetches the text from the hub over the band,
writes the marker block into its (sandboxed) ~/.claude/CLAUDE.md without
touching the rest, reports it in ``persona.status`` and removes it again.
Everything is undone at the end so other tests see no persona.
"""

from __future__ import annotations

import asyncio

PROFILE = {"id": "it-neutral", "name": "Example", "voice": "Plain and brief.",
           "rules": ["Say what you checked."],
           "addenda": {"claude-code": "Use the task list for multi-step work."}}
MARK = "<!-- rook:persona:begin"


def _rook(hub, cap, **args):
    reply = hub.call("rook_call", cap=cap, worker="rook", args=args)
    assert isinstance(reply, dict) and reply.get("ok"), reply
    return reply["result"]


def _instructions(hub) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def go():
        headers = {"Authorization": f"Bearer {hub.token}"}
        async with create_mcp_http_client(headers=headers) as http:
            async with streamable_http_client(hub.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as s:
                    return (await s.initialize()).instructions or ""
    return asyncio.run(asyncio.wait_for(go(), 60))


def test_persona_set_instructions_and_apply_on_a_worker(hub):
    worker = hub.workers[0]
    before = _instructions(hub)
    assert "## Persona" not in before
    try:
        assert _rook(hub, "persona.set", profile=PROFILE, note="it")["rev"] >= 1
        _rook(hub, "persona.assign", scope="default", profile="it-neutral")
        text = _instructions(hub)
        assert text.startswith(before.strip() + "\n\n## Persona: Example")
        got = _rook(hub, "persona.get", family="claude-code")
        assert "Use the task list" in got["text"] and got["source"]["scope"] == "default"

        seed = hub.call("rook_call", cap="shell.exec", worker=worker, args={
            "cmd": "mkdir -p ~/.claude && printf '# mine\\n\\nkeep me\\n' > ~/.claude/CLAUDE.md"})
        assert seed["ok"], seed
        dry = hub.call("rook_call", cap="persona.apply", worker=worker,
                       args={"harness": "claude-code", "dry_run": True})
        assert dry["ok"] and dry["result"]["action"] == "insert", dry
        done = hub.call("rook_call", cap="persona.apply", worker=worker,
                        args={"harness": "claude-code"})
        assert done["ok"] and done["result"]["action"] == "insert", done
        again = hub.call("rook_call", cap="persona.apply", worker=worker,
                         args={"harness": "claude-code"})
        assert again["result"]["action"] == "unchanged"
        cat = hub.call("rook_call", cap="shell.exec", worker=worker,
                       args={"cmd": "cat ~/.claude/CLAUDE.md"})
        body = cat["result"]["stdout"]
        assert body.startswith("# mine\n\nkeep me\n\n" + MARK) and "Use the task list" in body
        status = hub.call("rook_call", cap="persona.status", worker=worker,
                          args={"harness": "claude-code"})
        assert status["result"][0]["block"] and status["result"][0]["profile"] == "it-neutral"
        gone = hub.call("rook_call", cap="persona.apply", worker=worker,
                        args={"harness": "claude-code", "remove": True})
        assert gone["result"]["action"] == "remove"
        cat = hub.call("rook_call", cap="shell.exec", worker=worker,
                       args={"cmd": "cat ~/.claude/CLAUDE.md"})
        assert cat["result"]["stdout"] == "# mine\n\nkeep me\n"
    finally:
        hub.call("rook_call", cap="persona.assign", worker="rook",
                 args={"scope": "default", "profile": ""})
        hub.call("rook_call", cap="persona.delete", worker="rook", args={"id": "it-neutral"})
    assert "## Persona" not in _instructions(hub)
