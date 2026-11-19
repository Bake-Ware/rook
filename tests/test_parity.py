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
        while not {"shell.open", "shell.env", "file.read", "worker.ping"} <= set(await self.announced()):
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

    async def ok(self, worker, cap, /, **args):
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
        assert set(names) == {"shell.env", "shell.exec", "shell.open", "shell.which"}
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
    """Row 11: 3,000 lines in one reply, and 10 MB of output whole: over about a megabyte it comes as a
    stream of pieces, which the door says how to join (brief 54)."""
    async def body(rig):
        reply = await rig.ok(rig.moniker, "shell.exec", cmd="seq 1 3000")
        assert reply["stdout"].splitlines() == [str(n) for n in range(1, 3001)]
        running = await rig.ok(rig.moniker, "shell.exec", cmd="head -c 10000000 /dev/zero | tr '\\0' a")
        assert running["running"] is True
        texts, position, open_ = [], 0, True
        while open_:
            read = await rig.stream(action="read", stream=running["stream"], **{"from": position}, wait=5)
            texts, position, open_ = texts + [e["out"] for e in read["entries"]], read["next"], read["open"]
        end = json.loads(texts[-1])
        assert json.loads("".join(texts[:end["parts"]]))["stdout"] == "a" * 10_000_000
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


# ------------------------------------------------------------------ files, environment, liveness (brief 41)

def test_files_environment_and_ping(tmp_path):
    """Rows 36, 37, 38: write, read (text and bytes), list, search and stat a file; read the worker's
    environment; ping the worker."""
    async def body(rig):
        folder = rig.tmp_path / "files"
        note = folder / "a" / "b.txt"
        written = await rig.ok(rig.moniker, "file.write", path=str(note), content="line one\nneedle two\n",
                               create_parents=True)
        assert written["written"] == 20
        assert (await rig.ok(rig.moniker, "file.read", path=str(note)))["content"] == "line one\nneedle two\n"
        await rig.ok(rig.moniker, "file.write", path=str(folder / "bin"), content="AAEC/w==", encoding="base64")
        assert (await rig.ok(rig.moniker, "file.read", path=str(folder / "bin"), encoding="base64"))["content"] == "AAEC/w=="
        listed = await rig.ok(rig.moniker, "file.list", path=str(folder), recursive=True)
        assert {e["path"] for e in listed["entries"]} == {"a", "a/b.txt", "bin"}
        found = await rig.ok(rig.moniker, "file.search", path=str(folder), pattern="needle")
        assert [(m["line"], m["text"]) for m in found["matches"]] == [(2, "needle two")]
        assert (await rig.ok(rig.moniker, "file.stat", path=str(note)))["size"] == 20

        assert await rig.ok(rig.moniker, "shell.env", name="NO_SUCH_VARIABLE_X", default="dflt") == "dflt"
        assert (await rig.ok(rig.moniker, "worker.ping"))["pong"] is True
    run(tmp_path, body)


# ------------------------------------------------------------------ owner and config (briefs 40, 42)

def test_owner_and_config(tmp_path):
    """Rows 50, 51: reassign a worker's owner through the door; set a worker's config with a secret by
    reference, and see the worker apply it, the reference kept everywhere but on the wire."""
    async def body(rig):
        reply = await rig.ok("hub", "worker.reassign", worker=rig.moniker, owner=rig.user["moniker"])
        assert reply["owner"]["moniker"] == rig.user["moniker"]

        await rig.ok("hub", "secret.create", scope="hub", name="cfgpw", value="cfg-secret-77")
        set_ = await rig.ok("hub", "config.set", worker=rig.moniker, set={"greeting": "hi", "pw": "secret:cfgpw"})
        assert set_["config"]["values"] == {"greeting": "hi", "pw": "secret:cfgpw"}
        while (got := await rig.ok("hub", "config.get", worker=rig.moniker))["config"]["applied"] != set_["config"]["version"]:
            await asyncio.sleep(0.05)
        _, journal = await rig.tool("journal", limit=1000)
        assert "cfg-secret-77" not in json.dumps(journal) and "cfg-secret-77" not in json.dumps(got)
    run(tmp_path, body)


# ------------------------------------------------------------------ brief 59: stop, run, change, exit, signal

async def _pid(path):
    """The process ID a command wrote to `path`, once it has."""
    while not path.exists() or not path.read_text().strip():
        await asyncio.sleep(0.02)
    return int(path.read_text())


async def _gone(pid):
    """Wait until process `pid` no longer exists (a zombie counts as gone: it runs nothing)."""
    while True:
        try:
            with open(f"/proc/{pid}/stat") as stat:
                if stat.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return
        except FileNotFoundError:
            return
        await asyncio.sleep(0.02)


async def _journal(rig, **filters):
    """The journal's newest entry that matches `filters`, once one has ended (no longer running)."""
    while True:
        entries = (await rig.tool("journal", **filters))[1]["entries"]
        if entries and entries[0]["result"].get("type") != "running":
            return entries[0]
        await asyncio.sleep(0.05)


def test_stop_a_running_call(tmp_path, monkeypatch):
    """Row 28 and Found waits 1 (brief 59): a command that never ends, called through the door, is
    stopped by closing its call's stream. The command and what it started are gone; the stream's last
    entry is the reply, call_stopped, naming who stopped it and why; the journal keeps the same."""
    monkeypatch.setattr(rook_hub, "CALL_WAIT", 0.5)

    async def body(rig):
        parent, child = tmp_path / "parent.pid", tmp_path / "child.pid"
        running = await rig.ok(rig.moniker, "shell.exec",
                               cmd=f"sleep infinity & echo $! > {child}; echo $$ > {parent}; wait")
        assert running["running"] is True and "close that stream" in running["next"]
        pids = [await _pid(parent), await _pid(child)]

        failed, refused = await rig.tool("stream", action="write", stream=running["stream"], text="x")
        assert failed and refused.startswith("call_stream:") and "To stop the call, close the stream." in refused
        closed = await rig.stream(action="close", stream=running["stream"], why="Parity: it would run for ever.")
        assert closed["stopped"] == running["id"] and closed["reply"]["code"] == "call_stopped"
        for pid in pids:
            await _gone(pid)

        read = await rig.stream(action="read", stream=running["stream"])
        assert read["open"] is False
        reply = json.loads(read["entries"][-1]["out"])
        assert reply["type"] == "error" and reply["code"] == "call_stopped" and reply["next"]
        assert reply["message"].startswith("Alice (") and "through agent token" in reply["message"]
        assert "closing stream" in reply["message"] and "Parity: it would run for ever." in reply["message"]
        entry = (await rig.tool("journal", text=running["id"], kind="call"))[1]["entries"][0]
        assert entry["result"] == reply and entry["cap"] == "shell.exec"

        failed, again = await rig.tool("stream", action="close", stream=running["stream"], why="Again.")
        assert failed and again.startswith("stream_closed:")
        assert (await rig.ok(rig.moniker, "shell.exec", cmd="echo still-serving"))["stdout"] == "still-serving\n"
    run(tmp_path, body)


def test_run_a_job_now(tmp_path):
    """Row 27 (brief 59): job.run runs a job now, whatever its trigger, even a schedule job that is
    paused, as its owner, under the run ID it answers with; it ends with job.success like any run."""
    async def body(rig):
        job = (await rig.ok("hub", "job.create", title="parity nightly", match={"event": "schedule",
                                                                                  "payload": {"schedule": "at 03:00"}},
                            call={"worker": rig.moniker, "cap": "shell.exec", "args": {"cmd": "echo ran-now"}}))["job"]
        await rig.ok("hub", "job.pause", job=job["moniker"])
        started = await rig.ok("hub", "job.run", job=job["moniker"])
        assert started["job"] == job["moniker"] and started["run"]
        ran = await _journal(rig, kind="job", run=started["run"])
        assert ran["job"] == job["moniker"] and ran["result"]["result"]["stdout"] == "ran-now\n"
        assert ran["user"] == rig.user["moniker"] and "run now by Alice" in ran["why"]
        while not (await rig.tool("journal", kind="event", run=started["run"]))[1]["entries"]:
            await asyncio.sleep(0.05)
        event = (await rig.tool("journal", kind="event", run=started["run"]))[1]["entries"][0]
        assert event["result"]["event"] == "job.success"

        failed, refused = await rig.tool("call", worker="hub", cap="job.run", args={"job": "no-such-job"}, why=WHY)
        assert failed and refused.startswith("unknown_job:")
    run(tmp_path, body)


def test_stop_a_running_job(tmp_path):
    """Row 28 (brief 59): job.cancel stops a run still running: its command is gone, the run's
    journal entry is call_stopped naming who stopped it, and the run ends as job.fail (J3)."""
    async def body(rig):
        pidfile = tmp_path / "job.pid"
        job = (await rig.ok("hub", "job.create", title="parity forever", match={"event": "parity.forever"},
                            call={"worker": rig.moniker, "cap": "shell.exec",
                                  "args": {"cmd": f"echo $$ > {pidfile}; exec sleep infinity"}}))["job"]
        run_id = (await rig.ok("hub", "job.run", job=job["moniker"]))["run"]
        pid = await _pid(pidfile)

        stopped = await rig.ok("hub", "job.cancel", run=run_id)
        assert stopped["run"] == run_id and len(stopped["stopped"]) == 1
        await _gone(pid)
        ran = await _journal(rig, kind="job", run=run_id)
        assert ran["result"]["code"] == "call_stopped" and "with job.cancel" in ran["result"]["message"]
        while not (await rig.tool("journal", kind="event", run=run_id))[1]["entries"]:
            await asyncio.sleep(0.05)
        assert (await rig.tool("journal", kind="event", run=run_id))[1]["entries"][0]["result"]["event"] == "job.fail"

        failed, refused = await rig.tool("call", worker="hub", cap="job.cancel", args={"run": run_id}, why=WHY)
        assert failed and refused.startswith("run_not_running:")
    run(tmp_path, body)


def test_change_a_job(tmp_path):
    """Row 29 (brief 59): job.update changes a job in place: the same moniker, GUID, owner and
    created time; its earlier run stays its own in the journal; the next run makes the new call.
    A change that is not a valid job is refused and leaves the job as it was."""
    async def body(rig):
        job = (await rig.ok("hub", "job.create", title="parity old", match={"event": "parity.change"},
                            call={"worker": rig.moniker, "cap": "shell.exec", "args": {"cmd": "echo old"}}))["job"]
        await rig.ok("hub", "event.emit", name="parity.change")
        old = await _journal(rig, kind="job", job=job["moniker"])

        changed = (await rig.ok("hub", "job.update", job=job["moniker"], title="parity new",
                                call={"worker": rig.moniker, "cap": "shell.exec", "args": {"cmd": "echo new"}}))["job"]
        assert changed == dict(job, title="parity new",
                               call=dict(job["call"], args={"cmd": "echo new"}))     # identity, owner, match kept
        failed, refused = await rig.tool("call", worker="hub", cap="job.update", why=WHY,
                                         args={"job": job["moniker"], "match": {"event": "schedule"}})
        assert failed and refused.startswith("bad_message:")
        assert (await rig.ok("hub", "job.list"))["jobs"] == [changed]

        await rig.ok("hub", "event.emit", name="parity.change")
        while len(runs := (await rig.tool("journal", kind="job", job=job["moniker"]))[1]["entries"]) < 2 \
                or runs[0]["result"].get("type") == "running":
            await asyncio.sleep(0.05)
        assert [r["result"]["result"]["stdout"] for r in runs] == ["new\n", "old\n"]
        assert runs[1]["id"] == old["id"]
    run(tmp_path, body)


def test_a_stream_that_ends_says_its_exit_code(tmp_path):
    """Row 15 (brief 59): a terminal or a process on pipes that exits closes its stream with its
    exit code, in the read, the stream list and the journal's close entry."""
    async def body(rig):
        for pty, code in ((True, 5), (False, 3)):
            opened = await rig.ok(rig.moniker, "shell.open", cmd=f"echo bye; exit {code}", pty=pty)
            await rig.read_until(opened["stream"], "bye")
            while (read := await rig.stream(action="read", stream=opened["stream"]))["open"]:
                await asyncio.sleep(0.02)
            assert read["exit_code"] == code
            listed = {s["moniker"]: s for s in (await rig.stream(action="list"))["streams"]}
            assert listed[opened["stream"]]["exit_code"] == code and listed[opened["stream"]]["closed_by"] == "worker"
            closed = (await rig.tool("journal", kind="stream"))[1]["entries"][0]["result"]
            assert closed["closed"] == listed[opened["stream"]]["guid"] and closed["exit_code"] == code
    run(tmp_path, body)


def test_signals_to_a_stream(tmp_path):
    """Row 14 (brief 59): 0.5's signals reach the process behind a stream: INT to a command on pipes
    runs its trap, KILL ends one that ignores the rest, INT on a terminal reaches the program in the
    foreground. Each stream then closes with the exit code that says so. A signal 0.5 did not take
    is refused with the ones it can be."""
    async def body(rig):
        trapped = (await rig.ok(rig.moniker, "shell.open", pty=False,
                                cmd="trap 'echo got-INT; exit 7' INT; echo ready; while :; do sleep 0.05; done"))["stream"]
        _, position = await rig.read_until(trapped, "ready")
        assert (await rig.stream(action="signal", stream=trapped, signal="INT")) == {"signalled": trapped, "signal": "INT"}
        await rig.read_until(trapped, "got-INT", position)
        while (read := await rig.stream(action="read", stream=trapped))["open"]:
            await asyncio.sleep(0.02)
        assert read["exit_code"] == 7

        stubborn = (await rig.ok(rig.moniker, "shell.open", pty=False,
                                 cmd="trap '' INT TERM HUP QUIT; echo ready; exec sleep infinity"))["stream"]
        await rig.read_until(stubborn, "ready")
        await rig.stream(action="signal", stream=stubborn, signal="KILL")
        while (read := await rig.stream(action="read", stream=stubborn))["open"]:
            await asyncio.sleep(0.02)
        assert read["exit_code"] == -9

        terminal = (await rig.ok(rig.moniker, "shell.open", argv=["cat"]))["stream"]
        await rig.stream(action="write", stream=terminal, text="typed\n")
        await rig.read_until(terminal, "typed")
        await rig.stream(action="signal", stream=terminal, signal="INT")
        while (read := await rig.stream(action="read", stream=terminal))["open"]:
            await asyncio.sleep(0.02)
        assert read["exit_code"] == -2

        failed, refused = await rig.tool("stream", action="signal", stream=terminal, signal="INT")
        assert failed and refused.startswith("stream_closed:")
        other = (await rig.ok(rig.moniker, "shell.open", argv=["cat"]))["stream"]
        failed, refused = await rig.tool("stream", action="signal", stream=other, signal="USR1")
        assert failed and refused.startswith("bad_message:") and "INT" in refused
        await rig.stream(action="close", stream=other, why="Parity: done.")
    run(tmp_path, body)
