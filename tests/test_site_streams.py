"""Streams as a terminal on the worker card (P8, S2-S5, L1), through real HTTP as the
browser drives them, beside an agent on the MCP door, with a real rook_worker on an
in-memory link serving a demo stream cap that echoes its input."""

import asyncio
import base64
import json

import yarl

from helpers import eventually
from memory_link import connector
from rook_hub import Hub
from rook_worker import Cap, Worker
from test_door import mcp_client, tool
from test_site import COOKIE, ask, browser, login

streams, inputs, closed_seen = [], [], []

COLOURED = b"\x1b[1;32mbuild ok\x1b[0m \xff\xfe not text\r\n"     # colour codes and bytes that are not UTF-8


async def demo(args, stream):
    """A stream that echoes its input; the test makes it send output through `streams`."""
    streams.append(stream)

    async def echo():
        while (data := await stream.read()) is not None:
            inputs.append(data)
            await stream.send(b"echo: " + data)
        closed_seen.append(stream.handle)
    asyncio.create_task(echo())
    return {"title": args.get("title", "demo"), "description": args.get("description", "")}


CAPS = {"demo.stream": Cap("Open a stream that echoes input.", {"type": "object"}, demo, opens_stream=True)}


def raw(read):
    """The bytes of a terminal read, joined, and the directions of its entries."""
    return b"".join(base64.b64decode(e["data"]) for e in read["entries"]), [e["direction"] for e in read["entries"]]


def test_site_streams(tmp_path):
    streams.clear(), inputs.clear(), closed_seen.clear()
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    agent = hub.auth.mint("agent", user=alice["guid"])
    worker = Worker(tmp_path / "w", base, connector(hub), name="box", caps=CAPS, macs=[], knock_every=0.02)
    running = asyncio.create_task(worker.run())
    await eventually(hub.list_pending)
    box = hub.approve(worker.key, user=alice["guid"])["moniker"]
    await eventually(lambda: hub.get_worker(box)["online"])
    try:
        async with mcp_client(f"{base}/mcp", agent) as mcp, browser() as client, browser() as second:
            # An agent opens two streams on box (S1); the worker sends coloured output on the first.
            failed, opened = await tool(mcp, "call", worker=box, cap="demo.stream", why="follow the build",
                                        args={"title": "build", "description": "the nightly build, live"})
            assert not failed
            name = opened["stream"]
            failed, other = await tool(mcp, "call", worker=box, cap="demo.stream", why="a second one",
                                       args={"title": "logs", "description": "the service log"})
            await streams[0].send(COLOURED)
            await eventually(lambda: hub._db.execute("SELECT count(*) FROM stream_data").fetchone()[0] == 1)
            read_url = f"{base}/api/streams/{name}/read"

            # 1. Without a session every terminal request is refused, and nothing happens (T4).
            requests = [("GET", f"/api/streams?worker={box}", None), ("GET", f"/api/streams/{name}", None),
                        ("GET", f"/api/streams/{name}/read", None),
                        ("POST", f"/api/streams/{name}/input", {"text": "ls\r"}),
                        ("POST", f"/api/streams/{name}/close", {"why": "done"})]
            for method, path, body in requests:
                status, reply = await ask(client, method, base + path, body)
                assert status == 401 and reply["code"] == "no_session", (method, path)
            async with browser() as stranger:            # an agent's token is not a session
                stranger.cookie_jar.update_cookies({COOKIE: agent})
                for method, path, body in requests:
                    status, reply = await ask(stranger, method, base + path, body)
                    assert status == 401 and reply["code"] == "token_scope", (method, path)
            assert inputs == [] and hub.streams.get(name)["open"]

            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            assert (await login(second, base, "Alice", "correct horse"))[0] == 200
            session = hub.auth.verify(client.cookie_jar.filter_cookies(yarl.URL(base))[COOKIE].value)

            # 2. The card shows each open stream's title, description, who opened it and when (S3, S5).
            status, view = await ask(client, "GET", base + "/api/workers")
            [card] = [w for g in view["groups"] for w in g["workers"] if w["moniker"] == box]
            assert [(s["moniker"], s["title"], s["description"]) for s in card["streams"]] == [
                (other["stream"], "logs", "the service log"), (name, "build", "the nightly build, live")]
            assert card["streams"][1]["opened_by"] == {"name": "Alice", "moniker": alice["moniker"]}
            assert card["streams"][1]["opened_at"] and card["streams"][1]["open"] is True
            status, reply = await ask(client, "GET", f"{base}/api/streams/{name}")
            assert reply["stream"]["title"] == "build" and reply["stream"]["worker"] == box

            # 3. The bytes as they were: colour codes and bytes that are not text, unchanged.
            status, read = await ask(client, "GET", f"{read_url}?from=0")
            assert status == 200 and raw(read) == (COLOURED, ["out"]) and read["next"] == 1 and read["open"]

            # 4. A waiting read returns when the worker sends more, in both browsers and for the
            #    agent at once (S2); the agent's text rendering is what it was.
            site_wait = asyncio.create_task(ask(client, "GET", f"{read_url}?from=1&wait=10"))
            other_wait = asyncio.create_task(ask(second, "GET", f"{read_url}?from=1&wait=10"))
            agent_wait = asyncio.create_task(tool(mcp, "stream", action="read", stream=name, wait=10, **{"from": 1}))
            await asyncio.sleep(0.3)
            assert not site_wait.done() and not other_wait.done() and not agent_wait.done()
            started = asyncio.get_running_loop().time()
            await streams[0].send(b"\x1b[33mstep 2\x1b[0m\r\n")
            (_, first), (_, again), (failed, text) = await asyncio.gather(site_wait, other_wait, agent_wait)
            assert asyncio.get_running_loop().time() - started < 3
            assert raw(first) == raw(again) == (b"\x1b[33mstep 2\x1b[0m\r\n", ["out"]) and first["next"] == 2
            assert not failed and text["entries"] == [{"position": 1, "out": "\x1b[33mstep 2\x1b[0m\r\n"}]
            failed, text = await tool(mcp, "stream", action="read", stream=name)
            assert text["entries"][0] == {"position": 0, "out": "\x1b[1;32mbuild ok\x1b[0m \\xff\\xfe not text\r\n"}

            # 5. Input from the site reaches the worker as the user's, recorded with who sent it,
            #    exactly as an agent's input is.
            status, reply = await ask(client, "POST", f"{base}/api/streams/{name}/input", {"text": "ls -l\r"})
            assert status == 200 and reply == {"written": 6}
            await eventually(lambda: inputs == [b"ls -l\r"])
            failed, _ = await tool(mcp, "stream", action="write", stream=name, text="pwd\r")
            await eventually(lambda: len(inputs) == 2)
            rows = [dict(r) for r in hub._db.execute(
                "SELECT position, direction, user, token, data FROM stream_data WHERE direction = 'in' ORDER BY position")]
            assert [(r["user"], r["data"]) for r in rows] == [(alice["guid"], b"ls -l\r"), (alice["guid"], b"pwd\r")]
            assert rows[0]["token"] == session["guid"] and rows[1]["token"] == hub.auth.verify(agent)["guid"]
            await eventually(lambda: hub._db.execute("SELECT count(*) FROM stream_data").fetchone()[0] == 6)
            status, read = await ask(client, "GET", f"{read_url}?from=2")
            assert [(e["direction"], e.get("from")) for e in read["entries"]] == [
                ("in", alice["moniker"]), ("out", None), ("in", alice["moniker"]), ("out", None)]
            assert b"echo: ls -l\r" in raw(read)[0]
            failed, text = await tool(mcp, "stream", action="read", stream=name, **{"from": 2})
            assert text["entries"][0] == {"position": 2, "in": "ls -l\r", "from": alice["moniker"]}

            # 6. Close on purpose from the site: journaled under the user, and the worker sees it (S4, L1).
            for bad in ({}, {"why": "  "}):
                status, reply = await ask(client, "POST", f"{base}/api/streams/{name}/close", bad)
                assert status == 400 and reply["code"] == "bad_message"
            assert hub.streams.get(name)["open"]
            status, reply = await ask(client, "POST", f"{base}/api/streams/{name}/close", {"why": "the build is done"})
            assert status == 200 and reply == {"closed": name}
            await eventually(lambda: closed_seen == [streams[0].handle])
            [entry] = [dict(r) for r in hub._db.execute("SELECT * FROM journal WHERE kind = 'stream'")]
            assert (entry["user"], entry["token"], entry["why"]) == (alice["guid"], session["guid"], "the build is done")
            assert json.loads(entry["result"])["closed_by"] == alice["guid"]

            # 7. A closed stream still reads from the start, at once even with a wait, and is listed
            #    with the worker's closed streams (S5); it takes no more input.
            status, read = await ask(client, "GET", f"{read_url}?from=0&wait=10")
            assert status == 200 and not read["open"] and read["next"] == 6 and raw(read)[0].startswith(COLOURED)
            status, reply = await ask(client, "GET", f"{base}/api/streams?worker={box}&open=false")
            assert [(s["moniker"], s["open"], s["closed_by"]) for s in reply["streams"]] == [
                (name, False, {"name": "Alice", "moniker": alice["moniker"]})]
            assert reply["streams"][0]["closed_at"]
            status, reply = await ask(client, "GET", f"{base}/api/streams?worker={box}")
            assert {s["moniker"] for s in reply["streams"]} == {name, other["stream"]}
            status, reply = await ask(client, "POST", f"{base}/api/streams/{name}/input", {"text": "more\r"})
            assert status == 400 and reply["code"] == "stream_closed"
            status, view = await ask(client, "GET", base + "/api/workers")
            [card] = [w for g in view["groups"] for w in g["workers"] if w["moniker"] == box]
            assert [s["moniker"] for s in card["streams"]] == [other["stream"]]

            # Nothing closes on its own: the other stream is still open after all of this (S4).
            assert hub.streams.get(other["stream"])["open"]

            # Bad requests say what to do.
            for path in (f"{read_url}?from=x", f"{read_url}?from=99", f"{read_url}?wait=-1",
                         f"{base}/api/streams/no-such-stream/read", f"{base}/api/streams",
                         f"{base}/api/streams?worker=nobody-at-all"):
                status, reply = await ask(client, "GET", path)
                assert status == 400 and reply["next"], path
            status, reply = await ask(client, "POST", f"{base}/api/streams/{other['stream']}/input", {"text": ""})
            assert status == 400 and reply["code"] == "bad_message"
    finally:
        worker.stop()
        await running
        await hub.stop_serving()
        hub.close()
