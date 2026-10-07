"""sessions.* — one catalog and one set of verbs for every agent session here.

A **session** is one agent conversation (or one shell) on this host, keyed
``(agent, native_id)``: Claude's session id, Codex's rollout id, or, for a
shell or an agent that has not reported its id yet, the Rook terminal id. The
record shape is the contract in docs/design/sessions.md §3.1.

The worker is the source of truth for its own sessions, so the catalog is
built from what this host can see:

* Rook terminals (``work.stream.list``): the raw PTY tier, started by Rook.
* Claude/Codex history (``*-history.pull``): every transcript, live or not.
* Process evidence (:mod:`rook.worker.agent_activity`): which sessions a live
  process holds, Claude's busy/idle status from its PID markers, and which
  terminal runs which native session.
* The Claude Code mod's mirror spool (§3.4), when one exists.

``sessions.send`` puts text in front of the agent as the user: the session's
inbox when it has one (Claude peer messaging, Codex control socket), else
keystrokes into the Rook terminal. ``sessions.stop`` ends only what Rook
started; a session someone started in their own terminal is theirs to end.

``sessions.mirror`` (the live event tier) lives in ``session_mirror.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

from ..plugin import Plugin, capability
from .. import agent_activity

log = logging.getLogger("rook.worker.plugins.sessions")

AGENTS = ("claude", "codex", "hermes", "shell")
TRANSCRIPT_AGENTS = ("claude", "codex")
MAX_TEXT = 24000
MAX_KEYS = 16 * 1024                # work.stream.write's input limit
SEARCH_WINDOW = 500                 # history scanned for a query (as work.sessions did)
LIVE_WINDOW = 50                    # history scanned for live_only; live ones outside it come from process evidence
COUNT_EVERY = 120.0                 # seconds between heartbeat recounts
_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_INBOUND = ("accept", "hold", "refuse")


# -- mirror spool (§3.4) ------------------------------------------------------------

def worker_home() -> Path:
    """The worker's state dir: ``~/.rook-band-worker`` (``%USERPROFILE%`` on
    Windows), or ``ROOK_WORKER_HOME``."""
    override = os.environ.get("ROOK_WORKER_HOME")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("USERPROFILE") if sys.platform == "win32" else None
    return Path(home or Path.home()) / ".rook-band-worker"


def mirror_spool_path(agent: str, native_id: str, part: int = 0) -> Path | None:
    """Where the mod writes a session's events; ``part`` 1 is the rotated
    file. None for ids that cannot be a file name."""
    if agent not in AGENTS or not isinstance(native_id, str) or not _ID.fullmatch(native_id) \
            or native_id.startswith("."):
        return None
    name = f"{native_id}.{part}.jsonl" if part else f"{native_id}.jsonl"
    return worker_home() / "mirror" / agent / name


def mirror_spool_exists(agent: str, native_id: str) -> bool:
    for part in (0, 1):
        path = mirror_spool_path(agent, native_id, part)
        if path is not None and path.is_file():
            return True
    return False


def mirror_inbound(agent: str, native_id: str) -> str | None:
    """The ``inbound`` setting the mod saw at ``session.start`` (the first
    spool line), or None."""
    for part in (1, 0):  # the rotated file holds the oldest lines
        path = mirror_spool_path(agent, native_id, part)
        try:
            with open(path, encoding="utf-8") as f:
                first = json.loads(f.readline() or "{}")
        except (OSError, TypeError, ValueError):
            continue
        if isinstance(first, dict) and first.get("type") == "session.start":
            value = first.get("inbound")
            return value if isinstance(value, str) else None
    return None


# -- Claude's inbox policy --------------------------------------------------------

def _claude_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".claude"
    return Path.home() / ".claude"


def managed_settings_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ClaudeCode" / "managed-settings.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def _json(path: Path, cache: dict | None = None) -> dict:
    key = str(path)
    if cache is not None and key in cache:
        return cache[key]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    if cache is not None:
        cache[key] = data
    return data


def _bypass_flag(argv: list[str]) -> bool:
    for i, arg in enumerate(argv):
        if arg == "--dangerously-skip-permissions":
            return True
        if arg == "--permission-mode" and i + 1 < len(argv) and argv[i + 1] == "bypassPermissions":
            return True
        if arg == "--permission-mode=bypassPermissions":
            return True
    return False


def inbox_policy(cwd: str | None = None, argv: list[str] | None = None,
                 inbound: str | None = None, home: Path | None = None,
                 cache: dict | None = None) -> str:
    """What Claude Code does with a message another session (Rook) sends:
    ``accept`` (starts a turn), ``hold`` (waits for the person's OK),
    ``refuse`` (dropped) or ``unknown``.

    ``crossSessionInbound`` comes from managed settings, else the user's
    settings (it is a user setting; the mod's mirror reports the live value
    as ``inbound``). Its ``default`` holds messages while permissions are
    bypassed: by ``--dangerously-skip-permissions`` / ``--permission-mode
    bypassPermissions`` on the command line, or ``permissions.defaultMode``
    in the settings layers (managed, project local, project, user). Without
    the command line (Windows) a default that settings don't decide is
    ``unknown``."""
    home = home or _claude_home()
    managed = _json(managed_settings_path(), cache)
    user = _json(home / "settings.json", cache)
    value = inbound if inbound in _INBOUND else None
    if value is None:
        for layer in (managed, user):
            if layer.get("crossSessionInbound") in _INBOUND:
                value = layer["crossSessionInbound"]
                break
    if value:
        return value
    if argv and _bypass_flag(argv):
        return "hold"
    layers = [managed]
    if cwd:
        layers += [_json(Path(cwd) / ".claude" / "settings.local.json", cache),
                   _json(Path(cwd) / ".claude" / "settings.json", cache)]
    layers.append(user)
    for layer in layers:
        mode = (layer.get("permissions") or {}).get("defaultMode") if isinstance(layer.get("permissions"), dict) else None
        if mode:
            return "hold" if mode == "bypassPermissions" else ("accept" if argv else "unknown")
    return "accept" if argv else "unknown"


# -- the record --------------------------------------------------------------------

def record(agent: str, native_id: str, *, worker=None, meta: dict | None = None,
           term: dict | None = None, marker: dict | None = None, live: bool = False,
           activity: str | None = None, messageable: bool = False,
           policy: str = "unknown", mirror: bool = False) -> dict:
    """One §3.1 session record from what the worker knows about it."""
    meta = meta or {}
    running = bool(term and term.get("running"))
    live = live or running
    if not live:
        state = "closed"
    elif marker and marker.get("status"):
        state = "idle" if marker["status"] == "idle" else "live"
    elif activity == "ready" and not running:
        state = "idle"
    else:
        state = "live"
    inbox = live and messageable and policy != "refuse"
    if inbox and policy != "hold":
        how = "inbox"
    elif running:
        how = "pty"
    elif inbox:
        how = "inbox"
    else:
        how = "none"
    transcript = agent in TRANSCRIPT_AGENTS and not (term and native_id == term.get("id"))
    updated = meta.get("last_modified") or (term or {}).get("last_output") \
        or ((marker or {}).get("updatedAt") or 0) / 1000 or None
    out = {
        "key": f"{getattr(worker, 'worker_id', '') or ''}/{agent}/{native_id}",
        "worker_id": getattr(worker, "worker_id", None), "worker": getattr(worker, "name", None),
        "agent": agent, "native_id": native_id,
        "title": meta.get("title") or (term or {}).get("title") or (marker or {}).get("name") or native_id,
        "cwd": meta.get("cwd") or (term or {}).get("cwd") or (marker or {}).get("cwd"),
        "state": state,
        "origin": "rook" if term else "external",
        "updated": updated, "messages": meta.get("message_count"),
        "view": {"terminal": term["id"] if term else None, "mirror": bool(mirror),
                 "transcript": transcript},
        "input": how,
        "inbox_policy": policy,
        "links": {"work_session": term["session"]} if term and term.get("session") else {},
        "resumable": state == "closed" and transcript,
    }
    if activity:
        out["activity"] = activity
    pid = (marker or {}).get("pid") or (term or {}).get("pid")
    if pid and live:
        out["pid"] = pid
    if term and term.get("model"):
        out["model"] = term["model"]
    return out


def _rank(rec: dict) -> tuple:
    return (rec["state"] == "closed", -(rec.get("updated") or 0))


# -- plugin ------------------------------------------------------------------------

class SessionsPlugin(Plugin):
    NAMESPACE = "sessions"
    NAME = "sessions"
    SKILL = ("Sessions: `sessions.list(query, live_only)` is this host's catalog of agent "
             "sessions (Claude, Codex, Hermes, shells), live ones first, each with state "
             "live/idle/closed, how to view it (terminal id, mirror, transcript) and how input "
             "reaches it. `sessions.follow(agent, native_id, offset, version)` tails a "
             "transcript; `sessions.send(agent, native_id, text)` puts text in front of the "
             "agent as the user (delivery turn, held or keys); `sessions.stop` ends a session "
             "Rook started. Resume a closed one with work.stream.open(harness=agent, resume=id).")

    def __init__(self) -> None:
        super().__init__()
        self._worker = None
        self._counts: dict | None = None
        self._counter: asyncio.Task | None = None

    def available(self) -> bool:
        from .claude_history import _default_root
        from .codex_history import _root as codex_root
        if _default_root().is_dir() or codex_root().is_dir():
            return True
        return sys.platform != "win32" and os.name == "posix"   # shells in Rook terminals

    def bind_worker(self, worker) -> None:
        self._worker = worker

    async def start(self) -> None:
        self._counter = asyncio.create_task(self._count_loop())

    async def stop(self) -> None:
        if self._counter is not None:
            self._counter.cancel()
            await asyncio.gather(self._counter, return_exceptions=True)
            self._counter = None

    def heartbeat(self) -> dict | None:
        # hb.sessions = {live, idle}: what the hub shows before anyone opens
        # the Sessions page. Recounted in the background, never here.
        return dict(self._counts) if self._counts is not None else None

    # -- sources ---------------------------------------------------------------

    @property
    def _reg(self):
        return self._worker.registry if self._worker is not None else None

    def _has(self, cap: str) -> bool:
        return self._reg is not None and self._reg.has(cap)

    async def _call(self, cap: str, **args):
        result = await self._reg.call(cap, **args)
        return result if isinstance(result, dict) else {}

    async def _terminals(self) -> list[dict]:
        if not self._has("work.stream.list"):
            return []
        try:
            return list((await self._call("work.stream.list")).get("terminals") or [])
        except Exception:
            log.debug("work.stream.list failed", exc_info=True)
            return []

    @staticmethod
    def _processes() -> dict:
        """One process scan: Claude markers and the holders of each agent's
        live sessions (blocking; run in a thread)."""
        table = agent_activity.process_table()
        markers = agent_activity.claude_markers(table=table)
        owners = {a: agent_activity.session_owners(a, table=table) for a in TRANSCRIPT_AGENTS}
        return {"table": table, "markers": markers, "owners": owners}

    @staticmethod
    def _term_native(term: dict, procs: dict) -> str:
        agent = term.get("harness")
        if agent in TRANSCRIPT_AGENTS:
            if term.get("resume"):
                return str(term["resume"]).lower()
            if term.get("running") and term.get("pid"):
                sid = agent_activity.session_under(term["pid"], procs["owners"][agent], procs["table"])
                if sid:
                    return sid
        return term["id"]

    @staticmethod
    def _meta(agent: str, native_id: str) -> dict:
        """Transcript metadata for one session by exact id ({} if none)."""
        try:
            if agent == "claude":
                from .claude_history import _default_root, _resolve_session, _session_meta
                path = _resolve_session(_default_root(), native_id)
                return _session_meta(path) if path is not None and path.stem.lower() == native_id else {}
            from .codex_history import CodexHistoryPlugin, _resolve, _root
            path = _resolve(_root(), native_id)
            return CodexHistoryPlugin._session_meta(path) if path is not None else {}
        except Exception:
            return {}

    def _live_ids(self, agent: str, procs: dict) -> set[str]:
        ids = set(procs["owners"].get(agent, {}).values())
        if agent == "claude":
            ids |= set(procs["markers"])
        return ids

    def _policy(self, agent: str, native_id: str, cwd, marker, messageable, cache) -> str:
        if agent == "claude":
            return inbox_policy(cwd, (marker or {}).get("argv"), mirror_inbound(agent, native_id),
                                cache=cache)
        if agent == "codex":
            return "accept" if messageable else "unknown"
        return "unknown"

    async def _catalog(self, pull: int, offset: int = 0) -> tuple[list[dict], int]:
        """Every record this host knows of (history up to ``pull`` entries per
        agent), plus the summed history total."""
        terms = await self._terminals()
        procs = await asyncio.to_thread(self._processes)
        by_key: dict[tuple, dict] = {}
        term_of: dict[tuple, dict] = {}
        for t in terms:
            agent = t.get("harness") if t.get("harness") in AGENTS else "shell"
            key = (agent, self._term_native(t, procs))
            # A running terminal wins over a finished one for the same session.
            if key not in term_of or t.get("running"):
                term_of[key] = t
        total = 0
        cache: dict = {}
        live_ids = {a: self._live_ids(a, procs) for a in TRANSCRIPT_AGENTS}
        for agent in TRANSCRIPT_AGENTS:
            seen = set()
            if self._has(f"{agent}-history.pull") and pull > 0:
                try:
                    res = await self._call(f"{agent}-history.pull", limit=pull, offset=offset)
                except Exception:
                    log.debug("%s history pull failed", agent, exc_info=True)
                    res = {}
                total += int(res.get("total") or 0) if res.get("ok") else 0
                for s in res.get("sessions") or [] if res.get("ok") else []:
                    sid = s.get("session_id")
                    if not sid or s.get("error"):
                        continue
                    seen.add(sid.lower())
                    by_key[(agent, sid.lower())] = dict(meta=s, live=bool(s.get("active")),
                                                        activity=s.get("activity"),
                                                        messageable=bool(s.get("messageable")))
            # Live sessions outside the history window, or with no transcript yet.
            from ..session_messages import messageable
            for sid in live_ids[agent] - seen:
                meta = await asyncio.to_thread(self._meta, agent, sid)
                if meta:
                    total += 1
                by_key[(agent, sid)] = dict(meta=meta, live=True, activity=None,
                                            messageable=await asyncio.to_thread(messageable, agent, sid))
        for key in term_of:
            if key not in by_key:
                by_key[key] = dict(meta={}, live=False, activity=None, messageable=False)
                if key[0] not in TRANSCRIPT_AGENTS or key[1] == term_of[key]["id"]:
                    total += 1
        out = []
        for (agent, sid), info in by_key.items():
            term = term_of.get((agent, sid))
            marker = procs["markers"].get(sid) if agent == "claude" else None
            live = info["live"] or sid in live_ids.get(agent, ())
            cwd = info["meta"].get("cwd") or (term or {}).get("cwd") or (marker or {}).get("cwd")
            policy = self._policy(agent, sid, cwd, marker, info["messageable"], cache)
            out.append(record(agent, sid, worker=self._worker, meta=info["meta"], term=term,
                              marker=marker, live=live, activity=info["activity"],
                              messageable=info["messageable"], policy=policy,
                              mirror=mirror_spool_exists(agent, sid)))
        out.sort(key=_rank)
        return out, total

    async def _find(self, agent: str, native_id: str) -> dict:
        """One session's live facts, fresh: its terminal, whether a process
        holds it, its inbox and policy."""
        if agent not in AGENTS:
            raise ValueError(f"agent must be one of {', '.join(AGENTS)}")
        if not isinstance(native_id, str) or not _ID.fullmatch(native_id):
            raise ValueError("invalid native_id")
        terms = await self._terminals()
        procs = await asyncio.to_thread(self._processes)
        sid = native_id.lower() if agent in TRANSCRIPT_AGENTS else native_id
        term = None
        for t in terms:
            if (t.get("harness") or "shell") != agent:
                continue
            if t.get("id") == native_id or self._term_native(t, procs) == sid:
                if term is None or t.get("running"):
                    term = t
        if term is not None and term.get("id") == native_id:
            sid = self._term_native(term, procs)
        live = sid in self._live_ids(agent, procs) if agent in TRANSCRIPT_AGENTS else False
        messageable = False
        if live:
            from ..session_messages import messageable as can_message
            messageable = await asyncio.to_thread(can_message, agent, sid)
        marker = procs["markers"].get(sid) if agent == "claude" else None
        cwd = (term or {}).get("cwd") or (marker or {}).get("cwd")
        policy = self._policy(agent, sid, cwd, marker, messageable, {})
        rec = record(agent, sid, worker=self._worker, term=term, marker=marker, live=live,
                     messageable=messageable, policy=policy, mirror=mirror_spool_exists(agent, sid))
        return {"record": rec, "term": term, "native_id": sid, "live": live or bool(term and term.get("running"))}

    # -- caps --------------------------------------------------------------------

    @capability("list", risk="read", limit=20)
    async def list(self, limit: int = 20, offset: int = 0, query: str = "",
                   live_only: bool = False) -> dict:
        """This host's agent sessions, live and idle first, then closed,
        newest first within each (record shape: docs/design/sessions.md). ``query`` matches title, cwd and id;
        ``live_only`` drops closed ones. Page with ``limit``/``offset``.
        Resume a closed one with work.stream.open(harness=agent,
        resume=native_id, cwd=cwd)."""
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        live_only = live_only in (True, 1, "1", "true", "yes")
        query = str(query or "").strip().lower()
        pull = SEARCH_WINDOW if query else LIVE_WINDOW if live_only else offset + limit
        items, total = await self._catalog(pull)
        self._counts = self._tally(items)
        if live_only:
            items = [i for i in items if i["state"] != "closed"]
        if query:
            items = [i for i in items
                     if query in f"{i.get('title')} {i.get('cwd')} {i['native_id']}".lower()]
        if live_only or query:
            total = len(items)
        total = max(total, len(items))
        from .terminals import available_harnesses
        return {"ok": True, "harnesses": await asyncio.to_thread(available_harnesses)
                if self._has("work.stream.open") else [],
                "items": items[offset:offset + limit], "total": total,
                "next_offset": offset + limit if offset + limit < total else None}

    @capability("follow", risk="read")
    async def follow(self, agent: str, native_id: str, offset: int = 0, version: str = "") -> dict:
        """The transcript tail of a Claude or Codex session: pages from
        ``offset`` when the log changed since ``version`` (else
        ``unchanged``), as claude-history.follow returns them."""
        if agent not in TRANSCRIPT_AGENTS:
            raise ValueError("only claude and codex sessions have a transcript; view the terminal")
        cap = f"{agent}-history.follow"
        if not self._has(cap):
            return {"ok": False, "error": f"{agent} history is not available on this host"}
        out = await self._call(cap, session_id=native_id, offset=int(offset), version=str(version or ""))
        return {**out, "agent": agent, "native_id": native_id}

    @capability("send", risk="exec")
    async def send(self, agent: str, native_id: str, text: str, command_id: str = "") -> dict:
        """Put ``text`` in front of the agent as the user. Through the
        session's inbox when it has one (Claude peer messaging, Codex control
        socket), else as keystrokes into its Rook terminal (text, then Enter).
        Returns ``{ok, delivery, note}``: ``turn`` (arrives as a user turn),
        ``held`` (waits for approval on this host) or ``keys`` (typed into
        the terminal). ``command_id`` makes an inbox retry safe."""
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return {"ok": False, "error": f"Enter a message of 1–{MAX_TEXT} characters."}
        found = await self._find(agent, native_id)
        rec, term, sid = found["record"], found["term"], found["native_id"]
        route = rec["input"]
        if route == "inbox":
            cap = f"{agent}-history.send"
            if not self._has(cap):
                return {"ok": False, "error": f"{agent} history is not available on this host"}
            command_id = str(command_id or uuid.uuid4().hex)
            res = await self._call(cap, session_id=sid, text=text, command_id=command_id)
            if not res.get("ok"):
                return res
            held = rec["inbox_policy"] == "hold"
            note = ("Waiting for approval on this host: Claude Code holds messages from other "
                    "sessions here (crossSessionInbound).") if held else res.get("note") or "Delivered."
            return {"ok": True, "delivery": "held" if held else "turn", "note": note,
                    "detail": res.get("delivery"), "native_id": sid}
        if route == "pty":
            body = text.rstrip("\r\n")
            if "\n" in body and agent != "shell":
                # Bracketed paste keeps newlines as text instead of Enter.
                body = "\x1b[200~" + body + "\x1b[201~"
            payload = body + "\r"
            if len(payload.encode()) > MAX_KEYS:
                return {"ok": False, "error": f"Terminal input is limited to {MAX_KEYS} bytes."}
            await self._call("work.stream.write", id=term["id"], data=payload)
            return {"ok": True, "delivery": "keys", "note": "Typed into the Rook terminal.",
                    "terminal": term["id"], "native_id": sid}
        if rec["inbox_policy"] == "refuse" and found["live"]:
            return {"ok": False, "error": "Claude Code on this host refuses messages from other "
                                          "sessions (crossSessionInbound = refuse)."}
        if found["live"]:
            return {"ok": False, "error": "This session has no inbox Rook can reach and no Rook "
                                          "terminal. Type on its host, or run /rook-move there."}
        return {"ok": False, "error": "This session is closed. Resume it first."}

    @capability("stop", risk="exec")
    async def stop_session(self, agent: str, native_id: str) -> dict:
        """End a session Rook started: close its Rook terminal (or the
        ``proc.*`` process an older resume started). A session started in
        someone's own terminal is refused: end it there."""
        found = await self._find(agent, native_id)
        term, sid = found["term"], found["native_id"]
        if term is not None and term.get("running"):
            res = await self._call("work.stream.close", id=term["id"])
            return {"ok": True, "stopped": "terminal", "terminal": term["id"],
                    "exit_code": res.get("exit_code"), "native_id": sid}
        if agent in TRANSCRIPT_AGENTS and self._has(f"{agent}-history.resumed"):
            resumed = await self._call(f"{agent}-history.resumed")
            for s in resumed.get("sessions") or []:
                if s.get("session_id") == sid and s.get("running") and s.get("handle") \
                        and self._has("proc.close"):
                    await self._call("proc.close", handle=s["handle"])
                    return {"ok": True, "stopped": "process", "handle": s["handle"], "native_id": sid}
        if found["live"]:
            return {"ok": False, "error": "This session was started outside Rook. End it on its "
                                          "host, or run /rook-move there to hand it to Rook."}
        return {"ok": True, "stopped": None, "note": "Already closed.", "native_id": sid}

    # -- heartbeat counts ------------------------------------------------------------

    @staticmethod
    def _tally(items: list[dict]) -> dict:
        return {"live": sum(1 for i in items if i["state"] == "live"),
                "idle": sum(1 for i in items if i["state"] == "idle")}

    async def _quick_counts(self) -> dict:
        """Live/idle counts from process evidence and terminals only (no
        transcript reads)."""
        terms = await self._terminals()
        procs = await asyncio.to_thread(self._processes)
        live = idle = 0
        claimed = set()
        for sid, marker in procs["markers"].items():
            claimed.add(("claude", sid))
            if marker.get("status") == "idle":
                idle += 1
            else:
                live += 1
        for agent in TRANSCRIPT_AGENTS:
            for sid in set(procs["owners"][agent].values()):
                if (agent, sid) not in claimed:
                    claimed.add((agent, sid))
                    live += 1
        for t in terms:
            if t.get("running") and (t.get("harness") or "shell", self._term_native(t, procs)) not in claimed:
                live += 1
        return {"live": live, "idle": idle}

    async def _count_loop(self) -> None:
        while True:
            try:
                self._counts = await self._quick_counts()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("session count failed", exc_info=True)
            await asyncio.sleep(COUNT_EVERY)


PLUGIN = SessionsPlugin
