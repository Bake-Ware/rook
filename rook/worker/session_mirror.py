"""The session mirror spool: live events a Claude Code session writes about itself.

The Rook Claude Code mod writes every session's events (prompt, streamed
assistant text, tool calls and results, turn end, session start/end) to a
spool on its own host, so a session started in any terminal can be watched
live from the Sessions page without a wrapper. This module is the worker's
side of that contract (docs/design/sessions.md, section 3.4): where the spool
lives, reading it from a cursor across its chunks, and deleting the spools of
sessions that ended long ago.

Layout: ``<state>/mirror/<agent>/<native_id>.jsonl`` is the first chunk, then
``<native_id>.1.jsonl``, ``<native_id>.2.jsonl`` and so on. The mod has no
append call, so it rewrites the newest chunk whole on each flush and starts a
new chunk past ``CHUNK_BYTES``; older chunks never change again. A chunk the
mod emptied to bound the spool is skipped. One JSON object per line, each with
a ``seq`` that rises by one per event across the chunks; the cursor a reader
holds is the last ``seq`` it has seen.

A rewrite may be caught half done, so only whole lines (ending in a newline)
count, and a line that does not parse is skipped; the cursor is a ``seq``, not
a byte offset, so a short read costs nothing but a later answer.

Stdlib only: it ships inside the worker bundle.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

CHUNK_BYTES = 256 * 1024        # the mod starts a new chunk past this
KEEP_SECS = 7 * 86400           # closed spools are deleted after this
MAX_EVENTS = 500                # events per answer
MAX_ANSWER_BYTES = 512 * 1024   # bytes of event text per answer

_AGENT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_NATIVE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_CHUNK = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_-]{0,127})(?:\.(?P<n>\d{1,6}))?\.jsonl$")


def state_dir() -> Path:
    """The worker's state directory: ``ROOK_WORKER_HOME``, else
    ``~/.rook-band-worker`` (``%USERPROFILE%\\.rook-band-worker`` on Windows)."""
    env = os.environ.get("ROOK_WORKER_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".rook-band-worker"


def mirror_root() -> Path:
    return state_dir() / "mirror"


def check(agent: str, native_id: str) -> tuple[str, str]:
    """Both names as path parts: no separators, no dots, nothing to traverse."""
    if not isinstance(agent, str) or not _AGENT.fullmatch(agent):
        raise ValueError("agent must be a short lowercase name such as claude")
    if not isinstance(native_id, str) or not _NATIVE.fullmatch(native_id):
        raise ValueError("native_id must be letters, digits, '-' or '_'")
    return agent, native_id


def chunk_name(native_id: str, index: int) -> str:
    return f"{native_id}.jsonl" if index == 0 else f"{native_id}.{index}.jsonl"


def chunks(agent: str, native_id: str, root: Path | None = None) -> list[tuple[int, Path]]:
    """The spool's chunk files, oldest first."""
    agent, native_id = check(agent, native_id)
    folder = (root or mirror_root()) / agent
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        m = _CHUNK.match(name)
        if m and m.group("id") == native_id:
            out.append((int(m.group("n") or 0), folder / name))
    out.sort()
    return out


def parse_lines(raw: bytes) -> list[dict]:
    """Events from a chunk's bytes: whole lines that parse, with an int ``seq``."""
    end = raw.rfind(b"\n")
    if end < 0:
        return []
    out = []
    for line in raw[:end].split(b"\n"):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and isinstance(ev.get("seq"), int):
            out.append(ev)
    return out


def pid_alive(pid) -> bool | None:
    """Whether a process is running: None where this platform cannot say."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def is_done(events: list[dict]) -> bool:
    """Ended: the last event is ``session.end``, or the process that last
    started it is gone without saying so (a crash, a kill)."""
    if not events:
        return False
    if events[-1].get("type") == "session.end":
        return True
    for ev in reversed(events):
        if ev.get("type") == "session.start":
            return pid_alive(ev.get("pid")) is False
    return False


class SpoolReader:
    """Reads one spool from a cursor, remembering what it learned of the
    chunks that no longer change so a long poll re-reads only the newest."""

    def __init__(self, agent: str, native_id: str, root: Path | None = None) -> None:
        self.agent, self.native_id = check(agent, native_id)
        self.root = root
        self._last_seq: dict[str, tuple[int, int, int]] = {}   # path -> (mtime_ns, size, last seq)

    def stamp(self) -> tuple:
        """Cheap change marker: the chunks and the newest one's size and mtime."""
        found = chunks(self.agent, self.native_id, self.root)
        if not found:
            return ()
        try:
            st = found[-1][1].stat()
        except OSError:
            return (len(found),)
        return (len(found), found[-1][0], st.st_size, st.st_mtime_ns)

    def read(self, cursor: int = 0, max_events: int = MAX_EVENTS) -> dict:
        found = chunks(self.agent, self.native_id, self.root)
        events: list[dict] = []
        tail: list[dict] = []       # the newest chunk's events, for done
        size = 0
        full = False
        for pos, (_n, path) in enumerate(found):
            if full:
                break
            newest = pos == len(found) - 1
            try:
                st = path.stat()
            except OSError:
                continue
            known = self._last_seq.get(str(path))
            if (not newest and known is not None and known[:2] == (st.st_mtime_ns, st.st_size)
                    and known[2] <= cursor):
                continue    # an older chunk wholly behind the cursor
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            got = parse_lines(raw)
            if got:
                self._last_seq[str(path)] = (st.st_mtime_ns, st.st_size, got[-1]["seq"])
            if newest:
                tail = got
            for ev in got:
                if ev["seq"] <= cursor:
                    continue
                size += len(json.dumps(ev))
                if events and (len(events) >= max_events or size > MAX_ANSWER_BYTES):
                    full = True
                    break
                events.append(ev)
        nxt = events[-1]["seq"] if events else cursor
        if not tail and found:
            # The newest chunk is empty or mid-rewrite: judge by what came before.
            tail = events
        done = (not full) and is_done(tail)
        return {"ok": True, "events": events, "cursor": nxt, "done": done,
                "exists": bool(found)}


def spools(agent: str | None = None, root: Path | None = None) -> list[dict]:
    """Every spool on this host: ``{agent, native_id, updated, chunks}``, newest
    first. For the session catalog (``view.mirror``)."""
    base = root or mirror_root()
    out: dict[tuple[str, str], dict] = {}
    try:
        agents = [agent] if agent else sorted(os.listdir(base))
    except OSError:
        return []
    for a in agents:
        if not _AGENT.fullmatch(a):
            continue
        try:
            names = os.listdir(base / a)
        except OSError:
            continue
        for name in names:
            m = _CHUNK.match(name)
            if not m:
                continue
            try:
                mtime = (base / a / name).stat().st_mtime
            except OSError:
                continue
            row = out.setdefault((a, m.group("id")), {"agent": a, "native_id": m.group("id"),
                                                      "updated": 0.0, "chunks": 0})
            row["updated"] = max(row["updated"], mtime)
            row["chunks"] += 1
    return sorted(out.values(), key=lambda r: -r["updated"])


def cleanup(keep_secs: float = KEEP_SECS, root: Path | None = None,
            now: float | None = None) -> list[str]:
    """Deletes the spools of closed sessions untouched for ``keep_secs``.
    A spool whose process may still run is kept, however old. Returns the
    ``agent/native_id`` keys removed."""
    now = time.time() if now is None else now
    removed = []
    for row in spools(root=root):
        if now - row["updated"] < keep_secs:
            continue
        found = chunks(row["agent"], row["native_id"], root)
        events: list[dict] = []
        for _n, path in reversed(found):
            try:
                events = parse_lines(path.read_bytes())
            except OSError:
                events = []
            if events:
                break
        ended = not events or is_done(events)
        if not ended:
            # No session.end and no way to tell the process is gone: past
            # four times the window it is a crash nobody will resume.
            if now - row["updated"] < 4 * keep_secs:
                continue
        for _n, path in found:
            try:
                path.unlink()
            except OSError:
                pass
        removed.append(f"{row['agent']}/{row['native_id']}")
    return removed
