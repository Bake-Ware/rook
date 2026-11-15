"""Remote update: the newest build the hub holds for a remote's board, sent its way."""

from __future__ import annotations

import asyncio
import hashlib
import re

BOARD = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
VERSION = re.compile(r"\d+(\.\d+){0,3}")


def _key(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def newest(folder, board: str):
    """(version, path) of the highest-numbered rook-remote-<board>-<version>.bin in folder, or None."""
    best = None
    head = f"rook-remote-{board}-"
    try:
        names = [p.name for p in folder.iterdir()]
    except OSError:
        return None
    for name in names:
        if not (name.startswith(head) and name.endswith(".bin")):
            continue
        v = name[len(head):-4]
        path = folder / name
        if VERSION.fullmatch(v) and path.is_file() and not path.is_symlink() and (best is None or _key(v) > _key(best[0])):
            best = (v, path)
    return best


async def _say(ctx, remote: str, text: str) -> None:
    try:
        await asyncio.wait_for(ctx.call_on(remote, "remote.display", text=text, seconds=5), 6)
    except (asyncio.TimeoutError, ctx.Error):
        pass


async def check(ctx, event):
    p = (event or {}).get("payload") or {}
    remote, board, runs = p.get("worker"), str(p.get("board") or ""), str(p.get("version") or "")
    if not remote or not BOARD.fullmatch(board) or not VERSION.fullmatch(runs):
        raise ctx.Error("the remote.update.check event needs worker, board and version")
    out = {"remote": remote, "board": board, "runs": runs, "newest": None, "update": False}
    got = newest(ctx.data.parent.parent / "firmware", board)      # <data>/firmware: what GET /firmware/... serves
    if got is None:
        await _say(ctx, remote, "No builds on the hub")
        return out
    out["newest"] = got[0]
    if _key(got[0]) <= _key(runs):
        await _say(ctx, remote, f"Up to date\n{runs}")
        return out
    base = (ctx.settings["firmware_url"] or "").strip().rstrip("/")
    if not base:
        await _say(ctx, remote, f"{got[0]} is there:\nset firmware_url")
        raise ctx.Error("remote-update needs its firmware_url setting: the hub's address as the remote reaches it")
    sha = hashlib.sha256(await asyncio.to_thread(got[1].read_bytes)).hexdigest()
    await asyncio.wait_for(ctx.call_on(remote, "remote.update", url=f"{base}/firmware/{got[1].name}", sha256=sha), 20)
    out["update"] = True
    return out
