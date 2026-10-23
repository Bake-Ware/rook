"""Side threads off a role's main session (design 3.2): forks and by-the-way questions.

A **fork** is ``claude -p --resume <main id> --fork-session`` in
``<role>/forks/<fork>/`` (or a git worktree there): a copy of the main
conversation that goes on with its own task, its own hub token and the main
lane's tools. With ``at: last_completed`` while main is mid-turn, it is cut at
the last completed turn (``--resume-session-at <uuid>``).

A **by-the-way** question is a throwaway fork (``--no-session-persistence``,
no tools, no MCP servers) asked one short question; it answers from the main
session's current transcript and leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import roledir
from .lane import MODEL, STREAM_LIMIT, TEXT_CHARS, LaneError, _usage, base_env, check_env, \
    kill_group

if TYPE_CHECKING:  # pragma: no cover
    from .host import Host

log = logging.getLogger("rook.agents.side")

FORK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
KEEP_DONE = 20
MAX_TEXT = 200_000
BTW_PREFACE = ("A side question was asked while you are working. Answer from what you already "
               "know in this conversation, in one or two plain sentences, without tools. If a "
               "step is still running, say it is still running: it was not interrupted.")
EMPTY_MCP = f"{roledir.SCRATCH}/no-mcp.json"


class Fork:
    def __init__(self, fork_id: str, role: str, cwd: Path) -> None:
        self.id = fork_id
        self.role = role
        self.cwd = cwd
        self.state = "starting"
        self.t0 = time.monotonic()
        self.ended: float | None = None
        self.session_id: str | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.cancelled = False

    def status(self) -> dict:
        end = self.ended if self.ended is not None else time.monotonic()
        return {"fork": self.id, "role": self.role, "state": self.state,
                "session_id": self.session_id, "elapsed_s": round(end - self.t0, 1)}


class Side:
    def __init__(self, host: "Host") -> None:
        self.host = host
        self.forks: dict[str, Fork] = {}
        self._done: collections.deque = collections.deque(maxlen=KEEP_DONE)
        self._btw: set[asyncio.Task] = set()

    def running(self) -> list[Fork]:
        return [f for f in self.forks.values() if f.state in ("starting", "running")]

    def status(self) -> list[dict]:
        return [f.status() for f in [*self._done, *self.forks.values()]]

    def _main(self, role: str):
        rs = self.host.roles.get(role)
        lane = rs.lanes.get("main") if rs else None
        if lane is None:
            raise LaneError(f"role {role!r} has no main lane here")
        sid = lane.session_id or lane.sessions.get("main").get("id")
        if not sid:
            raise LaneError("the main session has not started yet")
        return rs, lane, sid

    # -- forks --------------------------------------------------------------------
    async def fork(self, op: dict) -> dict:
        fork_id = str(op.get("fork") or "")
        role = roledir.check_role(op.get("role"))
        text = str(op.get("text") or "")
        if not FORK_ID.match(fork_id):
            raise LaneError("fork: 1-64 of A-Z a-z 0-9 _ . -")
        if not text.strip() or len(text) > MAX_TEXT:
            raise LaneError("text: the side thread's task")
        if fork_id in self.forks:
            raise LaneError(f"side thread {fork_id} already exists")
        model = str(op.get("model") or "")
        if model and not MODEL.match(model):
            raise LaneError("model: an alias or a model id")
        env = check_env(op.get("env"))
        at = str(op.get("at") or "last_completed")
        if at not in ("last_completed", "now"):
            raise LaneError("at: last_completed or now")
        repo = op.get("repo")

        def refuse(error: str) -> dict:
            self.host.emit(role, "main", "fork", fork=fork_id, state="failed", error=error)
            raise LaneError(error)

        if len(self.running()) >= self.host.cfg.max_forks:
            refuse("too many side threads")
        try:
            rs, lane, sid = self._main(role)
        except LaneError as e:
            refuse(str(e))
        cut = None
        if at == "last_completed" and lane.turn is not None:
            cut = lane.last_completed_uuid
        cwd = rs.dir / "forks" / fork_id
        if cwd.exists():
            refuse(f"forks/{fork_id} already exists")
        fork = Fork(fork_id, role, cwd)
        self.forks[fork_id] = fork
        argv = [self.host.cfg.claude, "-p", "--resume", sid, "--fork-session"]
        if cut:
            argv += ["--resume-session-at", cut]
        argv += ["--output-format", "stream-json", "--verbose",
                 "--setting-sources", "project,local",
                 "--settings", str(rs.dir / ".claude" / "settings.json"),
                 "--strict-mcp-config", "--mcp-config", str(rs.dir / ".mcp.json"),
                 "--permission-mode", "dontAsk", "--tools", ",".join(lane.spec.tools)]
        model = model or lane.spec.model
        if model:
            argv += ["--model", model]
        child_env = base_env(self.host.cfg.token_env)
        child_env.update(env)
        fork.task = asyncio.create_task(self._run_fork(fork, argv, child_env, text, repo))
        return {"fork": fork_id, "state": "starting", "from": "last_completed" if cut else "now",
                "resume_session_at": cut}

    async def _run_fork(self, fork: Fork, argv: list[str], env: dict, text: str,
                        repo: Any) -> None:
        def emit(**kw) -> None:
            self.host.emit(fork.role, "main", "fork", fork=fork.id, **kw)

        final: dict | None = None
        texts: list[str] = []
        err_tail: collections.deque = collections.deque(maxlen=8)
        try:
            if repo:
                err = await self._worktree(str(repo), fork)
                if err:
                    raise LaneError(err)
            else:
                fork.cwd.mkdir(parents=True, exist_ok=True)
            if fork.cancelled:
                return
            fork.proc = proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(fork.cwd), env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT, start_new_session=True)
            fork.state = "running"
            assert proc.stdin is not None and proc.stdout is not None
            # The task goes in on stdin: no argv length limit, never read as a flag.
            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            async def errs() -> None:
                assert proc.stderr is not None
                async for raw in proc.stderr:
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        err_tail.append(line[:300])

            err_task = asyncio.create_task(errs())
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError:
                    continue
                if not raw:
                    break
                with contextlib.suppress(ValueError):
                    ev = json.loads(raw)
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("type") == "system" and ev.get("subtype") == "init" \
                            and fork.session_id is None and ev.get("session_id"):
                        fork.session_id = str(ev["session_id"])
                        emit(state="started", session_id=fork.session_id)
                    elif ev.get("type") == "assistant" and not ev.get("parent_tool_use_id"):
                        for b in (ev.get("message") or {}).get("content") or []:
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                                texts.append(str(b["text"]))
                    elif ev.get("type") == "result":
                        final = ev
            await proc.wait()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(err_task, 2)
            if fork.cancelled:
                return
            ms = round((time.monotonic() - fork.t0) * 1000)
            if final is not None and not final.get("is_error"):
                out = final.get("result") if isinstance(final.get("result"), str) else ""
                fork.state = "done"
                emit(state="done", session_id=fork.session_id,
                     text=(out or (texts[-1] if texts else ""))[:TEXT_CHARS],
                     usage=_usage(final.get("usage")), duration_ms=ms,
                     cost_usd=final.get("total_cost_usd"), num_turns=final.get("num_turns"))
            else:
                why = (str(final.get("result") or final.get("subtype")) if final else "") \
                    or " | ".join(list(err_tail)[-3:]) or f"claude exited {proc.returncode}"
                fork.state = "failed"
                emit(state="failed", session_id=fork.session_id, error=why[:500],
                     duration_ms=ms)
        except LaneError as e:
            fork.state = "failed"
            emit(state="failed", error=str(e)[:500])
        except OSError as e:
            fork.state = "failed"
            emit(state="failed", error=f"cannot start claude: {e.strerror or e}"[:500])
        except asyncio.CancelledError:
            if fork.proc is not None:
                await kill_group(fork.proc)
            raise
        finally:
            fork.ended = time.monotonic()
            if fork.cancelled and fork.state != "cancelled":
                fork.state = "cancelled"
                emit(state="cancelled", session_id=fork.session_id)
            self.forks.pop(fork.id, None)
            self._done.append(fork)

    async def _worktree(self, repo: str, fork: Fork) -> str | None:
        if not repo.startswith("/") or not Path(repo).is_dir():
            return "repo: an absolute path to a local git checkout"
        fork.cwd.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo, "worktree", "add", str(fork.cwd), "-b", f"fork/{fork.id}",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 60)
        except (OSError, asyncio.TimeoutError) as e:
            return f"git worktree add failed: {type(e).__name__}"
        if proc.returncode != 0:
            return "git worktree add failed: " + out.decode("utf-8", "replace").strip()[-300:]
        return None

    async def cancel(self, fork_id: str) -> dict:
        fork = self.forks.get(str(fork_id or ""))
        if fork is None:
            return {"fork": fork_id, "cancelled": False}
        fork.cancelled = True
        if fork.proc is not None:
            await kill_group(fork.proc)
        if fork.task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(fork.task), 5)
        return {"fork": fork.id, "cancelled": True}

    async def close(self) -> None:
        for fork in list(self.forks.values()):
            await self.cancel(fork.id)
        for t in list(self._btw):
            t.cancel()

    # -- by the way ---------------------------------------------------------------
    def btw(self, op: dict) -> dict:
        req = op.get("req")
        role = roledir.check_role(op.get("role"))
        question = str(op.get("question") or "").strip()
        if not question or len(question) > 8000:
            raise LaneError("question: one short question")
        model = str(op.get("model") or "haiku")
        if not MODEL.match(model):
            raise LaneError("model: an alias or a model id")
        rs, lane, sid = self._main(role)
        task = asyncio.create_task(self._run_btw(req, rs.dir, role, sid, question, model))
        self._btw.add(task)
        task.add_done_callback(self._btw.discard)
        return {"started": True}

    async def _run_btw(self, req: Any, role_dir: Path, role: str, sid: str, question: str,
                       model: str) -> None:
        t0 = time.monotonic()
        empty = role_dir / EMPTY_MCP
        if not empty.exists():
            roledir.write_text(empty, '{"mcpServers": {}}\n')
        argv = [self.host.cfg.claude, "-p", "--resume", sid, "--fork-session",
                "--no-session-persistence", "--model", model, "--effort", "low",
                "--tools", "", "--strict-mcp-config", "--mcp-config", str(empty),
                "--setting-sources", "", "--output-format", "json"]
        prompt = f"{BTW_PREFACE}\n\nThe question: {question}"
        ok, text, error = False, "", ""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(role_dir), env=base_env(self.host.cfg.token_env),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT, start_new_session=True)
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")),
                                              self.host.cfg.btw_timeout_s)
            res = None
            for line in reversed(out.decode("utf-8", "replace").splitlines()):
                with contextlib.suppress(ValueError):
                    cand = json.loads(line)
                    if isinstance(cand, dict) and cand.get("type") == "result":
                        res = cand
                        break
            if res is not None and not res.get("is_error"):
                ok, text = True, str(res.get("result") or "")
            else:
                error = (str(res.get("result") or res.get("subtype")) if res else "") or \
                    err.decode("utf-8", "replace").strip()[-300:] or \
                    f"claude exited {proc.returncode}"
        except asyncio.TimeoutError:
            error = "the side question took too long"
        except OSError as e:
            error = f"cannot start claude: {e.strerror or e}"
        finally:
            if proc is not None and proc.returncode is None:
                await kill_group(proc)
        fields = {"req": req, "ok": ok, "text": text[:TEXT_CHARS],
                  "ms": round((time.monotonic() - t0) * 1000)}
        if error:
            fields["error"] = error[:500]
        self.host.emit(role, "main", "btw", **fields)
