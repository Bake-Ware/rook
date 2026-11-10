"""rook.kit.cli.stream against a worker with only the primitives (the real
shell plugin and its streams): stdin in, lines out as they come, and the
process ended when a line says so, on the timeout or when the call is
cancelled."""

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.builtins import handlers
from rook.core2.worker.host import CallError, Host
from rook.kit.cli import Place, itself, stream
from rook.kit.ctx import KitError
from rook.streams import Streams


class Ctx:
    """call_on to one worker: its shell and file plugins and stream caps."""
    plugin = "tester"

    def __init__(self, worker, streams):
        self.worker, self.builtins = worker, handlers(None, streams, None)

    caller = ME

    async def call_as(self, who, cap, args, target=None):
        assert who.plugin == "tester" and not (who.person or who.agent)      # as the plugin itself
        return await self.call_on(target, cap, **args)

    async def call_on(self, wid, cap, **args):
        assert wid == "w1"
        try:
            if cap.startswith("stream."):
                return await self.builtins[cap](SimpleNamespace(caller=ME), **args)
            return await self.worker.call(cap, args, ME)
        except CallError as e:
            raise KitError(e.message) from None


def run(body):
    async def main():
        streams = Streams(host="w1")
        worker = Host(os_name="linux", streams=streams)
        await worker.load(PLUGINS / "shell")
        await worker.load(PLUGINS / "file")
        try:
            return await body(itself(Ctx(worker, streams)))
        finally:
            await worker.close()
    return go(main)


PY = [sys.executable, "-c"]


def test_lines_come_as_they_come_and_stdin_ends():
    seen = []
    script = "import sys; d = sys.stdin.read(); print(d.upper(), flush=True); print('bye', file=sys.stderr); sys.exit(2)"
    code, tail = run(lambda ctx: stream(ctx, "w1", PY + [script],
                                        stdin="one\ntwo", timeout="5s", name="Tester", setting="tester.x",
                                        line=seen.append))
    assert code == 2 and seen == ["ONE", "TWO", "bye"] and tail.endswith("bye")


def gone(marker):
    import time
    for _ in range(50):
        if not subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip():
            return True
        time.sleep(0.1)
    return False


def test_a_line_ends_it_and_so_does_the_timeout():
    """Checked while the worker still runs: stream ends the process itself."""
    async def stopped(ctx):
        code, _ = await stream(ctx, "w1", PY + ["import time; print('ERROR now', flush=True); "
                                                "time.sleep(30)  # kit-cli-a"],
                               stdin="", timeout="10s", name="Tester", setting="tester.x",
                               line=lambda text: text.startswith("ERROR"))
        return code, await asyncio.to_thread(gone, "kit-cli-a")
    assert run(stopped) == (None, True)

    async def slow(ctx):
        with pytest.raises(KitError, match="Tester took longer than 300ms") as e:
            await stream(ctx, "w1", PY + ["import time; time.sleep(30)  # kit-cli-b"], stdin="",
                         timeout="300ms", name="Tester", setting="tester.x")
        return e.value.retry, await asyncio.to_thread(gone, "kit-cli-b")
    assert run(slow) == (True, True)


def test_a_cancelled_call_ends_the_process():
    async def body(ctx):
        task = asyncio.ensure_future(stream(ctx, "w1", PY + ["import time; print('up', flush=True); "
                                                             "time.sleep(30)  # kit-cli-c"],
                                            stdin="", timeout="20s", name="Tester", setting="tester.x"))
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await asyncio.to_thread(gone, "kit-cli-c")
    assert run(body)


def test_a_missing_program_says_what_to_set():
    with pytest.raises(KitError, match="nope-xyz not found: install Tester or set tester.program"):
        run(lambda ctx: stream(ctx, "w1", ["nope-xyz"], stdin="", timeout="5s", name="Tester",
                               setting="tester.program"))


def test_place_home_and_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OS", raising=False)

    async def body(ctx):
        place = await Place.of(ctx, "w1")
        return place.expand("~/bin/claude"), await place.folder("work")
    program, work = run(body)
    assert program == f"{tmp_path}/bin/claude" and work == f"{tmp_path}/.rook-agents/tester/work"
    assert (tmp_path / ".rook-agents" / "tester" / "work").is_dir()
