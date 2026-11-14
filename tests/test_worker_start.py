"""The worker start command (R1, the part before an installer; R2, R4, R5, W7, H1): `python -m
rook_worker HUB_URL` as a person runs it from a clone, against the real hub start command, both as
real processes on localhost. Its state is in ~/.rook-worker, so each worker here gets a throwaway
HOME. It knocks and says it waits; once approved it links, is online and answers a call; SIGTERM and
Ctrl-C end it with code 0; started again it links with no second knock. A hub that cannot be reached,
a URL that is not a hub, and a revoked key each end it with one plain line, no traceback; a hub lost
after approval is waited for, said once."""

import asyncio
import os
import signal
import socket
import sys
import time

import aiohttp
from aiohttp import web

from test_first_run import ROOT, Started, start_command
from test_link_worker import free_port

NAME = socket.gethostname()     # the name a worker knocks with
STOPS = 5                       # seconds a stopped worker may take to exit


class WorkerStarted:
    """`python -m rook_worker URL` with HOME set to `home`: its output lines as they come."""

    def __init__(self, home, url):
        self.home, self.url = home, url
        self.lines = []

    async def start(self):
        env = dict(os.environ, HOME=str(self.home),
                   PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ("protocol/python", "core/python")))
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "rook_worker", self.url, cwd=ROOT, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._reading = asyncio.create_task(self._read())
        return self

    async def _read(self):
        while line := await self.process.stdout.readline():
            self.lines.append(line.decode().rstrip("\n"))

    async def said(self, text, timeout=20):
        """Wait until a line of output starts with `text`; return it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.lines:
                if line.startswith(text):
                    return line
            assert self.process.returncode is None or not self._reading.done(), (self.lines, await self.errors())
            await asyncio.sleep(0.05)
        raise AssertionError(f"never said {text!r}: {self.lines}")

    async def ended(self, signum=None, timeout=STOPS):
        """Send `signum` if given; the exit code, within `timeout` seconds, and stderr."""
        if signum is not None:
            self.process.send_signal(signum)
        code = await asyncio.wait_for(self.process.wait(), timeout)
        await self._reading
        return code, await self.errors()

    async def errors(self):
        return (await self.process.stderr.read()).decode() if self.process.returncode is not None else ""

    def kill(self):
        if self.process.returncode is None:
            self.process.kill()


async def ask(client, method, url, body=None):
    async with client.request(method, url, json=body) as response:
        return response.status, await response.json()


async def set_up(hub):
    """The hub's first user, signed in: the site client a person approves workers with."""
    client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    assert (await ask(client, "POST", hub.base + "/api/setup", {"name": "Alice", "password": "a password"}))[0] == 200
    return client


async def pending(client, base):
    return (await ask(client, "GET", base + "/api/workers"))[1]["pending"]


async def worker_row(client, base, moniker):
    view = (await ask(client, "GET", base + "/api/workers"))[1]
    return next(w for group in view["groups"] for w in group["workers"] if w["moniker"] == moniker)


async def a_call(client, base, moniker):
    """A call through the hub to the worker: net.http, a cap every worker serves (D3), on the hub's own setup URL."""
    return await ask(client, "POST", base + "/api/call", {"worker": moniker, "cap": "net.http",
                                                          "args": {"url": base + "/api/setup"}, "why": "a test call"})


async def approve(client, hub, worker):
    """Approve the one pending knock; wait until the worker says it is linked and the hub has it online."""
    [knock] = await pending(client, hub.base)
    status, approved = await ask(client, "POST", hub.base + "/api/approve", {"key": knock["key"]})
    assert status == 200, approved
    moniker = approved["approved"]["moniker"]
    assert await worker.said(f"Approved as {moniker}.")
    await worker.said(f"Linked to {hub.base} as {moniker}.")
    assert (await worker_row(client, hub.base, moniker))["online"]
    return knock, moniker


def test_start_knock_approve_serve_stop_and_start_again(tmp_path):
    asyncio.run(start_and_again(tmp_path))


async def start_and_again(tmp_path):
    workers = []
    async with Started(tmp_path / "data", tmp_path / "vault.key") as hub:
        client = await set_up(hub)
        try:
            # First start: it knocks under the machine's name and says it is waiting for approval.
            worker = await WorkerStarted(tmp_path / "home", hub.base).start()
            workers.append(worker)
            await worker.said(f"Waiting for approval: on the hub, approve the pending worker named {NAME}.")
            assert worker.lines[0] == f"Rook worker {NAME}, state in {tmp_path / 'home' / '.rook-worker'}"
            assert worker.lines[1].startswith(f"  Knocking at {hub.base} with the key in ")
            [knock] = await pending(client, hub.base)                   # R4: name and offered caps, before approval
            assert knock["name"] == NAME and {"net.http", "net.tcp", "net.udp"} <= {c["name"] for c in knock["caps"]}
            await asyncio.sleep(0.5)
            assert worker.process.returncode is None and not any(line.startswith("Linked") for line in worker.lines)

            # Approved: it links, is online, and answers a call through the hub.
            knock, moniker = await approve(client, hub, worker)
            status, reply = await a_call(client, hub.base, moniker)
            assert status == 200 and reply["type"] == "result" and reply["result"]["status"] == 200, reply
            assert sum(line.startswith("Waiting for approval") for line in worker.lines) == 1    # said once

            # SIGTERM ends it with code 0, no traceback.
            started = time.monotonic()
            code, errors = await worker.ended(signal.SIGTERM)
            assert code == 0 and errors == "" and time.monotonic() - started < STOPS
            assert worker.lines[-1] == "Stopped."

            # Started again on the same state: the same key and identity, straight to the link (R5, W7).
            again = await WorkerStarted(tmp_path / "home", hub.base).start()
            workers.append(again)
            await again.said(f"Linked to {hub.base} as {moniker}.")
            assert again.lines[1] == f"  Approved before as {moniker}: linking to {hub.base}."
            assert not any("Knocking" in line or "Waiting" in line for line in again.lines)
            assert await pending(client, hub.base) == []                # no second knock
            view = (await ask(client, "GET", hub.base + "/api/workers"))[1]
            assert [w["moniker"] for group in view["groups"] for w in group["workers"]] == [moniker]
            status, reply = await a_call(client, hub.base, moniker)
            assert status == 200 and reply["type"] == "result", reply

            # Ctrl-C (SIGINT) ends it with code 0 too.
            started = time.monotonic()
            code, errors = await again.ended(signal.SIGINT)
            assert code == 0 and errors == "" and time.monotonic() - started < STOPS
        finally:
            await client.close()
            for worker in workers:
                worker.kill()


def test_plain_failures_before_approval(tmp_path):
    asyncio.run(before_approval(tmp_path))


async def before_approval(tmp_path):
    def one_line(errors):
        assert "Traceback" not in errors and errors.count("\n") == 1, errors
        return errors.strip()

    # A hub that cannot be reached: one line, code 1.
    port = free_port()
    worker = await WorkerStarted(tmp_path / "home", f"http://127.0.0.1:{port}").start()
    code, errors = await worker.ended()
    assert code == 1 and one_line(errors) == (
        f"rook worker: cannot reach the hub at http://127.0.0.1:{port} (Connection refused). "
        "Check the address and that the hub is running, then start again.")

    # A URL where something answers, but not a hub.
    runner = web.AppRunner(web.Application())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    other = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        worker = await WorkerStarted(tmp_path / "home", other).start()
        code, errors = await worker.ended()
        assert code == 1 and one_line(errors) == (
            f"rook worker: {other} did not answer as a Rook hub. "
            "Check the address: it is the one the hub prints when it starts.")
    finally:
        await runner.cleanup()

    # Not an address at all.
    worker = await WorkerStarted(tmp_path / "home", "hub.example").start()
    code, errors = await worker.ended()
    assert code == 1 and one_line(errors).startswith("rook worker: hub.example is not a hub address.")

    # Ctrl-C while it waits for approval: code 0.
    async with Started(tmp_path / "data", tmp_path / "vault.key") as hub:
        worker = await WorkerStarted(tmp_path / "home", hub.base).start()
        try:
            await worker.said("Waiting for approval")
            code, errors = await worker.ended(signal.SIGINT)
            assert code == 0 and errors == "" and worker.lines[-1] == "Stopped."
        finally:
            worker.kill()


class HubOnPort:
    """The hub start command on a fixed port, so a stopped hub comes back at the same address."""

    def __init__(self, data, key, port):
        self.args, self.env = start_command(data, key)
        self.args[self.args.index("--port") + 1] = str(port)
        self.base = f"http://127.0.0.1:{port}"

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(*self.args, cwd=ROOT, env=self.env,
                                                            stdout=asyncio.subprocess.PIPE)
        while not (await asyncio.wait_for(self.process.stdout.readline(), 20)).startswith(b"  vault:"):
            pass
        return self

    async def stop(self):
        self.process.terminate()
        assert await asyncio.wait_for(self.process.wait(), 20) == 0


def test_a_lost_hub_is_waited_for_and_a_revoked_key_ends_it(tmp_path):
    asyncio.run(lost_and_revoked(tmp_path))


async def lost_and_revoked(tmp_path):
    data, key, port = tmp_path / "data", tmp_path / "vault.key", free_port()
    hub = await HubOnPort(data, key, port).start()
    client = await set_up(hub)
    workers = []
    try:
        first = await WorkerStarted(tmp_path / "first", hub.base).start()
        workers.append(first)
        await first.said("Waiting for approval")
        _, moniker = await approve(client, hub, first)

        # The hub stops: the worker says the link was lost and why it cannot link, once, and waits.
        await hub.stop()
        await first.said("The link to the hub was lost; linking again.")
        cannot = f"Cannot link to the hub: cannot reach {hub.base} (Connection refused). Trying again."
        await first.said(cannot)
        await asyncio.sleep(2)
        assert first.process.returncode is None and first.lines.count(cannot) == 1
        # The hub comes back: the worker links again by itself.
        hub = await HubOnPort(data, key, port).start()
        await client.close()
        client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        assert (await ask(client, "POST", hub.base + "/api/login", {"name": "Alice", "password": "a password"}))[0] == 200
        deadline = time.monotonic() + 40
        while first.lines.count(f"Linked to {hub.base} as {moniker}.") < 2:
            assert time.monotonic() < deadline, first.lines
            await asyncio.sleep(0.1)

        # A rekey: another key knocks and a person attaches it to this worker; the old key is revoked.
        second = await WorkerStarted(tmp_path / "second", hub.base).start()
        workers.append(second)
        await second.said("Waiting for approval")
        [knock] = await pending(client, hub.base)
        status, reply = await ask(client, "POST", hub.base + "/api/call", {
            "worker": "hub", "cap": "worker.attach", "args": {"key": knock["key"], "worker": moniker},
            "why": "a rekey"})
        assert status == 200 and reply["type"] == "result", reply
        refused = (f"rook worker: the hub refused this worker's key. This key was revoked from {moniker}. "
                   "Generate a new key, knock with it, and ask a person or agent to attach it. "
                   f"This worker's keys and identity are in {tmp_path / 'first' / '.rook-worker'}.")
        # The hub drops the revoked key's session without a GOODBYE, so the worker finds the link lost
        # after its PEER_TIMEOUT (15 s, rook_link.py), then link_open is refused.
        code, errors = await first.ended(timeout=30)
        assert code == 1 and errors == refused + "\n", errors
        await second.said(f"Linked to {hub.base} as {moniker}.")       # the new key carries on the identity

        # Started again with the revoked key: the same line at once, and no knock.
        again = await WorkerStarted(tmp_path / "first", hub.base).start()
        workers.append(again)
        code, errors = await again.ended()
        assert code == 1 and errors == refused + "\n" and await pending(client, hub.base) == []
        code, errors = await second.ended(signal.SIGTERM)
        assert code == 0 and errors == ""
    finally:
        await client.close()
        for worker in workers:
            worker.kill()
        if hub.process.returncode is None:
            await hub.stop()
