"""A stand-in engine for the memory tests (brief MEM5): memory-engine.turn takes the arguments of
agents-claude-code.turn, memory's fork, schema and precompact among them, and answers without a model.

It works in the folder its worker config names (`config.set {worker, set: {"memory-engine": {"dir": ...}}}`):
- every call it gets is added to calls.jsonl there (the door token left out), so a test reads what
  recall handed each turn;
- a call with a schema (a dream, or the dream's same check) answers the first answer queued in
  dreams.json (or same.json for the same check), else nothing found;
- in a turn's message, <<cap {json}>> calls that hub cap through the door with the turn's token, and
  <<reply: text>> is the reply (else "ok"); COMPACT does what Claude Code's hook before compaction
  does: calls the precompact cap through the door and waits for it."""

import json
import re
import urllib.request
import uuid
from pathlib import Path

from rook_worker import Cap


def _door(token, cap, args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "call", "arguments": {
        "worker": "hub", "cap": cap, "args": args, "why": "The memory tests' stand-in engine."}}}).encode()
    request = urllib.request.Request(WORKER.hub_url.rstrip("/") + "/mcp", data=body, method="POST",  # noqa: F821
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request) as reply:
        result = json.loads(reply.read())["result"]
    text = result["content"][0]["text"]
    return {"error": text} if result["isError"] else json.loads(text)


def _queued(folder, name):
    path = folder / name
    queue = json.loads(path.read_text()) if path.exists() else []
    if not queue:
        return None
    path.write_text(json.dumps(queue[1:]))
    return queue[0]


async def turn(args):
    import asyncio
    folder = Path(WORKER.config["dir"])  # noqa: F821
    with open(folder / "calls.jsonl", "a") as log:
        log.write(json.dumps({k: v for k, v in args.items() if k != "door"}) + "\n")
    session = args.get("session") if args.get("session") and not args.get("fork") else uuid.uuid4().hex
    if args.get("schema") is not None:
        same = "verdicts" in args["schema"].get("properties", {})
        got = _queued(folder, "same.json" if same else "dreams.json")
        if got == "FAIL":
            raise RuntimeError("the stand-in was told to fail")
        return {"text": "", "session": session, "structured": got or ({"verdicts": []} if same else {"facts": []})}
    message = args["prompt"].split("\n\n", 1)[1].split("\n\n---\n")[0].split("\n\n## Memory")[0]
    said = []
    for cap, raw in re.findall(r"<<([a-z][a-z.]*) (\{.*?\})>>", message):
        said.append(await asyncio.to_thread(_door, args["door"], cap, json.loads(raw)))
    if "COMPACT" in message and args.get("precompact"):
        hook = args["precompact"]
        said.append(await asyncio.to_thread(_door, args["door"], hook["cap"], hook.get("args") or {}))
    reply = re.search(r"<<reply: (.*?)>>", message)
    text = reply.group(1) if reply else "ok"
    with open(folder / "said.jsonl", "a") as log:
        log.write(json.dumps(said) + "\n")
    return {"text": text, "session": session}


CAPS = {"memory-engine.turn": Cap("A stand-in engine for the memory tests.", {"type": "object", "required": ["prompt"]},
                                  turn)}
