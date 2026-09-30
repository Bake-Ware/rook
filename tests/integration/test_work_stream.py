"""Live terminal streaming end to end: worker PTY -> band -> hub (opt-in).

Covers the two ways a terminal is consumed: an agent over MCP with
``rook_call work.stream.*`` long-polls, and the hub's TermHub fan-out over a
real band connection (the path the web terminal socket uses).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from pathlib import Path

import pytest

from rook.worker import termwire



def read_env_file(path) -> dict:
    out = {}
    for line in Path(path).read_text().splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and not k.startswith("#"):
            out[k] = v
    return out

# Enough output that replies are compressed and span many ~1 KB band fragments.
LINES = 20000
PRODUCER = f"i=0; while [ $i -lt {LINES} ]; do printf 'row %d %030d\\n' $i $i; i=$((i+1)); done; echo END-$((40+2))"


def expected_digest() -> str:
    body = "".join(f"row {i} {i:030d}\r\n" for i in range(LINES))
    return hashlib.sha256(body.encode()).hexdigest()


def rows_digest(text: str) -> str:
    # Rows can share a line with prompt/shell-integration escapes; match exactly.
    rows = re.findall(r"row \d+ \d{30}\r\n", text)
    return hashlib.sha256("".join(rows).encode()).hexdigest()


def test_stream_over_mcp_long_poll(hub):
    worker = hub.workers[0]

    async def go(s):
        opened = await hub.acall(s, "rook_call", cap="work.stream.open", worker=worker,
                                 args={"harness": "shell", "title": "integration terminal",
                                       "buffer_bytes": 1048576})
        assert opened["ok"] and opened["result"]["ok"], opened
        tid = opened["result"]["id"]
        try:
            wrote = await hub.acall(s, "rook_call", cap="work.stream.write", worker=worker,
                                    args={"id": tid, "data": "exec /bin/sh\r"})
            assert wrote["ok"], wrote
            wrote = await hub.acall(s, "rook_call", cap="work.stream.write", worker=worker,
                                    args={"id": tid, "data": "stty -echo; " + PRODUCER + "\r"})
            assert wrote["ok"], wrote
            out, cursor, encs = b"", 0, set()
            for _ in range(400):
                r = await hub.acall(s, "rook_call", cap="work.stream.read", worker=worker,
                                    args={"id": tid, "cursor": cursor, "wait": 5, "max_bytes": 32768})
                assert r["ok"], r
                res = r["result"]
                assert res["cursor"] == cursor and res["dropped"] == 0
                out += termwire.decode(res["enc"], res["data"])
                encs.add(res["enc"])
                cursor = res["next"]
                if b"END-42" in out:
                    break
            listed = await hub.acall(s, "rook_call", cap="work.sessions", worker=worker,
                                     args={"history": False})
            return out, encs, listed
        finally:
            await hub.acall(s, "rook_call", cap="work.stream.close", worker=worker, args={"id": tid})

    out, encs, listed = hub.run(go, timeout=240)
    assert b"END-42" in out
    assert rows_digest(out.decode()) == expected_digest()
    assert "z" in encs     # bulk output travelled compressed
    assert listed["ok"] and any(t["title"] == "integration terminal" for t in listed["result"]["live"])


def test_term_hub_fan_out_over_the_band(hub):
    """TermHub on its own band connection follows a worker terminal with two
    viewers, delivers input from the holder only, and sees the exit."""
    data_dir = hub.env.get("ROOK_IT_DATA_DIR")
    relay = hub.env.get("ROOK_IT_RELAY")
    if not data_dir or not relay or not (Path(data_dir) / "secrets.env").exists():
        pytest.skip("needs a test hub started by scripts/test-hub.sh (secrets.env + relay)")
    psk = read_env_file(Path(data_dir) / "secrets.env")["ROOK_BAND_PSK"]
    host, _, port = relay.rpartition(":")
    name = hub.workers[-1]

    async def go():
        from rook.band_mcp.client import BandClient
        from rook.remote.term_hub import TermHub, Viewer
        band = BandClient(psk=psk, hub_host=host, hub_port=int(port))
        await band.start()
        ended = []
        th = TermHub(lambda: band, on_end=ended.append, identity="integration:term-hub")
        try:
            async with asyncio.timeout(60):
                while not any(w.get("name") == name for w in band.workers.values()):
                    await asyncio.sleep(0.5)
            wid = next(w["worker_id"] for w in band.workers.values() if w.get("name") == name)
            opened = await th.call(wid, "work.stream.open", {"harness": "shell", "title": "fan-out"})
            stream = th.stream(wid, opened["id"])
            a, b = Viewer("a"), Viewer("b")
            stream.attach(a)
            stream.attach(b)
            marker = uuid.uuid4().hex[:8]
            stream.control(a, {"op": "input", "data": f"exec /bin/sh\rstty -echo; {PRODUCER}; echo {marker}-$((1+1)); exit\r"})
            with pytest.raises(ValueError):
                stream.control(b, {"op": "input", "data": "echo intruder\r"})

            async def collect(v):
                buf = b""
                async with asyncio.timeout(180):
                    while f"{marker}-2".encode() not in buf:
                        kind, item = await v.next()
                        if kind == "frame":
                            buf += item[8:]
                        elif kind == "resync":
                            raise AssertionError("viewer fell behind while draining promptly")
                return buf
            outs = dict(zip((a.id, b.id), await asyncio.gather(collect(a), collect(b))))
            async with asyncio.timeout(30):
                while not ended:
                    await asyncio.sleep(0.2)
            return list(outs.values()), stream.exit_code, stream.end, th.memory()
        finally:
            await th.stop()
            await band.stop()

    outs, code, end, mem = asyncio.run(go())
    for out in outs:
        assert rows_digest(out.decode()) == expected_digest()
    assert code == 0 and end > LINES * 30
    assert mem <= 256 * 1024 + 64 * 1024   # bounded replay ring, drained viewers
