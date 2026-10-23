"""Manual end-to-end check of the session host with the REAL claude (never run by tests).

    python tools/agents/smoke.py [--turns 5] [--model haiku] [--side]

Starts a session host in-process (no hub link) with one temp role under /tmp:
a main lane and a voice lane (Haiku, effort low, thinking off, partial
deltas), connects to the voice server over a websocket like the voice server
on kaiju does, and measures the time to the first text delta of each voice
turn. ``--side`` also asks a by-the-way question and runs a fork off the main
lane. Afterwards the temp dir and its Claude Code transcripts
(``~/.claude/projects/-tmp-rook-agents-smoke-...``) are deleted.

Uses this host's Claude Code login; costs a few cents of Haiku.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import aiohttp  # noqa: E402

from rook.agents.config import Config  # noqa: E402
from rook.agents.host import Host  # noqa: E402

PROMPTS = ["Hi, who are you?", "What is two plus three?", "Name a colour.",
           "Say something short about the weather.", "Thanks, that's all."]


def munge(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


async def wait_kind(events, kind, timeout=120, **match):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for e in events:
            if e.get("kind") == kind and all(e.get(k) == v for k, v in match.items()):
                return e
        await asyncio.sleep(0.05)
    raise TimeoutError(f"no {kind} {match}")


async def run(args, tmp: Path) -> dict:
    token = "smoke-" + os.urandom(8).hex()
    cfg = Config.from_dict({"roles_dir": str(tmp / "roles"), "voice_listen": "127.0.0.1:0",
                            "host": "smoke"})
    host = Host(cfg)
    events: list[dict] = []
    host.listeners.append(events.append)
    await host.start()
    persona = "You are a terse test assistant. Answer in one short sentence."
    files = {"CLAUDE.md": persona + "\n",
             ".claude/settings.json": json.dumps({"permissions": {"defaultMode": "dontAsk"}}),
             ".mcp.json": json.dumps({"mcpServers": {}}),
             "voice/CLAUDE.md": "You are the voice. Speak in one short sentence.\n",
             "voice/.claude/settings.json": json.dumps({"permissions": {}}),
             "voice/.mcp.json": json.dumps({"mcpServers": {}})}
    lanes = {"main": {"model": args.model, "effort": "low", "tools": [], "thinking": False},
             "voice": {"model": args.model, "effort": "low", "tools": [], "partial": True,
                       "thinking": False}}
    out: dict = {}
    try:
        await host.handle({"t": "configure", "roles": {"smoke": {
            "dir_files": files, "lanes": lanes,
            "voice_token_sha256": hashlib.sha256(token.encode()).hexdigest()}}})
        t0 = time.monotonic()
        await host.handle({"t": "send", "role": "smoke", "lane": "main", "item": "m1",
                           "text": "Reply with the single word: ready."})
        r = await wait_kind(events, "result", item="m1")
        out["main_first_turn"] = {"ok": r["ok"], "text": r["text"][:80],
                                  "s": round(time.monotonic() - t0, 2),
                                  "context_tokens": r["context_tokens"]}
        url = f"ws://127.0.0.1:{host.voice_server.port}/voice/smoke"
        firsts, totals, thinking = [], [], []
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, headers={"Authorization": f"Bearer {token}"}) as ws:
                ready = json.loads((await ws.receive(timeout=10)).data)
                assert ready["t"] == "ready", ready
                for i in range(args.turns):
                    tid = f"v{i}"
                    await ws.send_json({"t": "user", "turn": tid,
                                        "text": PROMPTS[i % len(PROMPTS)],
                                        "identity": {"owner": True, "name": "Smoke"}})
                    while True:
                        msg = json.loads((await ws.receive(timeout=120)).data)
                        if msg["t"] == "end" and msg["turn"] == tid:
                            break
                        if msg["t"] == "error":
                            raise RuntimeError(msg)
                    vr = await wait_kind(events, "voice_reply", turn=tid)
                    firsts.append(msg["first_ms"])
                    totals.append(msg["total_ms"])
                    thinking.append(vr["thinking_tokens"])
                    print(f"  voice turn {i}: first {msg['first_ms']} ms, total "
                          f"{msg['total_ms']} ms, thinking {vr['thinking_tokens']}: "
                          f"{msg['text'][:60]!r}", flush=True)
        valid = [f for f in firsts if f is not None]
        out["voice"] = {"turns": len(firsts), "first_ms": firsts, "total_ms": totals,
                        "first_ms_median": statistics.median(valid) if valid else None,
                        "warm_first_ms_median": (statistics.median(valid[1:])
                                                 if len(valid) > 1 else None),
                        "thinking_tokens": thinking}
        if args.side:
            await host.handle({"t": "btw", "req": "b1", "role": "smoke",
                               "question": "What word did you reply with first?"})
            b = await wait_kind(events, "btw", req="b1")
            out["btw"] = {"ok": b["ok"], "ms": b["ms"], "text": (b.get("text") or
                                                                b.get("error", ""))[:120]}
            await host.handle({"t": "fork", "fork": "f1", "role": "smoke", "text":
                               "In one sentence: what was the first thing I asked you?"})
            f = await wait_kind(events, "fork", fork="f1", state="done", timeout=180)
            out["fork"] = {"ms": f["duration_ms"], "text": f["text"][:120],
                           "started": bool([e for e in events if e.get("kind") == "fork"
                                            and e.get("state") == "started"])}
        out["sessions"] = [e for e in events if e.get("kind") == "session"]
        out["errors"] = [e for e in events if e.get("kind") == "state"
                         and e.get("state") == "crashed"]
    finally:
        await host.close()
    # A new host on the same role dir resumes the main session by id.
    host = Host(cfg)
    events2: list[dict] = []
    host.listeners.append(events2.append)
    try:
        role = {"dir_files": files, "lanes": {"main": lanes["main"]}}
        await host.handle({"t": "configure", "roles": {"smoke": role}})
        await host.handle({"t": "send", "role": "smoke", "lane": "main", "item": "m2",
                           "text": "What single word did you reply with earlier?"})
        r = await wait_kind(events2, "result", item="m2")
        sess = await wait_kind(events2, "session")
        first = next((e for e in out["sessions"] if e["lane"] == "main"), {})
        out["resume"] = {"resumed": sess["resumed"],
                         "same_id": sess["session_id"] == first.get("session_id"),
                         "text": r["text"][:80]}
    finally:
        await host.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=5)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--side", action="store_true", help="also a btw question and a fork")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="rook-agents-smoke-", dir="/tmp"))
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    try:
        out = asyncio.run(run(args, tmp))
        print(json.dumps(out, indent=2))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        prefix = munge(str(tmp))
        if projects.is_dir():
            for d in projects.iterdir():
                if d.name.startswith(prefix):
                    shutil.rmtree(d, ignore_errors=True)
                    print(f"removed transcripts {d}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
