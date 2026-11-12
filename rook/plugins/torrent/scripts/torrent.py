"""torrent: deluge-console on the worker the call is for, its output read here.

deluge-console takes the whole command as ONE argument and parses it again
(it splits on ';' first, quotes don't protect one, then shlex-splits each
piece), so argv-slot escaping doesn't protect it: the manifest's arg patterns
are the injection guard. It also exits 0 when it can't reach the daemon, so
that output is turned into an error here."""

import re

DOWN = re.compile(r"(?im)^\s*(failed to connect|could not connect|connection refused|not connected|"
                  r"password does not match)")
VERSION = re.compile(r"(?m)^deluge-console (?P<deluge>\S+)(?:[\s\S]*?^libtorrent: (?P<libtorrent>\S+))?")
# Deluge prints these lines in this order (console/cmdline/commands/status.py);
# the per-state counts are missing when it can't list the torrents.
STATUS = re.compile(
    r"(?m)^Total upload: (?P<upload>.+?)\r?\nTotal download: (?P<download>.+?)\r?\nDHT Nodes: (?P<dht_nodes>\d+)"
    r"(?:\r?\nTotal torrents: (?P<torrents>\d+)(?:\r?\n Allocating: \d+\r?\n Checking: (?P<checking>\d+)"
    r"\r?\n Downloading: (?P<downloading>\d+)\r?\n Seeding: (?P<seeding>\d+)\r?\n Paused: (?P<paused>\d+)"
    r"\r?\n Error: (?P<error>\d+)\r?\n Queued: (?P<queued>\d+))?)?")
ITEM = re.compile(r"^\[(?P<state>.)\]\s+(?P<progress>[\d.]+)%\s+(?P<name>.*?)\s+(?P<id>[0-9a-fA-F]{40})\s*$")


async def _console(ctx, command, daemon=True):
    argv = [ctx.settings["console"], *command]
    out = await ctx.call_on(ctx.worker["id"], "shell.exec", argv=argv, env={"LANG": "C"})
    if out["code"] != 0:
        tail = out["stderr"].strip()[-2000:]
        raise ctx.Error(f"{argv[0]} exited {out['code']}" + (f": {tail}" if tail else ""))
    text = out["stdout"]
    m = DOWN.search(text) if daemon else None
    if m:
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        raise ctx.Error("failed: " + text[start:end if end >= 0 else None].strip()[:500])
    return text


def _ints(found, *names):
    return {k: int(v) if k in names and v is not None else v for k, v in found.items()}


async def version(ctx):
    m = VERSION.search(await _console(ctx, ["--version"], daemon=False))
    return m.groupdict() if m else None


async def status(ctx):
    m = STATUS.search(await _console(ctx, ["status"]))
    return _ints(m.groupdict(), "dht_nodes", "torrents", "checking", "downloading", "seeding", "paused",
                 "error", "queued") if m else None


async def list_(ctx):
    text = await _console(ctx, ["info"])
    return [{**m.groupdict(), "progress": float(m["progress"])} for m in map(ITEM.search, text.splitlines()) if m]


async def _text(ctx, command):
    return (await _console(ctx, [command])).rstrip("\r\n")


async def get(ctx, torrent_id):
    return await _text(ctx, f"info -v {torrent_id}")


async def add(ctx, torrent):
    return await _text(ctx, f"add {torrent}")


async def pause(ctx, torrent_id="*"):
    return await _text(ctx, f"pause {torrent_id}")


async def resume(ctx, torrent_id="*"):
    return await _text(ctx, f"resume {torrent_id}")


async def remove(ctx, torrent_id):
    return await _text(ctx, f"rm {torrent_id}")


async def delete(ctx, torrent_id):
    return await _text(ctx, f"rm --remove_data {torrent_id}")
