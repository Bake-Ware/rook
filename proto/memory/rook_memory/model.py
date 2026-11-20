"""Model calls through Claude Code headless (`claude -p`), the way the house agent's turns go.

No API key: it uses whatever Claude Code on this machine is signed in with. Each call runs in an
empty folder with no tools, no settings, no MCP servers and no saved session, so nothing it reads
or writes is anything but the prompt. There is no timeout. Every call is recorded with its cost.
"""

import json
import subprocess
import tempfile
import time


class ModelError(Exception):
    pass


def ask(store, knobs, purpose, model, prompt, schema=None):
    """Send `prompt`; answer the JSON object the model wrote. With a `schema` and the knob
    structured_output on, Claude Code holds the answer to that shape. Records the call in the store."""
    argv = [knobs["model"]["claude"], "-p", "--output-format", "json", "--model", model,
            "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
            "--system-prompt", knobs["prompts"]["system"]]
    if schema and knobs["model"].get("structured_output"):
        argv += ["--json-schema", json.dumps(schema)]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rook-memory-call-") as empty:
        done = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=empty)
    seconds = time.monotonic() - started
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        store.add_call(purpose, model, seconds, None, None, None, False,
                       f"exit {done.returncode}: {(done.stderr or done.stdout)[:300]}")
        raise ModelError(f"claude answered no JSON (exit {done.returncode}): {(done.stderr or done.stdout)[:300]}")
    usage = reply.get("usage") or {}
    tokens_in = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    text = reply.get("result") or ""
    try:
        if reply.get("is_error"):
            raise ModelError(f"claude: {text[:300]}")
        answer = reply["structured_output"] if isinstance(reply.get("structured_output"), dict) else extract(text)
    except ModelError as e:
        store.add_call(purpose, model, seconds, reply.get("total_cost_usd"), tokens_in,
                       usage.get("output_tokens"), False, str(e)[:300])
        raise
    store.add_call(purpose, model, seconds, reply.get("total_cost_usd"), tokens_in, usage.get("output_tokens"), True)
    return answer


def extract(text):
    """The JSON object in a reply, also when the model wrapped it in a code fence or a sentence."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ModelError(f"no JSON object in the reply: {text[:300]}")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as e:
        raise ModelError(f"the reply's JSON does not parse ({e}): {text[:300]}") from None
