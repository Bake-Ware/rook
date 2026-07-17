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

import asyncio
import re
import shutil

from ..plugin import Plugin, capability


def _which_console() -> str | None:
    return shutil.which("deluge-console")


async def _run(*argv: str, timeout: float = 30.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


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
        return 127, "", "deluge-console not installed"
    return await _run(exe, command, timeout=timeout)


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
        return bool(_which_console() or shutil.which("deluged") or shutil.which("deluge"))

    @capability("status")
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
            fail = _conn_failed(out, err)
            if code == 0 and not fail:
                reachable = True
                summary = f"{len(_parse_info(out))} torrents"
            else:
                summary = f"daemon not reachable ({fail})" if fail else (err or out).strip()[:200]
        return {"ok": True, "daemon_running": running, "reachable": reachable,
                "summary": summary, "console": bool(_which_console())}

    @capability("list")
    async def _list(self) -> dict:
        """Current torrents: name, state, progress, size, ratio (+ raw output)."""
        code, out, err = await _console("info")
        fail = _conn_failed(out, err)
        if code != 0 or fail:
            return {"ok": False, "error": (f"cannot reach deluge daemon ({fail})" if fail
                    else (err or out).strip()[:300] or "deluge-console failed (is deluged running?)")}
        torrents = _parse_info(out)
        return {"ok": True, "count": len(torrents), "torrents": torrents,
                "raw": out[:8000]}

    @capability("files")
    async def _files(self, torrent_id: str) -> dict:
        """A torrent's save path + files — pull them over the band with file.read."""
        if not torrent_id:
            return {"ok": False, "error": "torrent_id required"}
        code, out, err = await _console(f"info -v {torrent_id}")
        if code != 0:
            return {"ok": False, "error": (err or out).strip()[:300]}
        save_path = None
        files = []
        for line in out.splitlines():
            m = re.match(r"\s*Download Folder:\s*(.+)", line)
            if m:
                save_path = m.group(1).strip()
            fm = re.match(r"\s*([^\(]+)\s*\(([\d.]+\s*\w+)\)\s*$", line)
            if "::Files" in line or (fm and save_path):
                pass  # deluge-console file lines vary; keep raw as the source of truth
        return {"ok": True, "torrent_id": torrent_id, "save_path": save_path,
                "raw": out[:8000],
                "note": "read files with file.read(save_path + '/' + name, encoding='base64')"}

    @capability("add")
    async def _add(self, torrent: str) -> dict:
        """Add a torrent by magnet link, .torrent URL, or local path."""
        torrent = str(torrent or "").strip()
        if not torrent:
            return {"ok": False, "error": "torrent (magnet/url/path) required"}
        code, out, err = await _console(f'add "{torrent}"', timeout=45)
        return {"ok": code == 0 and "error" not in out.lower(),
                "output": (out or err).strip()[:500]}

    @capability("pause")
    async def _pause(self, torrent_id: str = "*") -> dict:
        """Pause a torrent by id, or all with ``*``."""
        code, out, err = await _console(f"pause {torrent_id}")
        return {"ok": code == 0, "output": (out or err).strip()[:300]}

    @capability("resume")
    async def _resume(self, torrent_id: str = "*") -> dict:
        """Resume a torrent by id, or all with ``*``."""
        code, out, err = await _console(f"resume {torrent_id}")
        return {"ok": code == 0, "output": (out or err).strip()[:300]}

    @capability("remove")
    async def _remove(self, torrent_id: str, data: bool = False) -> dict:
        """Remove a torrent. ``data=True`` also deletes the downloaded files."""
        if not torrent_id:
            return {"ok": False, "error": "torrent_id required"}
        cmd = f"rm {'--remove_data ' if data else ''}{torrent_id}"
        code, out, err = await _console(cmd)
        return {"ok": code == 0, "output": (out or err).strip()[:300],
                "removed_data": bool(data)}


PLUGIN = DelugePlugin
