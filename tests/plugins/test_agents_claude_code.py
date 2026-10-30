"""agents-claude-code.turn against a fake ``claude`` on PATH (port_helpers.Bins):
the argv it builds, the prompt on stdin, resume, errors and the timeout."""

import json

import pytest

from port_helpers import CONTRACTS, ME, PLUGINS, go
from rook.kit.ctx import Ctx, KitError, invoke, load_run
from rook.kit.manifest import load

FOLDER = PLUGINS / "agents-claude-code"

CLAUDE = """
prompt = sys.stdin.read()
with open(os.path.join(os.path.dirname(sys.argv[0]), "claude.stdin"), "a") as f:
    f.write(prompt + "\\n---\\n")
mode = os.environ.get("FAKE_CLAUDE", "ok")
if mode == "ok":
    sid = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-1"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "hi: " + prompt,
                      "session_id": sid, "usage": {"input_tokens": 5, "output_tokens": 2, "server_tool_use": {}},
                      "cwd": os.getcwd()}))
elif mode == "error":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Credit balance is too low"}))
elif mode == "crash":
    sys.stderr.write("not logged in\\n")
    sys.exit(1)
elif mode == "slow":
    import time
    time.sleep(30)
"""


def turn(tmp_path, settings=None, **args):
    m = load(FOLDER, CONTRACTS).data
    ctx = Ctx(m, ME, settings=settings or {}, data_dir=tmp_path / "data", cap="turn")
    func = load_run(FOLDER, "scripts/claude.py:turn", {})
    return go(lambda: invoke(func, ctx, args))


def test_a_turn_and_its_resume(tmp_path, bins):
    bins.add("claude", CLAUDE)
    out = turn(tmp_path, prompt="hello", system="You are pm.")
    assert out == {"text": "hi: hello", "session": "sess-1", "usage": {"input_tokens": 5, "output_tokens": 2}}
    (call,) = bins.calls("claude")
    assert call["argv"] == ["-p", "--output-format", "json", "--model", "opus",
                            "--append-system-prompt", "You are pm."]
    assert (tmp_path / "data" / "work").is_dir()                   # its own work folder by default
    out = turn(tmp_path, {"model": "", "permission_mode": "acceptEdits", "allowed_tools": ["Read", "Bash(git:*)"],
                          "mcp_config": "/etc/rook/mcp.json"},
               prompt="again", session="sess-9", model="haiku", cwd=str(tmp_path), effort="high")
    assert out["session"] == "sess-9"
    assert bins.calls("claude")[1]["argv"] == [
        "-p", "--output-format", "json", "--resume", "sess-9", "--model", "haiku",
        "--permission-mode", "acceptEdits", "--allowedTools", "Read,Bash(git:*)", "--mcp-config", "/etc/rook/mcp.json"]
    assert (bins.dir / "claude.stdin").read_text() == "hello\n---\nagain\n---\n"


@pytest.mark.parametrize("mode, error", [
    ("error", "Claude Code: Credit balance is too low"),
    ("crash", r"Claude Code failed \(exit 1\): not logged in"),
])
def test_failures_say_why(tmp_path, bins, monkeypatch, mode, error):
    bins.add("claude", CLAUDE)
    monkeypatch.setenv("FAKE_CLAUDE", mode)
    with pytest.raises(KitError, match=error):
        turn(tmp_path, prompt="x")


def test_a_slow_turn_is_killed(tmp_path, bins, monkeypatch):
    bins.add("claude", CLAUDE)
    monkeypatch.setenv("FAKE_CLAUDE", "slow")
    with pytest.raises(KitError, match="took longer than 300ms") as e:
        turn(tmp_path, prompt="x", timeout="300ms")
    assert e.value.retry


def test_a_cancelled_turn_kills_claude(tmp_path, bins, monkeypatch):
    import asyncio
    import subprocess
    bins.add("claude", CLAUDE)
    monkeypatch.setenv("FAKE_CLAUDE", "slow")
    m = load(FOLDER, CONTRACTS).data
    func = load_run(FOLDER, "scripts/claude.py:turn", {})

    async def body():
        task = asyncio.ensure_future(invoke(func, Ctx(m, ME, data_dir=tmp_path / "data", cap="turn"), {"prompt": "x"}))
        for _ in range(250):
            if (bins.dir / "claude.stdin").exists():
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    go(body)
    ps = subprocess.run(["pgrep", "-f", str(bins.dir / "claude")], capture_output=True, text=True)
    assert ps.stdout.strip() == ""


def test_missing_program_and_folder(tmp_path, bins):
    with pytest.raises(KitError, match="nope not found: install Claude Code"):
        turn(tmp_path, {"claude": "nope"}, prompt="x")
    with pytest.raises(KitError, match="no folder"):
        turn(tmp_path, prompt="x", cwd=str(tmp_path / "missing"))


def test_it_provides_the_agent_backend_contract():
    m = load(FOLDER, CONTRACTS).data
    assert m["provides"] == {"agent.backend": "agents-claude-code.turn"}
    assert json.dumps(m["caps"]["agents-claude-code.turn"]["result"]["fields"]["session"]) == \
        json.dumps({"type": "str", "required": True})
