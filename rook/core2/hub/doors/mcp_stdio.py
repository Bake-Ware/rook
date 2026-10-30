"""stdio to the hub's MCP door, for agents that launch MCP servers as a
subprocess (Codex, Hermes, local harnesses):

    ROOK_TOKEN=<token> python -m rook.core2.hub.doors.mcp_stdio [--url http://127.0.0.1:8741/mcp]

One JSON-RPC message per line on stdin goes to the door as a POST; a reply
comes back as one line on stdout. Requests run side by side, so a long call
doesn't hold up a ping. The token comes from ``ROOK_TOKEN`` or
``--token-file``, never the command line (other users can read that).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WAIT = 960.0     # past the door's own 900 s


def main(argv=None, stdin=None, stdout=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rook.core2.hub.doors.mcp_stdio",
                                 description="Rook MCP door over stdio")
    ap.add_argument("--url", default="http://127.0.0.1:8741/mcp", help="the hub's MCP door")
    ap.add_argument("--token-file", help="a file holding the door token (else $ROOK_TOKEN)")
    args = ap.parse_args(argv)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        token = Path(args.token_file).read_text(encoding="utf-8").strip() if args.token_file \
            else os.environ.get("ROOK_TOKEN", "").strip()
    except OSError as e:
        print(f"mcp_stdio: {e}", file=sys.stderr)
        return 2
    if not token:
        print("mcp_stdio: set ROOK_TOKEN or pass --token-file", file=sys.stderr)
        return 2
    lock = threading.Lock()

    def write(obj: dict) -> None:
        with lock:
            stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
            stdout.flush()

    def one(line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return write(_err(None, -32700, "parse error"))
        mid = msg.get("id") if isinstance(msg, dict) and "method" in msg else None
        req = urllib.request.Request(args.url, data=line.encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=WAIT) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body, problem = e.read(), f"door: HTTP {e.code}"
            try:
                reply = json.loads(body)
            except ValueError:
                reply = None
            if not isinstance(reply, dict):
                reply = {}
            if "jsonrpc" in reply:
                return write(reply)
            if mid is not None:
                write(_err(mid, -32000, f"{problem}: {reply.get('error', '')}".rstrip(": ")))
            return
        except OSError as e:
            if mid is not None:
                write(_err(mid, -32000, f"door unreachable: {e}"))
            return
        if body:
            try:
                write(json.loads(body))
            except ValueError:
                if mid is not None:
                    write(_err(mid, -32000, "door: a reply that is not JSON"))

    with ThreadPoolExecutor(8) as pool:
        for line in stdin:
            if line.strip():
                pool.submit(one, line)
    return 0


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    sys.exit(main())
