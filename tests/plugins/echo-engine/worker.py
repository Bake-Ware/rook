"""A stand-in engine for the house agent's tests (brief HA2): echo-engine.turn takes 0.5's
agent.backend arguments and answers without any model or login.

It keeps each session's messages in WORKER.data/sessions.json, so a resumed session remembers
its first message across hub and worker restarts, as Claude Code's --resume does. With a door
token it calls the hub's MCP door as the turn (the workers tool) and says how many workers it
saw. A message holding WAIT waits until the call is stopped."""

import asyncio
import json
import urllib.request
import uuid

from rook_worker import Cap


def _door(token):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "workers", "arguments": {}}}).encode()
    request = urllib.request.Request(WORKER.hub_url.rstrip("/") + "/mcp", data=body, method="POST",  # noqa: F821
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request) as reply:
        result = json.loads(reply.read())["result"]
    return None if result["isError"] else len(json.loads(result["content"][0]["text"])["workers"])


async def turn(args):
    path = WORKER.data / "sessions.json"  # noqa: F821
    sessions = json.loads(path.read_text()) if path.exists() else {}
    session = args.get("session") or uuid.uuid4().hex
    message = args["prompt"].split("\n\n", 1)[1].split("\n\n---\n")[0]
    said = sessions.setdefault(session, [])
    said.append(message)
    path.write_text(json.dumps(sessions))
    if "WAIT" in message:
        await asyncio.sleep(3600)
    workers = await asyncio.to_thread(_door, args["door"]) if args.get("door") else None
    carried = "carried" if "\n\n---\n" in args["prompt"] else "plain"
    text = (f"echo {len(said)}: {message} | first: {said[0]} | workers: {workers} | {carried} | "
            f"system: {args.get('system', '').splitlines()[0]}")
    return {"text": text, "session": session, "usage": {"input_tokens": len(args["prompt"])}}


CAPS = {"echo-engine.turn": Cap("A stand-in engine: echoes, and remembers by session.",
                                {"type": "object", "required": ["prompt"]}, turn)}
