"""The memory tools, served to a `claude -p` turn over MCP (stdio, one JSON message per line).

Role "turn" (a live turn): search, pin, unpin. What a search returns counts as use when the turn
is with Bake (it moves the memories' last-used clock). Role "dream": search only, and nothing it
reads counts as use. The process is started per call with the session, the incoming message and
the conversation's time on its command line, so pin ties to the message he just sent.
"""

import json
import sys

from . import pin as pins
from . import recall


def serve(store, knobs, role, session=None, entry=None, at=None, stdin=sys.stdin, stdout=sys.stdout):
    tools = ["search"] + (["pin", "unpin"] if role == "turn" else [])
    person = knobs["people"]["person"]
    exclude = []
    if role == "dream" and session:
        exclude = [r["id"] for r in store.undreamed(session)]
    elif entry:
        exclude = [entry]
    who = store.one("SELECT speaker FROM entries WHERE id = ?", entry)["speaker"] if entry else None

    def call(name, args):
        if name == "search":
            r = recall.recall(store, knobs, str(args.get("query") or ""), at, exclude_entries=exclude)
            if role == "turn" and who == person:
                store.use(at, [(i.kind, i.id) for i in r.items()], session, entry, how="search")
            return recall.for_turn(r)
        if name == "pin":
            return pins.pin(store, knobs, args.get("statement"), entry, args.get("belief"), args.get("about") or "world")[1]
        if name == "unpin":
            return pins.unpin(store, knobs, entry, args.get("belief"))[1]
        raise KeyError(name)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method, params = msg.get("method"), msg.get("params") or {}
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "memory", "version": "0.2"}}
            elif method == "tools/list":
                result = {"tools": [{"name": t, **pins.TOOLS[t]} for t in tools]}
            elif method == "tools/call":
                name = params.get("name")
                if name not in tools:
                    raise KeyError(name)
                text = call(name, params.get("arguments") or {})
                store.log_tool(role, session, entry, name, params.get("arguments") or {}, text)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            elif method == "ping":
                result = {}
            else:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                         "error": {"code": -32601, "message": f"no method {method}"}}) + "\n")
                stdout.flush()
                continue
        except Exception as e:  # the agent sees the failure as the tool's answer
            result = {"content": [{"type": "text", "text": f"memory tool failed: {e!r}"}], "isError": True}
        stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        stdout.flush()


def config(store, knobs, role, session=None, entry=None, at=None):
    """The --mcp-config JSON that starts this server for one call."""
    import pathlib
    here = pathlib.Path(__file__).resolve().parent.parent
    args = ["-m", "rook_memory", "--store", str(store.folder), "--knobs", knobs["_path"], "serve", "--role", role]
    if session:
        args += ["--session", str(session)]
    if entry:
        args += ["--entry", str(entry)]
    if at:
        args += ["--at", at]
    return json.dumps({"mcpServers": {"memory": {"command": sys.executable, "args": args,
                                                 "env": {"PYTHONPATH": str(here)}}}})
