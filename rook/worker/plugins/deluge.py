"""deluge.* — manage a Deluge torrent client on this worker.

Loads only where Deluge is installed (running or not). Drives the client through
``deluge-console`` (no extra Python deps); the daemon must be running for the
live commands, and ``deluge.status`` reports whether it is.

    deluge.status()                 -> daemon running? + session summary
    deluge.list()                   -> current torrents (parsed + raw)
    deluge.files(torrent_id)        -> a torrent's save path + file list
                                       (pull them over the band with file.read)
    deluge.add(torrent)             -> add a magnet / .torrent URL / path
    deluge.pause(torrent_id="*")    -> pause one (or all)
    deluge.resume(torrent_id="*")   -> resume one (or all)
    deluge.remove(torrent_id, data=False) -> remove (optionally delete data)
"""

from __future__ import annotations

import re
import shlex

from ...core.pluginkit import RunTimeout, fail, require_str, run, which
from ..plugin import Plugin, capability


def _which_console() -> str | None:
    return which("deluge-console")


async def _run(*argv: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """``run``, with a timeout reported like a failed command (exit 124) so
    the console callers keep one code path."""
    try:
        return tuple(await run(argv, timeout))
    except RunTimeout as e:
        return 124, "", str(e)


# deluge-console exits 0 even when it fails to reach the daemon, so detect the
# failure from its output instead of the exit code.
_CONN_ERRORS = ("could not connect", "password does not match",
                "connection refused", "not connected", "failed to connect")


def _conn_failed(*texts: str) -> str | None:
    blob = " ".join(texts).lower()
    for m in _CONN_ERRORS:
        if m in blob:
            return m
    return None


async def _console(command: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a single deluge-console command against the local daemon."""
    exe = _which_console()
    if not exe:
        fail("deluge-console not installed")
    return await _run(exe, command, timeout=timeout)


# deluge-console takes the whole command as one argument and parses it
# itself: it splits on every ';' first (quotes do not protect one), then
# shlex-splits each piece. So a ';' or a newline in an identifier could
# smuggle in a second console command, quotes and backslashes could unbalance
# the quoting, and a leading '-' would be read as an option (rm --remove_data).
# deluge matches the identifier against torrent ids and names (prefix/suffix
# with '*'), so names are allowed: rejected are only those characters, and
# the rest is shlex-quoted as one word. A space-separated list of hex ids is
# still several ids, as before.
_HEX_IDS = re.compile(r"[0-9a-fA-F]{1,40}(?: [0-9a-fA-F]{1,40})*")
_BREAKOUT = re.compile(r"[;\'\"\\\x00-\x1f\x7f]")


def _torrent_id(value) -> str:
    """The identifier as console command text: ``*``, hex ids, or one
    quoted name."""
    tid = require_str(value, "torrent_id").strip()
    if tid == "*" or _HEX_IDS.fullmatch(tid):
        return tid
    if _BREAKOUT.search(tid):
        fail("torrent_id must not contain ; quotes, backslashes or control characters")
    if tid.startswith("-"):
        fail("torrent_id must not start with -")
    return shlex.quote(tid)


async def _simple(command: str) -> dict:
    """A console command whose only result is success and its output."""
    code, out, err = await _console(command)
    output = (out or err).strip()[:300]
    if code != 0 or _conn_failed(out, err):
        fail(output or f"deluge-console exit {code}")
    return {"ok": True, "output": output}


# `deluge-console info` state letters.
_STATES = {"S": "Seeding", "D": "Downloading", "P": "Paused", "Q": "Queued",
           "C": "Checking", "E": "Error", "U": "Allocating", "M": "Moving"}


def _parse_info(out: str) -> list[dict]:
    """Parse `deluge-console info` (compact form)::

        [S]   100% <name> <40-char hash>
            DL: 4.5 G (0 B) UL: 71.6 M (0 B) ETA: -
    """
    torrents: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        m = re.match(r"^\[(.)\]\s+([\d.]+)%\s+(.*?)\s+([0-9a-fA-F]{40})\s*$", line)
        if m:
            if cur:
                torrents.append(cur)
            cur = {"state": _STATES.get(m.group(1), m.group(1)),
                   "progress": float(m.group(2)),
                   "name": m.group(3).strip(), "id": m.group(4)}
            continue
        d = re.search(r"DL:\s*(.+?)\s+UL:\s*(.+?)\s+ETA:\s*(.+?)\s*$", line)
        if d and cur is not None:
            cur["downloaded"], cur["uploaded"], cur["eta"] = (
                d.group(1).strip(), d.group(2).strip(), d.group(3).strip())
    if cur:
        torrents.append(cur)
    return torrents


class DelugePlugin(Plugin):
    NAMESPACE = "deluge"

    def available(self) -> bool:
        # Installed is enough (running or not); live commands report if the
        # daemon is down. deluged alone (headless box) also counts.
        return bool(which("deluge-console", "deluged", "deluge"))

    @capability("status", risk="read", scope="local")
    async def _status(self) -> dict:
        """Whether the daemon is running, plus a session summary if reachable."""
        running = False
        try:
            code, out, _ = await _run("pgrep", "-x", "deluged", timeout=8)
            running = code == 0 and bool(out.strip())
        except Exception:
            pass
        summary = None
        reachable = False
        if _which_console():
            code, out, err = await _console("info", timeout=15)
            unreachable = _conn_failed(out, err)
            if code == 0 and not unreachable:
                reachable = True
                summary = f"{len(_parse_info(out))} torrents"
            else:
                summary = f"daemon not reachable ({unreachable})" if unreachable else (err or out).strip()[:200]
        return {"ok": True, "daemon_running": running, "reachable": reachable,
                "summary": summary, "console": bool(_which_console())}

    @capability("list", risk="read", scope="local")
    async def _list(self) -> dict:
        """Current torrents: name, state, progress, size, ratio (+ raw output)."""
        code, out, err = await _console("info")
        unreachable = _conn_failed(out, err)
        if code != 0 or unreachable:
            fail(f"cannot reach deluge daemon ({unreachable})" if unreachable
                 else (err or out).strip()[:300] or "deluge-console failed (is deluged running?)")
        torrents = _parse_info(out)
        return {"ok": True, "count": len(torrents), "torrents": torrents,
                "raw": out[:8000]}

    @capability("files", risk="read", scope="local")
    async def _files(self, torrent_id: str) -> dict:
        """A torrent's save path + files — pull them over the band with file.read.

        ``torrent_id`` is a hash (or prefix) or the torrent's name."""
        tid = _torrent_id(torrent_id)
        code, out, err = await _console(f"info -v {tid}")
        if code != 0:
            fail((err or out).strip()[:300] or f"deluge-console exit {code}")
        save_path = None
        files = []
        for line in out.splitlines():
            m = re.match(r"\s*Download Folder:\s*(.+)", line)
            if m:
                save_path = m.group(1).strip()
            fm = re.match(r"\s*([^\(]+)\s*\(([\d.]+\s*\w+)\)\s*$", line)
            if "::Files" in line or (fm and save_path):
                pass  # deluge-console file lines vary; keep raw as the source of truth
        return {"ok": True, "torrent_id": torrent_id.strip(), "save_path": save_path,
                "raw": out[:8000],
                "note": "read files with file.read(save_path + '/' + name, encoding='base64')"}

    @capability("add", risk="write", scope="local")
    async def _add(self, torrent: str) -> dict:
        """Add a torrent by magnet link, .torrent URL, or local path."""
        torrent = require_str(torrent, "torrent (magnet/url/path)")
        # Quotes do not protect a ';' from the console's own split (see
        # _torrent_id); single-quoting keeps a Windows path's backslashes.
        if re.search(r"[;\x00-\x1f\x7f]", torrent):
            fail("torrent must not contain ; or control characters")
        code, out, err = await _console(f"add {shlex.quote(torrent)}", timeout=45)
        output = (out or err).strip()[:500]
        if code != 0 or "error" in out.lower():
            fail(output or f"deluge-console exit {code}")
        return {"ok": True, "output": output}

    @capability("pause", risk="write", scope="local")
    async def _pause(self, torrent_id: str = "*") -> dict:
        """Pause a torrent by id or name, or all with ``*``."""
        return await _simple(f"pause {_torrent_id(torrent_id)}")

    @capability("resume", risk="write", scope="local")
    async def _resume(self, torrent_id: str = "*") -> dict:
        """Resume a torrent by id or name, or all with ``*``."""
        return await _simple(f"resume {_torrent_id(torrent_id)}")

    @capability("remove", risk="write", tags=("destructive",), scope="local")
    async def _remove(self, torrent_id: str, data: bool = False) -> dict:
        """Remove a torrent by id or name. ``data=True`` also deletes the downloaded files."""
        torrent_id = _torrent_id(torrent_id)
        cmd = f"rm {'--remove_data ' if data else ''}{torrent_id}"
        return {**await _simple(cmd), "removed_data": bool(data)}


PLUGIN = DelugePlugin
