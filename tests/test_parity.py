"""The parity checklist (brief 44, docs/parity.md), the rows that can be a test.

Each test is one or more rows of docs/parity.md, done the way an agent does them on 0.5:
through the MCP door, over HTTP, with an agent token. Behind the door is a real served hub
and a real Python worker, linked over the real carrier (Telesthete over loopback WebSocket),
serving the default plugins. Nothing is called on the hub's Python API that an agent could
not reach, except where a row says so (minting the agent token, which a person does on the
Auth page, and moving the hub's clock for the offline row).

No test here sets a deadline of its own: a row that never answers hangs, which is where
Bake wants a forever wait caught (RULES.md, 17 November)."""

import asyncio
import json
import time

import aiohttp

import rook_hub
from rook_hub import Hub
from rook_worker import Worker

WHY = "Parity checklist row."


class Rig:
    """A served hub, an agent token, and one real worker approved through the door."""

    def __init__(self, tmp_path):
        self.tmp_path, self.offset = tmp_path, 0.0

    async def __aenter__(self):
        self.hub = Hub(self.tmp_path / "hub", clock=lambda: time.time() + self.offset, vault_key=bytes(32))
        host, port = await self.hub.serve("127.0.0.1", 0)
        self.url = f"http://{host}:{port}"
        self.user = self.hub.auth.create_user("Alice", "a password", first=True)
        self.token = self.hub.auth.mint("agent", user=self.user["guid"])    # what the Auth page mints (T3, T5)
        self.http = aiohttp.ClientSession()
        self.worker = Worker(self.tmp_path / "box", self.url, name="box", macs=[], knock_every=0.02)
        self.task = asyncio.create_task(self.worker.run())
        while not (await self.tool("workers"))[1]["knocking"]:
            await asyncio.sleep(0.02)
        key = (await self.tool("workers"))[1]["knocking"][0]["key"]
        self.moniker = (await self.ok("hub", "worker.approve", key=key))["approved"]["moniker"]
        while "shell.open" not in await self.announced():
            await asyncio.sleep(0.02)
        return self

    async def announced(self):
        listed = (await self.tool("workers"))[1]["workers"]
        return listed[0]["caps"] if listed else []

    async def rpc(self, method, params=None, token=None):
        """One JSON-RPC message to /mcp, as an MCP client sends it. Returns (HTTP status, body)."""
        token = self.token if token is None else token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        async with self.http.post(self.url + "/mcp", json=body, headers=headers) as reply:
            return reply.status, await reply.json()

    async def tool(self, name, token=None, **arguments):
        """Call one MCP tool. Returns (is_error, the parsed reply or the error text)."""
        _, body = await self.rpc("tools/call", {"name": name, "arguments": arguments}, token)
        result = body["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text))

    async def ok(self, worker, cap, **args):
        failed, reply = await self.tool("call", worker=worker, cap=cap, args=args, why=WHY)
        assert not failed, reply
        return reply

    async def stream(self, **arguments):
        failed, reply = await self.tool("stream", **arguments)
        assert not failed, reply
        return reply

    async def read_until(self, stream, text, start=0):
        """What the stream says from `start` until `text` is in it (or the stream ends): (said, next)."""
        said, position = "", start
        while text not in said:
            read = await self.stream(action="read", stream=stream, **{"from": position}, wait=1)
            said += "".join(e.get("out", "") for e in read["entries"])
            position = read["next"]
            if not read["open"] and not read["entries"]:
                break
        return said, position

    async def __aexit__(self, *exc):
        self.worker.stop()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.http.close()
        await self.hub.stop_serving()
        self.hub.close()


def run(tmp_path, body):
    async def main():
        async with Rig(tmp_path) as rig:
            await body(rig)
    asyncio.run(main())


# ------------------------------------------------------------------ the door and getting in

def test_door_and_token(tmp_path):
    """Rows 1, 2: the door answers MCP with the five tools; no token, a wrong one or a revoked one is
    refused with what to do next."""
    async def body(rig):
        status, init = await rig.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                    "clientInfo": {"name": "parity", "version": "0"}})
        assert status == 200 and init["result"]["instructions"]
        _, listed = await rig.rpc("tools/list")
        assert [t["name"] for t in listed["result"]["tools"]] == ["workers", "caps", "call", "journal", "stream"]

        for token in ("", "not-a-token"):
            status, refused = await rig.rpc("tools/list", token=token)
            assert status == 401 and refused["error"]["data"]["next"]
        other = rig.hub.auth.mint("agent", user=rig.user["guid"])
        assert not (await rig.tool("workers", token=other))[0]
        rig.hub.auth.revoke_token(rig.hub.auth.verify(other)["moniker"], user=rig.user["guid"])   # as on the Auth page
        status, refused = await rig.rpc("tools/list", token=other)
        assert status == 401 and refused["error"]["data"]["code"] == "token_revoked"
    run(tmp_path, body)


# ------------------------------------------------------------------ workers, caps, heartbeat

def test_workers_caps_heartbeat_and_offline(tmp_path):
    """Rows 3, 4, 5, 6: workers lists the machine online with facts, health and last heartbeat; caps
    lists schemas, narrowed by worker and match; a stopped worker goes offline after three missed
    heartbeats (H3), keeps its record and caps, and a call to it says what to try next."""
    async def body(rig):
        _, listed = await rig.tool("workers")
        box = listed["workers"][0]
        assert box["online"] and box["last_heartbeat"] and box["facts"]["os"] and box["health"]
        assert {"shell.exec", "shell.open", "shell.which", "net.http", "net.tcp", "net.udp"} <= set(box["caps"])

        _, caps = await rig.tool("caps", worker=rig.moniker, match="shell.")
        names = {c["name"]: c for c in caps["caps"][0]["caps"]}
        assert set(names) == {"shell.exec", "shell.open", "shell.which"}
        assert names["shell.open"]["returns"] == "stream" and names["shell.exec"]["schema"]["properties"]
        _, hub_caps = await rig.tool("caps", worker="hub")
        assert "worker.approve" in {c["name"] for c in hub_caps["caps"][0]["caps"]}

        rig.worker.stop()
        await rig.task
        rig.offset += 3 * 15 + 1                           # three missed heartbeats at the default interval
        _, listed = await rig.tool("workers")
        box = listed["workers"][0]
        assert not box["online"] and "shell.exec" in box["caps"]
        failed, text = await rig.tool("call", worker=rig.moniker, cap="shell.exec", args={"cmd": "true"}, why=WHY)
        assert failed and text.startswith("worker_offline") and "Next:" in text
    run(tmp_path, body)


# ------------------------------------------------------------------ calls and commands

def test_call_a_command(tmp_path):
    """Rows 7, 8, 9, 10: a call answers with the cap's result; shell.exec gives stdout, stderr and the
    exit code, with argv, a shell, cwd, env and stdin; shell.which finds a program; an unknown cap,
    an unknown worker and a missing why are refused with a next step."""
    async def body(rig):
        reply = await rig.ok(rig.moniker, "shell.exec", cmd="echo out; echo err >&2; exit 3")
        assert reply == {"ok": False, "code": 3, "stdout": "out\n", "stderr": "err\n"}
        reply = await rig.ok(rig.moniker, "shell.exec", argv=["printf", "%s|", "a b", "c"])
        assert reply["stdout"] == "a b|c|"
        reply = await rig.ok(rig.moniker, "shell.exec", cmd="pwd; echo $FOO; cat", cwd=str(rig.tmp_path),
                             env={"FOO": "bar"}, stdin="fed")
        assert reply["stdout"] == f"{rig.tmp_path}\nbar\nfed"
        assert (await rig.ok(rig.moniker, "shell.which", name="sh")).endswith("/sh")

        for worker, cap, args, why, code in ((rig.moniker, "no.such", {}, WHY, "unknown_cap"),
                                             ("no-such-worker", "shell.exec", {}, WHY, "unknown_worker"),
                                             (rig.moniker, "shell.exec", {"cmd": "true"}, "", "bad_message")):
            failed, text = await rig.tool("call", worker=worker, cap=cap, args=args, why=why)
            assert failed and text.startswith(code) and "Next:" in text
    run(tmp_path, body)


def test_large_output(tmp_path):
    """Row 11: 3,000 lines in one reply, and 10 MB of output in one reply."""
    async def body(rig):
        reply = await rig.ok(rig.moniker, "shell.exec", cmd="seq 1 3000")
        assert reply["stdout"].splitlines() == [str(n) for n in range(1, 3001)]
        reply = await rig.ok(rig.moniker, "shell.exec", cmd="head -c 10000000 /dev/zero | tr '\\0' a")
        assert len(reply["stdout"]) == 10_000_000
    run(tmp_path, body)


def test_a_long_call_is_followed_as_a_stream(tmp_path, monkeypatch):
    """Row 12: a call that outlasts the door's wait answers {running, stream}; reading that stream
    gives the reply. The wait is shortened here so the row takes seconds, not 45."""
    monkeypatch.setattr(rook_hub, "CALL_WAIT", 0.5)

    async def body(rig):
        running = await rig.ok(rig.moniker, "shell.exec", cmd="sleep 2; echo slept")
        assert running["running"] is True and running["stream"]
        said, _ = await rig.read_until(running["stream"], "\"type\":\"result\"")
        assert json.loads(said)["result"]["stdout"] == "slept\n"
    run(tmp_path, body)


# ------------------------------------------------------------------ terminals and streams

def test_terminal(tmp_path):
    """Rows 13, 14, 15: open a terminal, type into it, read it, resize it, close it; the stream is
    listed with its title, and the close is journaled with its why."""
    async def body(rig):
        opened = await rig.ok(rig.moniker, "shell.open", argv=["sh"], cols=100, rows=30, title="parity terminal")
        stream = opened["stream"]
        listed = (await rig.stream(action="list", open=True))["streams"]
        assert [(s["moniker"], s["title"], s["worker"]) for s in listed] == [(stream, "parity terminal", rig.moniker)]

        await rig.stream(action="write", stream=stream, text="stty size; echo mark-$((6*7))\n")
        said, position = await rig.read_until(stream, "mark-42")
        assert "30 100" in said
        await rig.stream(action="resize", stream=stream, cols=77, rows=11)
        await rig.stream(action="write", stream=stream, text="stty size\n")
        said, _ = await rig.read_until(stream, "11 77", position)

        await rig.stream(action="close", stream=stream, why="Parity: the terminal's work is done.")
        assert not (await rig.stream(action="read", stream=stream))["open"]
        _, journal = await rig.tool("journal", kind="stream")
        assert journal["entries"][0]["why"] == "Parity: the terminal's work is done."
    run(tmp_path, body)


def test_a_stream_outlives_its_opener_and_takes_many_clients(tmp_path):
    """Row 16: a stream one agent opened is read, written and closed by a second agent's token (S2)."""
    async def body(rig):
        stream = (await rig.ok(rig.moniker, "shell.open", argv=["cat"], pty=False))["stream"]
        second = rig.hub.auth.mint("agent", user=rig.user["guid"])
        failed, _ = await rig.tool("stream", token=second, action="write", stream=stream, text="from-second\n")
        assert not failed
        said, _ = await rig.read_until(stream, "from-second")
        failed, _ = await rig.tool("stream", token=second, action="close", stream=stream, why="Parity: done.")
        assert not failed
    run(tmp_path, body)


def test_a_3000_line_stream(tmp_path):
    """Row 17: a process stream carrying 3,000 lines is read whole, in order, page by page."""
    async def body(rig):
        stream = (await rig.ok(rig.moniker, "shell.open", argv=["seq", "1", "3000"], pty=False))["stream"]
        said, position = "", 0
        while True:
            read = await rig.stream(action="read", stream=stream, **{"from": position}, wait=1)
            said += "".join(e.get("out", "") for e in read["entries"])
            position = read["next"]
            if not read["open"] and not read["entries"]:
                break
        assert said.splitlines() == [str(n) for n in range(1, 3001)]
    run(tmp_path, body)


# ------------------------------------------------------------------ journal

def test_journal(tmp_path):
    """Rows 18, 19: every call is journaled with token, user, worker, cap, why and result; the journal
    narrows by cap, kind and text, and a call is found by its id."""
    async def body(rig):
        await rig.ok(rig.moniker, "shell.exec", cmd="echo journal-row")
        _, journal = await rig.tool("journal", cap="shell.exec")
        entry = journal["entries"][0]
        assert entry["kind"] == "call" and entry["why"] == WHY and entry["worker"] == rig.moniker
        assert entry["user"] == rig.user["moniker"] and entry["token"]
        assert entry["result"]["result"]["stdout"] == "journal-row\n"
        _, found = await rig.tool("journal", text=entry["result"]["id"])
        assert [e["id"] for e in found["entries"]] == [entry["id"]]
    run(tmp_path, body)


# ------------------------------------------------------------------ secrets

def test_secret_by_reference(tmp_path):
    """Rows 20, 21: an agent makes a secret and uses it by reference; the worker gets the value, the
    journal never holds it, and the list never shows it."""
    async def body(rig):
        value = "s3cr3t-Parity-Value-91"
        await rig.ok("hub", "secret.create", scope="hub", name="parity_pw", value=value)
        reply = await rig.ok(rig.moniker, "shell.exec", cmd="echo got:$X", env={"X": "secret:parity_pw"})
        assert reply["stdout"] == f"got:{value}\n"
        _, journal = await rig.tool("journal", limit=1000)
        assert value not in json.dumps(journal)
        assert value not in json.dumps(await rig.ok("hub", "secret.list"))
    run(tmp_path, body)


# ------------------------------------------------------------------ jobs

def test_jobs(tmp_path):
    """Rows 22, 23, 24: an agent makes a job, an event runs it (with a secret by reference), a
    second job chained on the first's success runs under the same run ID; pause and delete."""
    async def body(rig):
        await rig.ok("hub", "secret.create", scope="hub", name="parity_pw", value="job-secret")
        first = (await rig.ok("hub", "job.create", title="parity echo", match={"event": "parity.ping"},
                              call={"worker": rig.moniker, "cap": "shell.exec",
                                    "args": {"cmd": "echo ran:$X", "env": {"X": "secret:parity_pw"}}}))["job"]
        second = (await rig.ok("hub", "job.create", title="parity chain",
                               match={"event": "job.success", "payload": {"job_moniker": first["moniker"]}},
                               call={"worker": "hub", "cap": "secret.list", "args": {}}))["job"]
        await rig.ok("hub", "event.emit", name="parity.ping")
        while len((await rig.tool("journal", kind="job"))[1]["entries"]) < 2:
            await asyncio.sleep(0.05)
        runs = (await rig.tool("journal", kind="job"))[1]["entries"]
        assert {r["job"] for r in runs} == {first["moniker"], second["moniker"]}
        assert len({r["run"] for r in runs}) == 1
        ran = next(r for r in runs if r["job"] == first["moniker"])
        assert ran["result"]["result"]["stdout"].startswith("ran:")
        assert "job-secret" not in json.dumps(runs)

        assert (await rig.ok("hub", "job.pause", job=first["moniker"]))["paused"] is True
        await rig.ok("hub", "job.delete", job=second["moniker"])
        listed = (await rig.ok("hub", "job.list"))["jobs"]
        assert [(j["moniker"], j["paused"]) for j in listed] == [(first["moniker"], True)]
    run(tmp_path, body)


# ------------------------------------------------------------------ plugins

def test_install_a_plugin_and_call_its_caps(tmp_path):
    """Row 25: an agent installs a plugin from a folder on the hub's disk; its hub cap and its worker
    cap are callable without a restart."""
    folder = tmp_path / "parityplug"
    folder.mkdir()
    (folder / "plugin.json").write_text(json.dumps({"name": "parity", "version": "0.0.1", "hub": {"module": "hub.py"},
                                                    "worker": {"python": {"module": "worker.py"}}}))
    (folder / "hub.py").write_text(
        "def setup(plugin):\n"
        "    @plugin.cap('parity.twice', 'Twice a number.', {'type': 'object', 'properties': {'n': {'type': 'integer'}}})\n"
        "    def twice(call, args):\n"
        "        return args['n'] * 2\n")
    (folder / "worker.py").write_text(
        "from rook_worker import Cap\n"
        "async def hello(args):\n"
        "    return {'hello': args.get('name', 'world')}\n"
        "CAPS = {'parity.hello': Cap('Say hello.', {'type': 'object', 'properties': {'name': {'type': 'string'}}}, hello)}\n")

    async def body(rig):
        await rig.ok("hub", "plugin.install", path=str(folder))
        assert await rig.ok("hub", "parity.twice", n=21) == 42
        while "parity.hello" not in await rig.announced():
            await asyncio.sleep(0.02)
        assert await rig.ok(rig.moniker, "parity.hello", name="rook") == {"hello": "rook"}
    run(tmp_path, body)
