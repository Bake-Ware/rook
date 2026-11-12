"""Worker edges the lifecycle scenario does not reach."""

import asyncio
import os
import stat
import time

from memory_link import link_pair
from rook_protocol import decode, encode, validate
from rook_worker import Cap, Worker


async def echo(args):
    return args


async def no_connect(key):
    raise AssertionError("not used")


def test_heartbeat_from_this_machine_is_valid(tmp_path):
    # H1, H4: whatever this machine reports must be a valid heartbeat.
    worker = Worker(tmp_path, "http://unused", no_connect, caps={"demo.echo": Cap("Echo.", {}, echo)})
    worker.health()                       # first CPU sample
    time.sleep(0.2)
    beat = worker.heartbeat()
    validate(beat)
    assert beat["identity"] == {"name": worker.name, "key": worker.key}
    assert set(beat["facts"]) == {"os", "arch", "hostname", "form", "version", "macs"}
    assert beat["health"]["disk"]["total"] > 0
    if os.path.exists("/proc/meminfo"):
        assert {"ram", "uptime", "cpu"} <= set(beat["health"])


def test_key_is_private_to_the_worker(tmp_path):
    Worker(tmp_path, "http://unused", no_connect)
    assert stat.S_IMODE(os.stat(tmp_path / "key").st_mode) == 0o600


def test_worker_answers_a_cap_it_does_not_serve(tmp_path):
    # W5: an unservable call fails with a reason and a next step.
    async def go():
        hub_end, worker_end = link_pair("k")

        async def connect(key):
            return worker_end

        worker = Worker(tmp_path, "http://unused", connect, caps={"demo.echo": Cap("Echo.", {}, echo)})
        worker.identity = {"guid": "g", "moniker": "m", "signature": "s"}
        running = asyncio.create_task(worker.run())
        assert decode(await hub_end.recv())["type"] == "heartbeat"
        await hub_end.send(encode({"type": "call", "id": "c1", "cap": "video.capture", "args": {}}))
        reply = decode(await hub_end.recv())
        assert reply["id"] == "c1" and reply["code"] == "unknown_cap" and reply["next"]
        await hub_end.send(encode({"type": "call", "id": "c2", "cap": "demo.echo", "args": {"a": 1}}))
        assert decode(await hub_end.recv()) == {"type": "result", "id": "c2", "result": {"a": 1}}
        worker.stop()
        await running
    asyncio.run(go())
