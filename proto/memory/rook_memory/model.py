"""Model calls through Claude Code headless (`claude -p`), the way the house agent's turns go.

No API key: it uses whatever Claude Code on this machine is signed in with. No built-in tools, no
settings, only the memory MCP server. Live sessions run in one fixed folder (`<store>/house`) so
they can be resumed and forked; replays save no session. There is no timeout. Every call is
recorded with its cost, time and how many model turns it took inside.
"""

import json
import subprocess
import time
import uuid


class ModelError(Exception):
    pass


def call(store, knobs, purpose, prompt, *, model, system=None, schema=None, new_session=False, resume=None,
         fork=False, persist=True, mcp=None, tools=()):
    """Answers Claude Code's JSON reply (result, structured_output, session_id, ...)."""
    m = knobs["model"]
    argv = [m["claude"], "-p", "--output-format", "json", "--model", model, "--tools", "",
            "--setting-sources", "", "--strict-mcp-config", "--permission-mode", m["permission_mode"]]
    session = None
    if new_session:
        session = str(uuid.uuid4())
        argv += ["--session-id", session]
    if resume:
        argv += ["--resume", resume]
        if fork:
            argv += ["--fork-session"]
    if not persist:
        argv += ["--no-session-persistence"]
    if system and not resume:
        argv += ["--system-prompt", system]
    if schema:
        argv += ["--json-schema", json.dumps(schema)]
    if mcp:
        argv += ["--mcp-config", mcp]
    if tools:
        argv += ["--allowedTools", ",".join(f"mcp__memory__{t}" for t in tools)]
    house = store.folder / "house"
    house.mkdir(exist_ok=True)
    started = time.monotonic()
    done = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=house)
    seconds = time.monotonic() - started
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        store.add_call(purpose, model, seconds, None, None, None, None, False,
                       f"exit {done.returncode}: {(done.stderr or done.stdout)[:300]}", resume or session)
        raise ModelError(f"claude answered no JSON (exit {done.returncode}): {(done.stderr or done.stdout)[:300]}")
    usage = reply.get("usage") or {}
    tokens_in = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    ok = not reply.get("is_error") and (not schema or isinstance(reply.get("structured_output"), dict))
    # total_cost_usd is the session's running total when a session is resumed (and a fork's includes its
    # parent's), so this call's cost is what it added.
    total = reply.get("total_cost_usd")
    cost = total
    if total is not None and resume:
        cost = total - store.cc_total(resume)
    if total is not None and persist and (resume or session) and not fork:
        store.set_cc_total(reply.get("session_id") or resume or session, total)
    store.add_call(purpose, model, seconds, cost, tokens_in, usage.get("output_tokens"),
                   reply.get("num_turns"), ok, None if ok else str(reply.get("result"))[:300],
                   reply.get("session_id") or session, total)
    if not ok:
        raise ModelError(f"claude: {str(reply.get('result'))[:300]}")
    reply.setdefault("session_id", session)
    return reply
