"""A job script's own process (castling-jobs), run by scripts/jobs.py as
``python -I child.py`` in a scratch folder. Standard library only.

The script is plain Python with ``def main(job)``; what it returns is the
run's result. ``job`` is the workflow library::

    def main(job):
        files = job.call("file.list", {"path": "/srv/backups"}, on="nas", why="see last night's backups")
        if not files["entries"]:
            if job.ask("No backup last night. Run one now?", ["yes", "no"]) == "yes":
                job.retry(lambda: job.call("shell.exec", {"argv": ["backup.sh"]}, on="nas"), times=3)
            job.notify("Last night's backup was missing.")
        a, b = job.parallel(lambda: job.call("battery.status", on="phone"),
                            lambda: job.call("hub.workers"))
        return {"files": len(files["entries"])}

- ``job.call(cap, args=None, *, on=None, why=None)``: one cap call, made by
  the hub as the job's identity through dispatch (rights, gates, holds,
  journal). ``on`` names the worker. Raises ``job.Error`` (``.retry`` says
  trying later may work).
- ``job.notify(text, *, title=None, to=None)``: ``notify.send``, by default
  to the job's person (a persona's or plugin's job: the owners).
- ``job.ask(question, options=None, *, timeout="1h")``: tells the person and
  waits for ``jobs.answer``; None if nobody answers in time.
- ``job.wait(duration)``, ``job.retry(fn, *, times=3, wait="10s")``,
  ``job.parallel(*fns)`` (threads; results in order, the first error raised),
  ``job.log(*parts)`` (``print`` logs too).
- ``job.name``, ``job.run``, ``job.trigger``, ``job.attempt``, ``job.vars``,
  ``job.event`` ({name, payload} or None), ``job.payload``, ``job.last`` (when
  the last successful run started, or None), ``job.person``.

Protocol: the first stdin line is ``{script, func, job}``. Then JSON lines:
to the hub ``{op: call|ask, id, ...}``, ``{op: log, text}``, finally ``{op:
done, result}`` or ``{op: error, error}``; from the hub ``{id, ok, result |
error, retry}``. stdout is the protocol's: anything else written there goes
to stderr, which the hub keeps in the run's log.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import sys
import threading
import time
import traceback

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", str(value))
    if not parts or "".join(a + b for a, b in parts) != str(value).strip():
        raise JobError(f"not a duration: {value!r}")
    return sum(float(a) * _UNITS[b] for a, b in parts)


class JobError(Exception):
    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.message, self.retry = message, retry


class Wire:
    def __init__(self, out, inp):
        self.out, self.inp = out, inp
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.waiting: dict[int, list] = {}          # id -> [event, reply]
        self.gone = False
        threading.Thread(target=self._read, daemon=True).start()

    def send(self, msg: dict) -> None:
        line = json.dumps(msg, default=str)
        with self.lock:
            self.out.write(line + "\n")
            self.out.flush()

    def request(self, msg: dict) -> object:
        rid = next(self.ids)
        slot = self.waiting[rid] = [threading.Event(), None]
        if self.gone:
            raise JobError("the hub went away")
        self.send({**msg, "id": rid})
        slot[0].wait()
        self.waiting.pop(rid, None)
        reply = slot[1]
        if reply is None:
            raise JobError("the hub went away")
        if not reply.get("ok"):
            raise JobError(str(reply.get("error") or "failed"), bool(reply.get("retry")))
        return reply.get("result")

    def _read(self) -> None:
        for line in self.inp:
            try:
                reply = json.loads(line)
                slot = self.waiting.get(reply.get("id"))
            except (ValueError, AttributeError):
                continue
            if slot is not None:
                slot[1] = reply
                slot[0].set()
        self.gone = True
        for slot in list(self.waiting.values()):
            slot[0].set()


class Job:
    Error = JobError

    def __init__(self, info: dict, wire: Wire):
        self._wire = wire
        self.name, self.id, self.run = info["name"], info["id"], info["run"]
        self.trigger, self.attempt = info.get("trigger"), info.get("attempt", 1)
        self.vars = info.get("vars") or {}
        self.event = info.get("event")
        self.payload = (self.event or {}).get("payload")
        self.last = info.get("last")
        self.person = info.get("person")          # whose job it is (None: a persona's or a plugin's)

    def call(self, cap: str, args: dict | None = None, *, on: str | None = None, why: str | None = None):
        return self._wire.request({"op": "call", "cap": cap, "args": args or {}, "on": on, "why": why})

    def notify(self, text: str, *, title: str | None = None, to: str | None = None):
        to = to or self.person
        args = {"text": text, **({"title": title} if title else {}), **({"to": to} if to else {})}
        return self.call("notify.send", args, why=f"tell {to or 'the owners'}: {title or text[:60]}")

    def ask(self, question: str, options: list | None = None, *, timeout="1h"):
        return self._wire.request({"op": "ask", "question": question, "options": options, "timeout": timeout})

    def wait(self, duration) -> None:
        time.sleep(seconds(duration))

    def retry(self, fn, *, times: int = 3, wait="10s"):
        for i in range(times):
            try:
                return fn()
            except JobError as e:
                if i == times - 1:
                    raise
                self.log(f"retrying after: {e.message}")
                self.wait(wait)

    def parallel(self, *fns) -> list:
        results, errors = [None] * len(fns), [None] * len(fns)

        def go(i, fn):
            try:
                results[i] = fn()
            except BaseException as e:      # handed back below
                errors[i] = e
        threads = [threading.Thread(target=go, args=(i, fn), daemon=True) for i, fn in enumerate(fns)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        first = next((e for e in errors if e is not None), None)
        if first is not None:
            raise first
        return results

    def log(self, *parts) -> None:
        self._wire.send({"op": "log", "text": " ".join(map(str, parts))})


class _Printer:
    """``print`` in a script: lines go to the run's log."""

    def __init__(self, wire: Wire):
        self.wire, self.buf = wire, ""

    def write(self, text: str) -> int:
        self.buf += text
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            self.wire.send({"op": "log", "text": line})
        return len(text)

    def flush(self) -> None:
        pass


def main() -> int:
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)                       # stray writes to stdout land in the log, not the protocol
    cfg = json.loads(sys.stdin.readline())
    wire = Wire(out, sys.stdin)
    sys.stdout = _Printer(wire)
    try:
        ns = {"__name__": "rook_job", "__file__": cfg["script"]}
        with open(cfg["script"], encoding="utf-8") as f:
            exec(compile(f.read(), cfg["script"], "exec"), ns)
        fn = ns.get(cfg["func"])
        if not callable(fn):
            raise JobError(f"the script has no function {cfg['func']}")
        result = fn(Job(cfg["job"], wire))
        wire.send({"op": "done", "result": result})
        return 0
    except JobError as e:
        wire.send({"op": "error", "error": e.message})
    except BaseException as e:
        traceback.print_exc()
        wire.send({"op": "error", "error": f"{type(e).__name__}: {e}"})
    return 1


if __name__ == "__main__":
    sys.exit(main())
