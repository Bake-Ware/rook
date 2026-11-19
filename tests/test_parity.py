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
        assert [(j["moniker"], j["paused"]) for j in listed if j["owner"] != "hub-system-owner"] == \
            [(first["moniker"], True)]                              # beside the system owner's job (brief 56)
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


# ------------------------------------------------------------------ brief 61: the gaps the checklist found

def test_replace_and_delete_a_secret(tmp_path):
    """Row 56 (0.5's secret.set and secret.delete): a secret given a new value keeps its moniker and its
    references, and the worker gets the new value; neither value is journaled. A deleted secret is gone from
    the list, and a call that refers to it is refused with what to do."""
    async def body(rig):
        made = (await rig.ok("hub", "secret.create", scope="hub", name="rotating", value="first-Value-0001"))["created"]
        failed, refused = await rig.tool("call", worker="hub", cap="secret.create", why=WHY,
                                         args={"scope": "hub", "name": "rotating", "value": "x"})
        assert failed and "secret.set" in refused
        replaced = (await rig.ok("hub", "secret.set", scope="hub", name="rotating", value="second-Value-0002"))["replaced"]
        assert (replaced["moniker"], replaced["reference"]) == (made["moniker"], "secret:rotating")
        reply = await rig.ok(rig.moniker, "shell.exec", cmd="echo got:$X", env={"X": "secret:rotating"})
        assert reply["stdout"] == "got:second-Value-0002\n"
        new = (await rig.ok("hub", "secret.set", scope="hub", name="fresh", value="made-by-set-0003"))
        assert new["created"]["name"] == "fresh"                 # set makes one that does not exist yet
        _, journal = await rig.tool("journal", limit=1000)
        for value in ("first-Value-0001", "second-Value-0002", "made-by-set-0003"):
            assert value not in json.dumps(journal)

        deleted = (await rig.ok("hub", "secret.delete", scope="hub", name="rotating"))["deleted"]
        assert deleted["moniker"] == made["moniker"]
        assert [s["name"] for s in (await rig.ok("hub", "secret.list"))["secrets"]] == ["fresh"]
        failed, refused = await rig.tool("call", worker=rig.moniker, cap="shell.exec", why=WHY,
                                         args={"cmd": "echo $X", "env": {"X": "secret:rotating"}})
        assert failed and refused.startswith("unknown_secret") and "secret.create" in refused
        failed, refused = await rig.tool("call", worker="hub", cap="secret.delete", why=WHY,
                                         args={"scope": "hub", "name": "rotating"})
        assert failed and refused.startswith("unknown_secret") and "secret.list" in refused
        # The scrubber forgets a value no secret holds any more, and learns the new one (L4).
        held = {v for by_offset in rig.hub.vault.index.values() for by_len in by_offset.values()
                for values in by_len.values() for v in values}
        assert held == {"made-by-set-0003"}
    run(tmp_path, body)


def test_stream_search(tmp_path):
    """Row 45 (0.5's stream.search): the stream tool's action search finds streams by any part of their
    title, description, cap, worker or opener, in any case; worker and open narrow it."""
    async def body(rig):
        deploy = (await rig.ok(rig.moniker, "shell.open", argv=["sh"], title="Deploy the website"))["stream"]
        logs = (await rig.ok(rig.moniker, "shell.open", argv=["sh"], title="Tail the logs"))["stream"]
        found = (await rig.stream(action="search", query="WEBSITE"))["streams"]
        assert [s["moniker"] for s in found] == [deploy]
        assert {s["moniker"] for s in (await rig.stream(action="search", query="shell.open"))["streams"]} == {deploy, logs}
        assert {s["moniker"] for s in (await rig.stream(action="search", query="the", worker=rig.moniker))["streams"]} \
            == {deploy, logs}
        await rig.stream(action="close", stream=logs, why="Parity: done with the logs.")
        assert [s["moniker"] for s in (await rig.stream(action="search", query="the", open=True))["streams"]] == [deploy]
        assert (await rig.stream(action="search", query="nothing like it"))["streams"] == []
        failed, refused = await rig.tool("stream", action="search")
        assert failed and refused.startswith("bad_message")
    run(tmp_path, body)


def test_settings_as_an_agent(tmp_path):
    """Row 48 (0.5's setting.list and setting.set): an agent reads and changes what the Settings page
    holds, the hub's name; the site shows the change."""
    async def body(rig):
        assert (await rig.ok("hub", "setting.list"))["settings"] == [
            {"section": "hub", "key": "name", "value": None, "description": "The hub's name, shown on the site."}]
        assert (await rig.ok("hub", "setting.set", key="name", value="  Home  "))["value"] == "Home"
        assert (await rig.ok("hub", "setting.list"))["settings"][0]["value"] == "Home"
        assert rig.hub.name() == "Home"                             # what the Settings page reads (GET /api/hub)
        failed, refused = await rig.tool("call", worker="hub", cap="setting.set", why=WHY,
                                         args={"key": "colour", "value": "red"})
        assert failed and refused.startswith("unknown_setting") and "name" in refused
    run(tmp_path, body)


def test_backups_as_an_agent(tmp_path):
    """Row 53 (0.5's backup.run, backup.list, backup.verify): an agent backs up the running hub to a file
    on its disk, finds it, and proves it; a changed byte is refused; the command restores it into a hub
    that holds the same secret. A file that exists is never written over."""
    from rook_hub import backup

    async def body(rig):
        await rig.ok("hub", "secret.create", scope="hub", name="kept", value="kept-through-backup")
        folder = tmp_path / "backups"
        made = await rig.ok("hub", "backup.run", to=str(folder / "one.rookbackup"))
        assert made["bytes"] > 0 and made["path"] == str((folder / "one.rookbackup").resolve())
        (folder / "notes.txt").write_text("not a backup")
        listed = (await rig.ok("hub", "backup.list", folder=str(folder)))["backups"]
        assert [b["path"] for b in listed] == [made["path"]] and listed[0]["bytes"] == made["bytes"]
        verified = await rig.ok("hub", "backup.verify", path=made["path"])
        assert verified["verified"] is True and verified["files"] >= 4

        failed, refused = await rig.tool("call", worker="hub", cap="backup.run", why=WHY, args={"to": made["path"]})
        assert failed and refused.startswith("backup_exists")
        changed = bytearray((folder / "one.rookbackup").read_bytes())
        changed[len(changed) // 2] ^= 1
        (folder / "changed.rookbackup").write_bytes(bytes(changed))
        failed, refused = await rig.tool("call", worker="hub", cap="backup.verify", why=WHY,
                                         args={"path": str(folder / "changed.rookbackup")})
        assert failed and refused.startswith("backup_changed")

        key_file = tmp_path / "vault.key"
        key_file.write_text(bytes(32).hex())                      # the key this rig's hub was started with
        backup.restore(made["path"], tmp_path / "restored", key_file)
        restored = Hub(tmp_path / "restored", vault_key=bytes(32))
        try:
            assert restored.vault.substitute({"x": "secret:kept"}, "hub") == {"x": "kept-through-backup"}
        finally:
            restored.close()
    run(tmp_path, body)


def test_heartbeat_interval(tmp_path):
    """Row 59 (H2): an agent sets a worker's heartbeat interval; the linked worker is sent it at once and
    heartbeats on it. Reading the heartbeats table is the one step an agent would read on the Workers page's
    chart instead."""
    async def body(rig):
        def beats():
            with rig.hub._lock:
                return rig.hub._db.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0]
        reply = await rig.ok("hub", "worker.interval", worker=rig.moniker, seconds=1)
        assert reply["sent"] is True and reply["interval"] == 1
        await asyncio.sleep(0.5)
        before = beats()
        await asyncio.sleep(3.2)
        assert beats() - before >= 3                              # every second, not every 15
        assert rig.worker.interval == 1
        await rig.ok("hub", "worker.interval", worker=rig.moniker, seconds=600)
        while rig.worker.interval != 600:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        before = beats()
        await asyncio.sleep(2)
        assert beats() == before
        failed, refused = await rig.tool("call", worker="hub", cap="worker.interval", why=WHY,
                                         args={"worker": rig.moniker, "seconds": 0})
        assert failed and refused.startswith("bad_message")
    run(tmp_path, body)


def test_install_a_plugin_from_a_url(tmp_path):
    """Row 43 (PL4: "a folder or a URL"): an agent installs a plugin from the URL of a tar.gz holding its
    folder, and of a zip with plugin.json at its top; its cap is callable at once. A URL that answers 404,
    and one that is not an archive, are refused with what to do."""
    import io
    import tarfile
    import zipfile
    from aiohttp import web

    def plugin(name):
        files = {"plugin.json": json.dumps({"name": name, "version": "1.0.0", "hub": {"module": "hub.py"}}),
                 "hub.py": "def setup(plugin):\n"
                           f"    @plugin.cap('{name}.hello', 'Say hello.', {{'type': 'object'}})\n"
                           "    def hello(call, args):\n"
                           f"        return '{name} says hello'\n"}
        return files

    def targz(name):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for file, text in plugin(name).items():
                data = text.encode()
                info = tarfile.TarInfo(f"{name}-main/{file}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return out.getvalue()

    def zipped(name):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for file, text in plugin(name).items():
                z.writestr(file, text)
        return out.getvalue()

    served = {"/tarred.tar.gz": targz("tarred"), "/zipped.zip": zipped("zipped"), "/page.html": b"<html></html>"}

    async def serve(request):
        return web.Response(body=served[request.path]) if request.path in served else web.Response(status=404)

    async def body(rig):
        app = web.Application()
        app.router.add_get("/{file}", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        base = "http://127.0.0.1:%d" % runner.addresses[0][1]
        try:
            for name, path in (("tarred", "/tarred.tar.gz"), ("zipped", "/zipped.zip")):
                reply = await rig.ok("hub", "plugin.install", url=base + path)
                assert reply["installed"]["name"] == name and reply["from"] == base + path
                assert await rig.ok("hub", f"{name}.hello") == f"{name} says hello"
            failed, refused = await rig.tool("call", worker="hub", cap="plugin.install", why=WHY,
                                             args={"url": base + "/missing.zip"})
            assert failed and refused.startswith("plugin_download_failed") and "404" in refused
            failed, refused = await rig.tool("call", worker="hub", cap="plugin.install", why=WHY,
                                             args={"url": base + "/page.html"})
            assert failed and refused.startswith("plugin_archive_bad")
        finally:
            await runner.cleanup()
    run(tmp_path, body)


def test_cap_failed_says_what_to_do(tmp_path):
    """Rows 11, 38: a cap that fails says why and what to try, not "check the arguments against the schema";
    a result of 20 MB, refused before brief 54, comes whole as pieces of a stream."""
    async def body(rig):
        failed, refused = await rig.tool("call", worker=rig.moniker, cap="worker.ping", why=WHY, args={"loud": True})
        assert failed and refused.startswith("cap_failed") and "it takes none" in refused
        assert "against the schema" not in refused and "another worker" in refused
        failed, refused = await rig.tool("call", worker=rig.moniker, cap="file.read", why=WHY,
                                         args={"path": str(tmp_path / "no-such-file")})
        assert failed and "file.stat" in refused and "against the schema" not in refused

        running = await rig.ok(rig.moniker, "shell.exec", cmd="head -c 20000000 /dev/zero | tr '\\0' b")
        texts, position, open_ = [], 0, True
        while open_:
            read = await rig.stream(action="read", stream=running["stream"], **{"from": position}, wait=5)
            texts, position, open_ = texts + [e["out"] for e in read["entries"]], read["next"], read["open"]
        end = json.loads(texts[-1])
        assert end["type"] == "result" and json.loads("".join(texts[:end["parts"]]))["stdout"] == "b" * 20_000_000
    run(tmp_path, body)
