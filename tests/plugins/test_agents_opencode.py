"""agents-opencode.turn on the hub against a fake ``opencode`` on the PATH of a
worker with only the primitives (port_helpers.on_worker): the argv it builds,
the instructions at the head of a new session's first message (free models
refuse a custom system prompt), the generated config (the door as the
``rook`` MCP server, token only in the environment, own tools off), the JSON
events it reads as they come, and failures. The fake prints the event shapes
a real ``opencode run --format json`` 1.18.35 printed on Nov 4 2026."""

import json

import pytest

from port_helpers import CONTRACTS, PLUGINS, on_worker
from rook.kit.ctx import KitError
from rook.kit.manifest import load

FOLDER = PLUGINS / "agents-opencode"

OPENCODE = """
prompt = sys.stdin.read()
d = os.path.dirname(sys.argv[0])
with open(os.path.join(d, "opencode.stdin"), "a") as f:
    f.write(prompt + "\\n---\\n")
with open(os.path.join(d, "opencode.env"), "w") as f:
    json.dump({k: os.environ.get(k) for k in ("ROOK_DOOR_TOKEN", "OPENCODE_CONFIG", "XDG_CONFIG_HOME")}, f)
mode = os.environ.get("FAKE_OPENCODE", "ok")
sid = argv[argv.index("--session") + 1] if "--session" in argv else "ses_e57c624e5ffeULsEWLUR7a1EMV"
def ev(**e):
    print(json.dumps({"timestamp": 1, "sessionID": sid, **e}), flush=True)
if mode == "ok":
    ev(type="step_start", part={"type": "step-start"})
    ev(type="text", part={"type": "text", "text": "let me look"})
    ev(type="tool_use", part={"type": "tool", "tool": "rook_call", "state": {"status": "completed"}})
    ev(type="step_finish", part={"type": "step-finish", "reason": "tool-calls",
                                 "tokens": {"input": 10, "output": 3, "reasoning": 0}})
    ev(type="step_start", part={"type": "step-start"})
    ev(type="text", part={"type": "text", "text": "ok: " + prompt[-5:]})
    ev(type="step_finish", part={"type": "step-finish", "reason": "stop",
                                 "tokens": {"input": 20, "output": 2, "reasoning": 1}, "cost": 0})
elif mode == "limit":
    sys.stderr.write('timestamp=2026-11-04T20:25:47Z level=ERROR run=dd17 message="stream error" '
                     'providerID=opencode modelID=big-pickle error.error="AI_APICallError: Free usage limit '
                     'reached. Free models work in OpenCode"\\n')
    sys.stderr.flush()
    import time
    time.sleep(30)
elif mode == "error":
    ev(type="error", error={"name": "ProviderModelNotFoundError", "data": {"message": "Model not found: x/y"}})
elif mode == "crash":
    sys.stderr.write("boom\\n")
    sys.exit(3)
elif mode == "slow":
    import time
    time.sleep(30)
"""

SETTINGS = {"worker": "w1"}
REF = "{{secret:turn-abc}}"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.delenv("OS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return folder


def turn(tmp_path, settings=None, **args):
    return on_worker("agents-opencode", "agents-opencode.turn", args, settings={**SETTINGS, **(settings or {})})[0]


def test_a_turn_and_its_resume(tmp_path, bins, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "userconf"))
    bins.add("opencode", OPENCODE)
    out = turn(tmp_path, {"model": "opencode/glm-5.3-flash-free"}, prompt="hello", system="You are the house agent.")
    assert out == {"text": "ok: hello", "session": "ses_e57c624e5ffeULsEWLUR7a1EMV",
                   "usage": {"input_tokens": 30, "output_tokens": 5, "reasoning_tokens": 1}}
    (call,) = bins.calls("opencode")
    assert call["argv"] == ["run", "--format", "json", "--print-logs", "--log-level", "ERROR",
                            "--model", "opencode/glm-5.3-flash-free"]
    first = (bins.dir / "opencode.stdin").read_text()
    assert first.startswith("<instructions>\nYou are the house agent.\n</instructions>\n\nhello")
    out = turn(tmp_path, prompt="again", session="ses_abc", system="You are the house agent.", effort="high")
    assert out["session"] == "ses_abc"
    assert bins.calls("opencode")[1]["argv"][-4:] == ["--session", "ses_abc", "--variant", "high"]
    # a resumed session has its instructions already
    assert (bins.dir / "opencode.stdin").read_text().endswith("---\nagain\n---\n")
    assert (tmp_path / "home" / ".rook-plugins" / "agents-opencode" / "work").is_dir()


def test_the_config_gives_rook_and_only_rook(tmp_path, bins, monkeypatch):
    user = tmp_path / "userconf" / "opencode"
    user.mkdir(parents=True)
    (user / "opencode.jsonc").write_text('{\n // mine\n "model": "kaiju/q", "provider": {"kaiju": {"npm": "x"}},\n'
                                         ' "mcp": {"other": {"type": "remote", "url": "http://x"}},\n'
                                         ' "agent": {"mine": {}},}\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "userconf"))
    bins.add("opencode", OPENCODE)
    turn(tmp_path, prompt="hi", door=REF)
    env = json.loads((bins.dir / "opencode.env").read_text())
    assert env["ROOK_DOOR_TOKEN"] == REF          # by name, in the environment (the hub resolves it there)
    assert REF not in json.dumps(bins.calls("opencode")[0]["argv"])
    cfg_text = open(env["OPENCODE_CONFIG"]).read()
    assert env["OPENCODE_CONFIG"] == str(tmp_path / "home" / ".rook-plugins" / "agents-opencode" / "config"
                                         / "opencode" / "opencode.json")
    assert REF not in cfg_text                                        # never on disk
    cfg = json.loads(cfg_text)
    assert env["XDG_CONFIG_HOME"] != str(tmp_path / "userconf")       # isolated from the user's config
    assert cfg["mcp"] == {"rook": {"type": "remote", "url": "http://127.0.0.1:8741/mcp", "enabled": True,
                                   "headers": {"Authorization": "Bearer {env:ROOK_DOOR_TOKEN}"}}}
    assert cfg["model"] == "kaiju/q" and "kaiju" in cfg["provider"]   # providers are kept
    assert "agent" not in cfg
    assert "tools" not in cfg and cfg["permission"] == {"*": "allow"}  # its own tools on, nothing denied
    turn(tmp_path, {"own_tools": True, "isolate": False}, prompt="hi")
    env = json.loads((bins.dir / "opencode.env").read_text())
    cfg = json.loads(open(env["OPENCODE_CONFIG"]).read())
    assert "mcp" not in cfg and "tools" not in cfg and env["ROOK_DOOR_TOKEN"] is None
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "userconf")
    with pytest.raises(KitError, match="door_url"):
        turn(tmp_path, {"door_url": ""}, prompt="hi", door=REF)


@pytest.mark.parametrize("model, settings, expect", [
    ("three", {"model": "a/b", "model_three": "c/d"}, ["--model", "c/d"]),
    ("three", {"model": "a/b"}, ["--model", "a/b"]),
    ("two", {"model": "a/b"}, ["--model", "a/b"]),
    (None, {}, []),
])
def test_system_names_pick_the_settings_models(tmp_path, bins, model, settings, expect):
    bins.add("opencode", OPENCODE)
    turn(tmp_path, settings, prompt="x", **({"model": model} if model else {}))
    argv = bins.calls("opencode")[0]["argv"]
    assert (argv[argv.index("--model"):argv.index("--model") + 2] if "--model" in argv else []) == expect


@pytest.mark.parametrize("args", [{"session": "--auto"}, {"model": "--pure"}, {"effort": "-x"}])
def test_args_are_never_options(tmp_path, bins, args):
    bins.add("opencode", OPENCODE)
    with pytest.raises(KitError, match="bad args"):
        turn(tmp_path, prompt="hi", **args)
    assert not bins.calls("opencode")


@pytest.mark.parametrize("mode, error", [
    ("limit", "OpenCode: AI_APICallError: Free usage limit reached"),
    ("error", "OpenCode: Model not found: x/y"),
    ("crash", r"OpenCode failed \(exit 3\): boom"),
])
def test_failures_say_why_without_waiting(tmp_path, bins, monkeypatch, mode, error):
    bins.add("opencode", OPENCODE)
    monkeypatch.setenv("FAKE_OPENCODE", mode)
    with pytest.raises(KitError, match=error):
        turn(tmp_path, prompt="x", timeout="10s")       # limit: ends at once, not after the timeout


def test_a_slow_turn_is_killed(tmp_path, bins, monkeypatch):
    bins.add("opencode", OPENCODE)
    monkeypatch.setenv("FAKE_OPENCODE", "slow")
    with pytest.raises(KitError, match="took longer than 300ms") as e:
        turn(tmp_path, prompt="x", timeout="300ms")
    assert e.value.retry


def test_missing_worker_program_and_folder(tmp_path, bins):
    with pytest.raises(KitError, match="set agents-opencode.worker"):
        turn(tmp_path, {"worker": ""}, prompt="x")
    with pytest.raises(KitError, match="nope not found: install OpenCode"):
        turn(tmp_path, {"opencode": "nope"}, prompt="x")
    with pytest.raises(KitError, match="not found: .*missing"):
        turn(tmp_path, prompt="x", cwd=str(tmp_path / "missing"))


def test_it_provides_the_agent_backend_contract():
    m = load(FOLDER, CONTRACTS).data
    assert m["provides"] == {"agent.backend": "agents-opencode.turn"}


def test_its_models_are_the_ones_opencode_lists(tmp_path, bins, home):
    """opencode models on the worker, provider/model one a line."""
    bins.add("opencode", "print('opencode/big-pickle'); print('a warning'); print('openai/gpt-x')")
    out, _ = on_worker("agents-opencode", "agents-opencode.models", settings=SETTINGS)
    assert out == {"models": ["opencode/big-pickle", "openai/gpt-x"], "default": ""}
    assert bins.calls("opencode")[0]["argv"] == ["models"]
    bins.add("opencode", "sys.stderr.write('boom\\\\n'); sys.exit(3)")
    with pytest.raises(KitError, match=r"opencode models failed \(exit 3\): boom"):
        on_worker("agents-opencode", "agents-opencode.models", settings=SETTINGS)
