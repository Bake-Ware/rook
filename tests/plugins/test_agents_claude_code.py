"""agents-claude-code.turn on the hub against a fake ``claude`` on the PATH of
a worker with only the primitives (port_helpers.on_worker): the argv it
builds, the prompt on stdin, resume, the work folder and MCP config on the
worker, the door token by name, errors and the timeout."""

import json

import pytest

from port_helpers import CONTRACTS, PLUGINS, on_worker
from rook.kit.ctx import KitError
from rook.kit.manifest import check, load

FOLDER = PLUGINS / "agents-claude-code"

CLAUDE = """
prompt = sys.stdin.read()
with open(os.path.join(os.path.dirname(sys.argv[0]), "claude.stdin"), "a") as f:
    f.write(prompt + "\\n---\\n")
with open(os.path.join(os.path.dirname(sys.argv[0]), "claude.cwd"), "w") as f:
    f.write(os.getcwd())
mode = os.environ.get("FAKE_CLAUDE", "ok")
if mode == "ok":
    sys.stderr.write("a warning {not json}\\n")
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


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.delenv("OS", raising=False)
    return folder


def turn(settings=None, **args):
    return on_worker("agents-claude-code", "agents-claude-code.turn", args,
                     settings={"worker": "w1", **(settings or {})})


def test_a_turn_and_its_resume(home, bins, tmp_path):
    bins.add("claude", CLAUDE)
    out, seen = turn(prompt="hello", system="You are pm.")
    assert out == {"text": "hi: hello", "session": "sess-1", "usage": {"input_tokens": 5, "output_tokens": 2}}
    (call,) = bins.calls("claude")
    assert call["argv"] == ["-p", "--output-format", "json", "--model", "opus",
                            "--append-system-prompt", "You are pm.",
                            "--setting-sources", "project,local", "--strict-mcp-config"]
    work = home / ".rook-plugins" / "agents-claude-code" / "work"
    assert (bins.dir / "claude.cwd").read_text() == str(work)        # the same folder every turn
    (opened,) = [a for c, a in seen if c == "shell.open"]
    assert opened["pty"] is False and opened["stdin"] == "hello" and "env" not in opened
    out, _ = turn({"model": "", "permission_mode": "acceptEdits", "allowed_tools": ["Read", "Bash(git:*)"],
                   "mcp_config": "/etc/rook/mcp.json", "isolate": False},
                  prompt="again", session="sess-9", model="haiku", cwd="~", effort="high")
    assert out["session"] == "sess-9"
    assert bins.calls("claude")[1]["argv"] == [
        "-p", "--output-format", "json", "--resume", "sess-9", "--model", "haiku",
        "--permission-mode", "acceptEdits", "--allowedTools", "Read,Bash(git:*)", "--mcp-config", "/etc/rook/mcp.json"]
    assert (bins.dir / "claude.cwd").read_text() == str(home)          # ~ is the worker's home
    assert (bins.dir / "claude.stdin").read_text() == "hello\n---\nagain\n---\n"


@pytest.mark.parametrize("args", [{"session": "--dangerously-skip-permissions"},
                                  {"session": "--permission-mode=bypassPermissions"},
                                  {"model": "--settings=/tmp/hooks.json"}])
def test_session_and_model_are_never_options(home, bins, args):
    bins.add("claude", CLAUDE)
    with pytest.raises(KitError, match="bad args"):
        turn(prompt="hi", **args)
    assert not bins.calls("claude")


@pytest.mark.parametrize("mode, error", [
    ("error", "Claude Code: Credit balance is too low"),
    ("crash", r"Claude Code failed \(exit 1\): not logged in"),
])
def test_failures_say_why(home, bins, monkeypatch, mode, error):
    bins.add("claude", CLAUDE)
    monkeypatch.setenv("FAKE_CLAUDE", mode)
    with pytest.raises(KitError, match=error):
        turn(prompt="x")


def test_a_slow_turn_is_killed(home, bins, monkeypatch):
    import subprocess
    bins.add("claude", CLAUDE)
    monkeypatch.setenv("FAKE_CLAUDE", "slow")
    with pytest.raises(KitError, match="took longer than 300ms") as e:
        turn(prompt="x", timeout="300ms")
    assert e.value.retry
    ps = subprocess.run(["pgrep", "-f", str(bins.dir / "claude")], capture_output=True, text=True)
    assert ps.stdout.strip() == ""


def test_missing_worker_program_and_folder(home, bins):
    with pytest.raises(KitError, match="set agents-claude-code.worker"):
        on_worker("agents-claude-code", "agents-claude-code.turn", {"prompt": "x"})
    with pytest.raises(KitError, match="nope not found: install Claude Code or set agents-claude-code.claude"):
        turn({"claude": "nope"}, prompt="x")
    with pytest.raises(KitError, match="not found: .*missing"):
        turn(prompt="x", cwd=str(home / "missing"))


def test_it_runs_on_the_hub_and_provides_the_agent_backend_contract():
    data, issues = check(FOLDER, CONTRACTS)
    assert data is not None and [str(i) for i in issues] == []
    m = load(FOLDER, CONTRACTS).data
    assert m["runs_on"] == "hub" and m["provides"] == {"agent.backend": "agents-claude-code.turn"}
    assert json.dumps(m["caps"]["agents-claude-code.turn"]["result"]["fields"]["session"]) == \
        json.dumps({"type": "str", "required": True})


def test_system_three_is_its_model_three_setting(home, bins):
    bins.add("claude", CLAUDE)
    turn(prompt="x", model="three")
    turn({"model_three": "claude-haiku-5-5"}, prompt="x", model="three")
    turn(prompt="x", model="two")
    models = [c["argv"][c["argv"].index("--model") + 1] for c in bins.calls("claude")]
    assert models == ["haiku", "claude-haiku-5-5", "opus"]


def test_a_turn_reaches_rook_with_its_token_by_name(home, bins):
    """The door's token name goes on untouched in the process's environment
    (the hub resolves it on the hop to the worker); the MCP config on the
    worker names the variable, never the token, and Rook's tools are allowed."""
    bins.add("claude", CLAUDE + """
cfg = argv[argv.index("--mcp-config") + 1:]
with open(os.path.join(os.path.dirname(sys.argv[0]), "mcp.seen"), "w") as f:
    json.dump([json.load(open(p)) for p in cfg if p.endswith(".json")], f)
""")
    ref = "{{secret:turn-abc}}"
    _, seen = turn({"mcp_config": "/dev/null", "allowed_tools": ["Read"]}, prompt="hi", door=ref)
    (opened,) = [a for c, a in seen if c == "shell.open"]
    assert opened["env"] == {"ROOK_DOOR_TOKEN": ref}
    argv = bins.calls("claude")[0]["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "Read,mcp__rook"
    i = argv.index("--mcp-config")
    config = home / ".rook-plugins" / "agents-claude-code" / "rook-mcp.json"
    assert argv[i + 1:i + 3] == ["/dev/null", str(config)] and argv[i + 3] == "--setting-sources"
    assert json.loads((bins.dir / "mcp.seen").read_text()) == [{"mcpServers": {"rook": {
        "type": "http", "url": "http://127.0.0.1:8741/mcp",
        "headers": {"Authorization": "Bearer ${ROOK_DOOR_TOKEN}"}}}}]
    assert bins.calls("claude")[0]["env"]["ROOK_DOOR_TOKEN"] == ref        # the fake worker resolves nothing
    with pytest.raises(KitError, match="door_url"):
        turn({"door_url": ""}, prompt="hi", door=ref)


def test_its_models_need_no_worker():
    """The aliases the CLI takes (its own list), and the model setting; the worker isn't asked."""
    out, seen = on_worker("agents-claude-code", "agents-claude-code.models", settings={"worker": ""})
    assert out == {"models": ["sonnet", "opus", "haiku", "fable"], "default": "opus"} and seen == []
