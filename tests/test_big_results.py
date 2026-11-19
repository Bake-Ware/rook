"""A big result is a stream (brief 54; docs/protocol.md, "A big result"). A real hub, served, with
the MCP door and the site, and a real rook_worker over its real carrier (Telesthete over loopback
WebSocket), serving the default plugins and one plugin of the test's. A result whose JSON text is
within PIECE characters comes whole, as ever; one past it comes as {running, stream}, its pieces the
stream's entries in order, and its reply, last, says how many."""

import asyncio
import base64
import json
import os
import time

import rook_hub
from helpers import eventually
from rook_hub import Hub, journal
from rook_worker import PIECE, Worker
from test_door import mcp_client, tool
from test_site import ask, browser, login

PLUGIN = '''
import asyncio
from rook_worker import Cap, Text

async def sized(args):
    """An ordinary value: a string whose JSON text is `chars` characters."""
    return "z" * (args["chars"] - 2)

async def slow(args):
    """Waits, then gives an ordinary list of `rows` rows."""
    await asyncio.sleep(args["sleep"])
    return {"rows": [{"n": i, "text": "row %d" % i} for i in range(args["rows"])]}

async def broken(args):
    """Text that gives `chars` characters, then fails."""
    async def pieces():
        for _ in range(args["chars"] // 100_000):
            yield "b" * 100_000
        raise RuntimeError("the disk went away")
    return {"text": Text(pieces())}

CAPS = {
    "big.sized": Cap("A string whose JSON text is chars characters.", {"type": "object"}, sized),
    "big.slow": Cap("Wait sleep seconds, then give rows rows.", {"type": "object"}, slow),
    "big.broken": Cap("Give chars characters, then fail.", {"type": "object"}, broken),
}
'''
BIG = ["big.broken", "big.sized", "big.slow"]


class Band:
    """A served hub with the door, Alice and her agent token, and one real worker, approved, that
    has the default plugins and the test's `big` plugin."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    async def __aenter__(self):
        self.hub = Hub(self.tmp_path / "hub", vault_key=os.urandom(32))
        host, port = await self.hub.serve("127.0.0.1", 0)
        self.base = f"http://{host}:{port}"
        self.alice = self.hub.auth.create_user("Alice", "a password")["guid"]
        self.agent = self.hub.auth.mint("agent", user=self.alice)
        folder = self.tmp_path / "big"
        folder.mkdir()
        (folder / "plugin.json").write_text(json.dumps({"name": "big", "version": "1.0.0",
                                                        "worker": {"python": {"module": "worker.py"}}}))
        (folder / "worker.py").write_text(PLUGIN)
        self.hub.plugins.install(folder, {"user": self.alice, "token": None})
        self.worker = Worker(self.tmp_path / "box", self.base, name="box", macs=[], knock_every=0.02)
        self.task = asyncio.create_task(self.worker.run())
        await eventually(lambda: any(p["key"] == self.worker.key for p in self.hub.list_pending()))
        self.moniker = self.hub.approve(self.worker.key, user=self.alice)["moniker"]
        await eventually(lambda: {"shell.exec", "file.read", *BIG} <= {
            c["name"] for c in self.hub.get_worker(self.moniker)["caps"] or []}, timeout=20)
        self._door = mcp_client(f"{self.base}/mcp", self.agent)
        self.door = await self._door.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._door.__aexit__(*exc)
        self.worker.stop()
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 30)
        await self.hub.stop_serving()
        self.hub.close()

    async def call(self, cap, **args):
        """A call through the door: (whether it failed, what it answered)."""
        return await tool(self.door, "call", worker=self.moniker, cap=cap, args=args, why="a test of big results")

    async def follow(self, stream):
        """Read a stream through the door, with waits, until it closes, as the door tells an agent to."""
        entries, position = [], 0
        while True:
            failed, read = await tool(self.door, "stream", action="read", stream=stream, wait=5, **{"from": position})
            assert not failed, read
            entries, position = entries + read["entries"], read["next"]
            if not read["open"]:
                return entries

    async def big(self, cap, **args):
        """A call whose result comes in pieces: (the result, the reply, the number of pieces, seconds
        until the door answered). The pieces are the stream's entries, in order, and the reply last."""
        started = time.monotonic()
        failed, answer = await self.call(cap, **args)
        answered = time.monotonic() - started
        assert not failed and answer["running"] is True and answer["worker"] == self.moniker, answer
        entries = await self.follow(answer["stream"])
        reply = json.loads(entries[-1]["out"])
        assert reply["type"] == "result" and reply["result"] is None and reply["stream"] == answer["stream"], reply
        assert len(entries) == reply["parts"] + 1 and [e["position"] for e in entries] == list(range(len(entries)))
        return json.loads("".join(e["out"] for e in entries[:-1])), reply, reply["parts"], answered

    def journaled(self, cap):
        return journal.read(self.hub, cap=cap, kind="call", limit=1)[0]


def test_a_result_within_the_switch_point_is_whole_and_one_past_it_is_a_stream(tmp_path):
    asyncio.run(switch_point(tmp_path))


async def switch_point(tmp_path):
    async with Band(tmp_path) as band:
        # Just within: one result message, the reply as ever, no stream.
        failed, answer = await band.call("big.sized", chars=PIECE)
        assert not failed and answer == "z" * (PIECE - 2)
        assert band.hub.streams.list() == []
        assert band.journaled("big.sized")["result"]["result"] == "z" * (PIECE - 2)

        # Just past: {running, stream} at once; two pieces, whole and in order; the journal keeps the
        # reply that names the stream, not the result.
        result, reply, parts, answered = await band.big("big.sized", chars=PIECE + 1)
        assert result == "z" * (PIECE - 1) and parts == 2 and answered < rook_hub.CALL_WAIT
        kept = band.journaled("big.sized")["result"]
        assert kept["parts"] == 2 and kept["stream"] == reply["stream"] and kept["result"] is None
        [stream] = band.hub.streams.list()
        assert not stream["open"] and stream["closed_by"] == "hub" and stream["cap"] == "big.sized"


def test_the_built_in_caps_give_a_big_result_whole_and_in_order(tmp_path):
    asyncio.run(built_in(tmp_path))


async def built_in(tmp_path):
    async with Band(tmp_path) as band:
        # shell.exec: about 5.9 MB of output, the code and ok after it.
        result, reply, parts, answered = await band.big("shell.exec", argv=["seq", "1", "1000000"])
        assert result == {"code": 0, "ok": True, "stderr": "", "stdout": "".join(f"{n}\n" for n in range(1, 1_000_001))}
        assert parts >= 6 and answered < rook_hub.CALL_WAIT
        # ... and with as much on stderr, which never stops the command on a full pipe.
        result, *_ = await band.big("shell.exec", cmd="seq 1 300000; seq 1 300000 >&2; exit 3")
        assert result["stdout"] == result["stderr"] == "".join(f"{n}\n" for n in range(1, 300_001))
        assert result["code"] == 3 and result["ok"] is False

        # file.read: 3 MB of bytes as base64, and as text.
        raw = os.urandom(3_000_000)
        (tmp_path / "bytes").write_bytes(raw)
        result, *_ = await band.big("file.read", path=str(tmp_path / "bytes"), encoding="base64")
        assert base64.b64decode(result["content"]) == raw and result["size"] == 3_000_000
        assert "truncated" not in result
        text = "é and some text\n" * 200_000
        (tmp_path / "text").write_text(text, "utf-8")
        result, *_ = await band.big("file.read", path=str(tmp_path / "text"), max_bytes=2_500_001)
        assert result["content"] == text.encode("utf-8")[:2_500_001].decode("utf-8", "replace")
        assert result["truncated"] is True

        # file.list: 12,000 entries; file.search: 20,000 matches, in order.
        folder = tmp_path / "many"
        folder.mkdir()
        for i in range(12_000):
            (folder / f"file-number-{i:05d}.txt").touch()
        result, *_ = await band.big("file.list", path=str(folder))
        assert sorted(e["name"] for e in result["entries"]) == [f"file-number-{i:05d}.txt" for i in range(12_000)]
        (tmp_path / "lines.txt").write_text("".join(f"line {i} has a needle in it\n" for i in range(20_000)))
        result, *_ = await band.big("file.search", pattern="needle", path=str(tmp_path / "lines.txt"))
        assert [m["line"] for m in result["matches"]] == list(range(1, 20_001)) and "truncated" not in result

        # The same file.read through the site, as a page's call() and follow() do it.
        async with browser() as client:
            assert (await login(client, band.base, "Alice", "a password"))[0] == 200
            status, answer = await ask(client, "POST", f"{band.base}/api/call", {
                "worker": band.moniker, "cap": "file.read", "args": {"path": str(tmp_path / "bytes"), "encoding": "base64"},
                "why": "read it on a page"})
            assert status == 200 and answer["type"] == "running", answer
            entries, position, open_ = [], 0, True
            while open_:
                status, read = await ask(client, "GET", f"{band.base}/api/streams/{answer['stream']}/read?from={position}&wait=5")
                entries, position, open_ = entries + read["entries"], read["next"], read["open"]
            data = [base64.b64decode(e["data"]) for e in entries]
            reply = json.loads(data[-1])
            assert base64.b64decode(json.loads(b"".join(data[:reply["parts"]]))["content"]) == raw


def test_a_slow_big_result_an_error_after_pieces_and_the_hubs_own_callers(tmp_path, monkeypatch):
    monkeypatch.setattr(rook_hub, "CALL_WAIT", 0.5)
    asyncio.run(slow_and_broken(tmp_path))


async def slow_and_broken(tmp_path):
    async with Band(tmp_path) as band:
        # Slow, then big: "running" at the end of the wait, the stream empty until the pieces come.
        result, reply, parts, answered = await band.big("big.slow", sleep=2, rows=60_000)
        assert result == {"rows": [{"n": i, "text": f"row {i}"} for i in range(60_000)]}
        assert 0.5 <= answered < 2 and parts >= 2

        # Text that fails after 3 MB: the pieces sent stay on the stream, and its last entry is the error.
        failed, answer = await band.call("big.broken", chars=3_000_000)
        assert not failed and answer["running"] is True
        entries = await band.follow(answer["stream"])
        error = json.loads(entries[-1]["out"])
        assert error["code"] == "cap_failed" and "the disk went away" in error["message"] and len(entries) >= 4

        # A caller inside the hub (a job, a device through a worker) gets the reply naming its stream,
        # and Hub.joined gives the value.
        reply = await band.hub.call(band.moniker, "big.sized", {"chars": 3 * PIECE}, "for a job", user=band.alice)
        assert reply["type"] == "result" and reply["parts"] == 3 and reply["result"] is None
        assert band.hub.joined(reply) == "z" * (3 * PIECE - 2)
        small = await band.hub.call(band.moniker, "big.sized", {"chars": 10}, "small", user=band.alice)
        assert "parts" not in small and band.hub.joined(small) == "z" * 8

        # A vault value in a big result is scrubbed on the stream, as the journal scrubs a result (L4).
        band.hub.vault.create({"scope": "hub", "name": "deploy-key", "value": "hunter2-correct-horse-battery"},
                              {"user": band.alice, "token": None})
        result, *_ = await band.big("shell.exec", cmd="seq 1 300000; echo hunter2-correct-horse-battery")
        assert result["stdout"].endswith("300000\n(secret:deploy-key)\n")
