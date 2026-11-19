"""The house agent on the hub, thinking through an engine on a worker (brief HA2).

Everything here goes through the MCP door (POST /mcp) with an agent token, to a real served hub
and a real worker over the real carrier. The engine is a stand-in (tests/plugins/echo-engine), a
worker cap with the engine's arguments that echoes, remembers by session id on the worker's disk
and calls the door with the turn's token, so the suite needs no Claude login. A real Claude Code
run is in the pull request.
"""

import asyncio
import os
import pathlib
import time

import aiohttp

from rook_hub import Hub
from rook_worker import Worker

ECHO = str(pathlib.Path(__file__).parent / "plugins" / "echo-engine")
WHY = "The house agent's tests (brief HA2)."


class Rig:
    """A served hub with the stand-in engine as the house agent's, an agent token, one worker."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.port = 0

    async def start_hub(self):
        self.hub = Hub(self.tmp_path / "hub", vault_key=bytes(32))
        host, self.port = await self.hub.serve("127.0.0.1", self.port)
        self.url = f"http://{host}:{self.port}"

    async def stop_hub(self):
        await self.hub.stop_serving()
        self.hub.close()

    def start_worker(self):
        self.worker = Worker(self.tmp_path / "box", self.url, name="box", macs=[], knock_every=0.02)
        self.task = asyncio.create_task(self.worker.run())

    async def stop_worker(self):
        self.worker.stop()
        await asyncio.gather(self.task, return_exceptions=True)

    async def __aenter__(self):
        await self.start_hub()
        self.user = self.hub.auth.create_user("Alice", "a password", first=True)
        self.token = self.hub.auth.mint("agent", user=self.user["guid"])
        self.http = aiohttp.ClientSession()
        self.start_worker()
        while not (await self.tool("workers"))[1]["knocking"]:
            await asyncio.sleep(0.02)
        key = (await self.tool("workers"))[1]["knocking"][0]["key"]
        self.moniker = (await self.ok("hub", "worker.approve", key=key))["approved"]["moniker"]
        await self.ok("hub", "plugin.install", path=ECHO)
        await self.ok("hub", "setting.set", plugin="agents", key="backend", value="echo-engine.turn")
        # A new hub starts in setup, and its house turns carry the first-run skill until it is done (HA3).
        # These tests are about turns, not setup (tests/test_wizard.py is): setup is done here.
        await self.ok("hub", "agent.setup", done=True)
        await self.linked()
        return self

    async def linked(self):
        """The worker is online and announces the engine, so the house agent can think."""
        while not (await self.ok("hub", "agent.site"))["ready"]:
            await asyncio.sleep(0.02)

    async def tool(self, name, token=None, **arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        headers = {"Authorization": f"Bearer {token or self.token}"}
        async with self.http.post(self.url + "/mcp", json=body, headers=headers) as reply:
            result = (await reply.json())["result"]
        text = result["content"][0]["text"]
        import json
        return result["isError"], (text if result["isError"] else json.loads(text))

    async def ok(self, worker, cap, /, **args):
        failed, reply = await self.tool("call", worker=worker, cap=cap, args=args, why=WHY)
        assert not failed, reply
        return reply

    async def say(self, text, **args):
        return await self.ok("hub", "agent.chat.send", agent="house", text=text, **args)

    async def events(self, after=0, **args):
        return await self.ok("hub", "agent.chat.read", agent="house", after=after, **args)

    async def until(self, check, after=0):
        """Read the conversation until check(events) holds; return the events."""
        async with asyncio.timeout(20):
            while True:
                got = (await self.events(after))["events"]
                if check(got):
                    return got
                await asyncio.sleep(0.05)

    async def __aexit__(self, *exc):
        await self.stop_worker()
        await self.http.close()
        await self.stop_hub()


def run(tmp_path, body):
    async def main():
        async with Rig(tmp_path) as rig:
            await body(rig)
    asyncio.run(main())


def replies(events):
    return [e["data"]["text"] for e in events if e["type"] == "turn" and e["data"]["role"] == "reply"]


def test_send_and_read_through_the_door(tmp_path):
    """agent.chat.send answers the reply with 0.5's shape; agent.chat.read gives the conversation as
    0.5's events, with seq and next as cursors; the turn reached the door with its own token, which
    is revoked once the turn ends."""
    async def body(rig):
        assert (await rig.ok("hub", "agent.list"))["agents"][0]["name"] == "house"
        sent = await rig.say("hello there")
        assert sent["agent"] == "house" and sent["seq"] == 1 and sent["reply_seq"] == 3
        assert sent["text"].startswith("echo 1: hello there | first: hello there | workers: 1 | plain")
        assert "system: You are Rookie, the agent \"house\" on this Rook band (identity agent:house)." in sent["text"]
        read = await rig.events()
        assert [(e["seq"], e["type"], e["data"].get("role")) for e in read["events"]] == [
            (1, "input", None), (2, "turn", "prompt"), (3, "turn", "reply")]
        assert read["events"][0]["by"] == "person:Alice" and read["events"][0]["data"] == {"text": "hello there"}
        assert read["events"][1]["data"]["by"] == "person:Alice" and read["events"][1]["data"]["call"]
        assert read["events"][2]["data"]["usage"]["input_tokens"] > 0
        assert read["next"] == 3 and read["stream"] == sent["stream"] and read["closed"] is False
        assert (await rig.events(after=3))["events"] == []
        # The turn's door token was minted as Alice and is revoked now.
        tokens = (await rig.ok("hub", "token.list"))["tokens"]
        turn_tokens = [t for t in tokens if t["minted_by"] is not None]
        assert len(turn_tokens) == 1 and turn_tokens[0]["owner"]["name"] == "Alice"
        assert turn_tokens[0]["revoked_at"] is not None
        # The conversation is a stream the stream tool reads, and no one writes to it there.
        found = (await rig.tool("stream", action="search", query="house with person:Alice"))[1]["streams"]
        assert [s["moniker"] for s in found] == [sent["stream"]] and found[0]["worker"] == "hub"
        failed, why = await rig.tool("stream", action="write", stream=sent["stream"], text="hi")
        assert failed and "agent.chat.send" in why or "plugin agents" in why
        # Another person has a conversation of their own; party reads Alice's.
        bob = rig.hub.auth.create_user("Bob", "another password")
        bobs = rig.hub.auth.mint("agent", user=bob["guid"])
        failed, got = await rig.tool("call", token=bobs, worker="hub", cap="agent.chat.read",
                                     args={"agent": "house"}, why=WHY)
        assert not failed and got == {"stream": None, "events": [], "next": 0, "closed": False}
        failed, got = await rig.tool("call", token=bobs, worker="hub", cap="agent.chat.read",
                                     args={"agent": "house", "party": "person:Alice"}, why=WHY)
        assert not failed and len(got["events"]) == 3
    run(tmp_path, body)


def test_turns_run_one_at_a_time_in_order(tmp_path):
    """Messages sent without waiting are answered in the order sent, each turn after the last."""
    async def body(rig):
        sent = [await rig.say(f"message {n}", wait=False) for n in range(1, 5)]
        assert [s["seq"] for s in sent] == sorted(s["seq"] for s in sent)
        events = await rig.until(lambda got: len(replies(got)) == 4)
        assert [r.split(" | ")[0] for r in replies(events)] == [f"echo {n}: message {n}" for n in range(1, 5)]
        assert [e["data"]["text"] for e in events if e["type"] == "input"] == [f"message {n}" for n in range(1, 5)]
        roles = [e["data"]["role"] for e in events if e["type"] == "turn"]
        assert roles == ["prompt", "reply"] * 4           # each turn ends before the next begins
        prompts = [e for e in events if e["data"].get("role") == "prompt"]
        assert [p["seq"] for p in prompts] == sorted(p["seq"] for p in prompts)
        assert [p["data"]["text"] for p in prompts] == [f"message {n}" for n in range(1, 5)]
    run(tmp_path, body)


def test_the_conversation_resumes_across_hub_and_worker_restarts(tmp_path):
    """The transcript and the engine's session survive a hub restart and a worker restart: the
    third turn still resumes the first turn's session. A message left unanswered when the hub
    stopped is answered with an error that says to send it again."""
    async def body(rig):
        first = await rig.say("remember the word cobalt")
        assert first["text"].startswith("echo 1: remember the word cobalt")
        await rig.say("WAIT here", wait=False)
        await rig.until(lambda got: any(e["data"].get("text") == "WAIT here" and e["type"] == "turn" for e in got))
        await rig.stop_hub()
        await rig.start_hub()
        await rig.linked()
        events = (await rig.events())["events"]
        assert events[-1]["type"] == "status" and "send it again" in events[-1]["data"]["error"]
        second = await rig.say("what was the word?")
        assert second["text"].startswith("echo 3: what was the word? | first: remember the word cobalt")
        await rig.stop_worker()
        rig.start_worker()
        await rig.linked()
        third = await rig.say("and now?")
        assert third["text"].startswith("echo 4: and now? | first: remember the word cobalt")
        assert [e["seq"] for e in (await rig.events())["events"]] == list(range(1, third["reply_seq"] + 1))
    run(tmp_path, body)


def test_a_job_sends_the_house_agent_a_message(tmp_path):
    """Any job can wake it: an event fires a job whose call is agent.chat.send, as the job's owner."""
    async def body(rig):
        await rig.ok("hub", "job.create", title="Tell the house agent", match={"event": "house.ping"},
                     call={"worker": "hub", "cap": "agent.chat.send", "args": {"agent": "house", "text": "from a job"}})
        await rig.ok("hub", "event.emit", name="house.ping", payload={})
        events = await rig.until(lambda got: len(replies(got)) == 1)
        assert events[0]["data"] == {"text": "from a job"} and events[0]["by"] == "person:Alice"
        assert replies(events)[0].startswith("echo 1: from a job")
    run(tmp_path, body)


def test_a_person_stops_a_turn_by_closing_its_call(tmp_path):
    """A turn has no timeout; closing the stream its prompt names stops it, as any call is stopped."""
    async def body(rig):
        await rig.say("WAIT for ever", wait=False)
        events = await rig.until(lambda got: any(e["data"].get("call") for e in got))
        call = next(e["data"]["call"] for e in events if e["data"].get("call"))
        failed, closed = await rig.tool("stream", action="close", stream=call, why="Enough waiting.")
        assert not failed, closed
        events = await rig.until(lambda got: got[-1]["type"] == "status")
        assert "stopped this call" in events[-1]["data"]["error"]
        # The agent answers the next message; the stopped turn never gave a session to resume.
        assert (await rig.say("still there?"))["text"].startswith("echo 1: still there?")
    run(tmp_path, body)


def test_engine_settings_and_errors_say_what_to_do(tmp_path):
    """The agents plugin's settings are listed and set like the hub's; an engine nobody serves fails
    the turn with a reason, kept in the conversation."""
    async def body(rig):
        listed = {s["key"]: s["value"] for s in (await rig.ok("hub", "setting.list", plugin="agents"))["settings"]}
        assert {k: listed[k] for k in ("house_name", "dock", "backend", "worker")} == {
            "house_name": None, "dock": "house", "backend": "echo-engine.turn", "worker": None}
        assert set(listed) == {"house_name", "dock", "backend", "worker", "setup", "sound", "rookie", "rookie_theme"}  # HA3's
        assert listed["setup"]["done"] is True and listed["sound"] is None
        await rig.ok("hub", "setting.set", plugin="agents", key="house_name", value="Jeeves")
        assert "You are Jeeves" in (await rig.say("hi"))["text"]
        await rig.ok("hub", "setting.set", plugin="agents", key="backend", value="nobody.turn")
        failed, why = await rig.tool("call", worker="hub", cap="agent.chat.send",
                                     args={"agent": "house", "text": "anyone?"}, why=WHY)
        assert failed and "No online worker announces nobody.turn" in why
        assert (await rig.events())["events"][-1]["data"]["error"].startswith("No online worker announces")
        await rig.ok("hub", "setting.set", plugin="agents", key="backend", value=None)
        assert (await rig.ok("hub", "setting.list", plugin="agents"))["settings"][2]["value"] == \
            "agents-claude-code.turn"
        failed, why = await rig.tool("call", worker="hub", cap="agent.chat.send", args={"agent": "pm", "text": "x"},
                                     why=WHY)
        assert failed and "No agent pm" in why
    run(tmp_path, body)


def test_a_carried_skill_rides_every_turn_while_its_condition_holds(tmp_path):
    """Plugin.carry: while the condition gives text, the turn carries the skill in full after it
    (0.5's setup brief, which first-run uses); agent.skill reads a declared skill."""
    async def body(rig):
        folder = tmp_path / "carrier"
        (folder / "skills").mkdir(parents=True)
        (folder / "plugin.json").write_text('{"name": "carrier", "version": "1.0.0", "hub": {"module": "hub.py"}}')
        (folder / "skills" / "tour.md").write_text("---\ndescription: Show the hub.\nwhen: always.\n---\n# Tour\nStep 1.")
        (folder / "hub.py").write_text(
            "def setup(plugin):\n"
            "    plugin.setting('on', 'Carry the tour.', default=True)\n"
            "    plugin.skill('tour', 'skills/tour.md')\n"
            "    @plugin.carry('tour')\n"
            "    def touring(call):\n"
            "        return 'Give the tour.' if call.settings['on'] else None\n")
        await rig.ok("hub", "plugin.install", path=str(folder))
        assert (await rig.ok("hub", "agent.skill", name="tour")) == {
            "name": "tour", "description": "Show the hub.", "when": "always.", "text": "# Tour\nStep 1."}
        assert " | carried | " in (await rig.say("one"))["text"]
        await rig.ok("hub", "setting.set", plugin="carrier", key="on", value=False)
        assert " | plain | " in (await rig.say("two"))["text"]
    run(tmp_path, body)


def test_the_claude_code_engine_is_on_every_python_worker(tmp_path):
    """agents-claude-code ships as a default plugin: its turn is announced by every Python worker,
    and the house agent uses it unless the backend setting says otherwise."""
    async def body(rig):
        caps = (await rig.tool("caps", worker=rig.moniker, match="agents-claude-code."))[1]["caps"][0]["caps"]
        assert [c["name"] for c in caps] == ["agents-claude-code.turn"]
        assert set(caps[0]["schema"]["properties"]) == {"prompt", "session", "cwd", "model", "effort", "system", "door"}
    run(tmp_path, body)


def test_the_claude_code_turn_runs_claude_with_0_5s_arguments(tmp_path, monkeypatch):
    """The engine's command line, from a stand-in `claude` on the worker: 0.5's flags, the prompt on
    stdin, --resume with the session, and the door as a Rook MCP server with the turn's token."""
    async def body(rig):
        fake = tmp_path / "claude"
        log = tmp_path / "claude.log"
        fake.write_text("#!/bin/sh\n"
                        f"printf '%s\\n' \"$@\" > {log}\n"
                        f"cat >> {log}\n"
                        "for a in \"$@\"; do case $prev in --mcp-config) cat \"$a\" >> " + str(log) + ";; esac; prev=$a; done\n"
                        "echo '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
                        "\"result\":\"hi from claude\",\"session_id\":\"s-123\",\"usage\":{\"input_tokens\":5}}'\n")
        fake.chmod(0o755)
        # The stand-in is also first on PATH, so no turn here can ever reach a real Claude Code.
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
        version = (await rig.ok("hub", "config.set", worker=rig.moniker,
                                set={"agents-claude-code": {"claude": str(fake)}}))["config"]["version"]
        while rig.hub.get_worker(rig.moniker)["config_version"] != version:
            await asyncio.sleep(0.02)
        await rig.ok("hub", "setting.set", plugin="agents", key="backend", value=None)
        sent = await rig.say("ping")
        assert sent["text"] == "hi from claude"
        args = log.read_text()
        assert args.splitlines()[:4] == ["-p", "--output-format", "json", "--model"]
        assert "--permission-mode\nauto\n" in args and "--strict-mcp-config" in args
        assert "Message from person:Alice:\n\nping" in args and "Bearer ey" in args
        assert f'"url": "{rig.url}/mcp"' in args
        await rig.say("again")
        assert "--resume\ns-123\n" in log.read_text()
    run(tmp_path, body)
