"""What every stress round shares (brief 48): a hub and workers started from this clone with the
README's own commands, an agent token got the way a person gets one (the setup page, then the Auth
page), the MCP door called the way an agent calls it, and each process's RSS, open file
descriptors and thread count read from /proc.

Nothing here waits with a deadline. The HTTP client has no total, connect or read timeout and no
limit on connections, so a call that never answers keeps its request open, and the round that made
it waits for it: that is a forever wait to write down (RULES.md, 17 November).

Every round writes its numbers as JSON lines to a log file named after the round, under the round's
directory in /tmp, and prints them too.
"""

import asyncio
import json
import os
import pathlib
import signal
import sys
import time

import aiohttp

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYTHON = sys.executable
WHY = "Stress test (brief 48): load on a local hub."
NAME, PASSWORD = "stress", "stress test password"


def env(*paths, home=None):
    e = dict(os.environ, PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in paths), PYTHONUNBUFFERED="1")
    if home is not None:
        e["HOME"] = str(home)
    return e


class Log:
    """One JSON object per line, to the file and to stdout."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a")

    def __call__(self, what, **fields):
        line = json.dumps({"t": round(time.time(), 3), "what": what, **fields})
        self.file.write(line + "\n")
        self.file.flush()
        print(line[:600], flush=True)


# ------------------------------------------------------------------ processes

class Process:
    """A child process whose stdout is copied to a file and watched for lines."""

    def __init__(self, label, argv, environ, out):
        self.label, self.argv, self.environ, self.out = label, argv, environ, pathlib.Path(out)
        self.proc, self.lines, self._seen = None, [], asyncio.Event()

    async def start(self):
        self.lines = []
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=ROOT, env=self.environ, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        self._copy = asyncio.create_task(self._copy_out())
        return self

    async def _copy_out(self):
        with open(self.out, "ab") as f:
            async for line in self.proc.stdout:
                f.write(b"%.3f " % time.time() + line)   # when each line was said
                f.flush()
                self.lines.append(line.decode(errors="replace").rstrip("\n"))
                self._seen.set()

    async def line(self, text):
        """Wait for a line holding `text` (no deadline) and return it."""
        while True:
            for line in self.lines:
                if text in line:
                    return line
            if self.proc.returncode is not None and self._copy.done():
                raise RuntimeError(f"{self.label} exited ({self.proc.returncode}): {self.lines[-5:]}")
            self._seen.clear()
            await self._seen.wait()

    @property
    def pid(self):
        return self.proc.pid

    def send(self, signum):
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(signum)

    async def stop(self, signum=signal.SIGTERM):
        self.send(signum)
        code = await self.proc.wait()
        await self._copy
        return code


class HubProcess(Process):
    def __init__(self, base, port=0):
        self.base = pathlib.Path(base)
        self.port = port
        super().__init__("hub", [], env("protocol/python", "hub"), self.base / "hub.out")

    async def start(self):
        self.argv = [PYTHON, "-m", "rook_hub", "--data", str(self.base / "hub"), "--vault-key",
                     str(self.base / "vault.key"), "--port", str(self.port)]
        await super().start()
        self.url = (await self.line("Rook hub at ")).split("Rook hub at ")[1].rstrip("/")
        self.port = int(self.url.rsplit(":", 1)[1])   # a restart listens where the workers already look
        return self


class WorkerProcess(Process):
    """`python -m rook_worker URL` with its own HOME, so its state is <home>/.rook-worker."""

    def __init__(self, base, label, url):
        self.home = pathlib.Path(base) / label
        self.home.mkdir(parents=True, exist_ok=True)
        super().__init__(label, [PYTHON, "-m", "rook_worker", url + "/"],
                         env("protocol/python", "core/python", home=self.home), pathlib.Path(base) / f"{label}.out")

    @property
    def key(self):
        from nacl.signing import SigningKey
        return SigningKey(bytes.fromhex((self.home / ".rook-worker" / "key").read_text().strip())).verify_key.encode().hex()


def stats(pid):
    """RSS and peak RSS in MB, open file descriptors, threads and CPU seconds used so far of one process,
    from /proc."""
    try:
        status = pathlib.Path(f"/proc/{pid}/status").read_text()
        fields = dict(line.split(":", 1) for line in status.splitlines())
        ticks = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"rss_mb": round(int(fields["VmRSS"].split()[0]) / 1024, 1),
                "peak_mb": round(int(fields["VmHWM"].split()[0]) / 1024, 1),     # the most RSS it has had
                "fds": len(os.listdir(f"/proc/{pid}/fd")), "threads": int(fields["Threads"]),
                "cpu_s": round((int(ticks[11]) + int(ticks[12])) / os.sysconf("SC_CLK_TCK"), 1)}
    except (FileNotFoundError, ProcessLookupError):
        return None


def snapshot(procs):
    return {p.label: stats(p.pid) for p in procs if p.proc and p.proc.returncode is None}


# ------------------------------------------------------------------ the door

class Door:
    """An MCP client of the hub's door: one JSON-RPC message per POST, as an agent sends it."""

    def __init__(self, url, token):
        self.url, self.token = url, token
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=None, sock_read=None, sock_connect=None),
            connector=aiohttp.TCPConnector(limit=0, limit_per_host=0))

    async def close(self):
        await self.http.close()

    async def rpc(self, method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        async with self.http.post(self.url + "/mcp", json=body,
                                  headers={"Authorization": f"Bearer {self.token}"}) as reply:
            return reply.status, json.loads(await reply.read())

    async def tool(self, name, **arguments):
        """(error code or None, the reply or the error text). A transport failure raises."""
        status, body = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        if "result" not in body:
            return f"rpc_{status}", json.dumps(body)
        text = body["result"]["content"][0]["text"]
        if body["result"]["isError"]:
            return text.split(":", 1)[0], text
        return None, json.loads(text)

    async def call(self, worker, cap, args, why=WHY):
        """A call through the door. A reply that is "running" is followed on its stream to its end,
        as an agent does: the stream's last entry is the reply. Returns (code, reply, followed)."""
        code, reply = await self.tool("call", worker=worker, cap=cap, args=args, why=why)
        if code or not (isinstance(reply, dict) and reply.get("running")):
            return code, reply, False
        position, last = 0, None
        while True:
            code, read = await self.tool("stream", action="read", stream=reply["stream"], **{"from": position}, wait=40)
            if code:
                return code, read, True
            if read["entries"]:
                last = read["entries"][-1]
            position = read["next"]
            if not read["open"]:
                break
        end = json.loads(last["out"])
        if end["type"] == "error":
            return end["code"], end, True
        return None, end["result"], True


async def site_login(url):
    """Make the first user on the setup page (or log in), and mint an agent token on the Auth page.
    Returns (agent token, session cookie)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None),
                                     cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:   # unsafe: an IP address
        async with http.get(url + "/api/setup") as r:
            needed = (await r.json())["needed"]
        async with http.post(url + ("/api/setup" if needed else "/api/login"),
                             json={"name": NAME, "password": PASSWORD}) as r:
            assert r.status == 200, await r.text()
        cookies = {c.key: c.value for c in http.cookie_jar}
        async with http.post(url + "/api/tokens") as r:
            assert r.status == 200, await r.text()
            token = (await r.json())["token"]
    return token, cookies


async def approve(door, workers):
    """Approve each worker process through the door (worker.approve), by its key; wait until linked."""
    keys = {}
    for w in workers:
        await w.line("Knocking at") if not (w.home / ".rook-worker" / "identity.json").exists() else None
    for w in workers:
        if (w.home / ".rook-worker" / "identity.json").exists():
            continue
        while True:   # its knock reaches the hub a moment after it says it is knocking
            _, listed = await door.tool("workers")
            if any(k["key"] == w.key for k in listed["knocking"]):
                break
            await asyncio.sleep(0.1)
        code, reply, _ = await door.call("hub", "worker.approve", {"key": w.key})
        assert code is None, reply
        keys[w.label] = reply["approved"]["moniker"]
    for w in workers:
        await w.line("Linked to")
    return keys


async def ready(door, monikers, caps=("shell.exec", "shell.open", "worker.ping")):
    """Wait until every worker announces the default plugins' caps (pushed after approval)."""
    while True:
        _, listed = await door.tool("workers")
        have = {w["moniker"]: set(w["caps"]) for w in listed["workers"] if w["online"]}
        if all(set(caps) <= have.get(m, set()) for m in monikers):
            return
        await asyncio.sleep(0.2)


class Band:
    """A hub and `n` workers under one directory in /tmp, with an agent token and a door."""

    def __init__(self, base, n=1, log=None):
        self.base = pathlib.Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.n = n
        self.log = log or Log(self.base / "round.jsonl")

    async def start(self):
        self.hub = await HubProcess(self.base).start()
        tokenfile = self.base / "token.json"
        if tokenfile.exists():
            saved = json.loads(tokenfile.read_text())
            self.token, self.cookies = saved["token"], saved["cookies"]
        else:
            self.token, self.cookies = await site_login(self.hub.url)
            tokenfile.write_text(json.dumps({"token": self.token, "cookies": self.cookies}))
            tokenfile.chmod(0o600)
        self.door = Door(self.hub.url, self.token)
        self.workers = [await WorkerProcess(self.base, f"w{i + 1}", self.hub.url).start() for i in range(self.n)]
        await approve(self.door, self.workers)
        _, listed = await self.door.tool("workers")
        self.monikers = [w["moniker"] for w in listed["workers"]]
        await ready(self.door, self.monikers)
        self.log("band", hub=self.hub.url, workers=self.monikers)
        return self

    @property
    def procs(self):
        return [self.hub, *self.workers]

    def measure(self, when, **extra):
        self.log("measure", when=when, procs=snapshot(self.procs), **extra)

    async def stop(self):
        await self.door.close()
        for w in self.workers:
            await w.stop()
        await self.hub.stop()


def main(round_name, body, n=1):
    """Run one round: `python tests/stress/<round>.py [--keep] [--log FILE]`. The hub and workers live
    in a new directory in /tmp, removed when the round ends unless --keep; the log is kept."""
    import argparse
    import shutil
    import tempfile
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the round's directory in /tmp")
    parser.add_argument("--log", help="where to write the round's JSON lines (default: in /tmp)")
    args = parser.parse_args()
    base = pathlib.Path(tempfile.mkdtemp(prefix=f"rook-stress-{round_name}-"))
    log = Log(args.log or pathlib.Path(tempfile.gettempdir()) / f"rook-stress-{round_name}.jsonl")

    async def run():
        band = await Band(base, n, log).start()
        try:
            await body(band)
        finally:
            band.measure("end")
            await band.stop()
    try:
        asyncio.run(run())
    finally:
        for out in base.glob("*.out"):
            log("output", file=out.name, tail=out.read_text(errors="replace").splitlines()[-15:])
        if not args.keep:
            shutil.rmtree(base)
