"""agents-codex.turn against a fake ``codex`` on PATH (port_helpers.Bins): the
argv it builds (new session and resume), the prompt on stdin, the JSONL
events it reads, errors and the timeout. The fake prints the event shapes a
real ``codex exec --json`` 0.160 printed on Oct 31 2026."""

import json

import pytest

from port_helpers import CONTRACTS, ME, PLUGINS, go
from rook.kit.ctx import Ctx, KitError, invoke, load_run
from rook.kit.manifest import load

FOLDER = PLUGINS / "agents-codex"

CODEX = """
prompt = sys.stdin.read()
with open(os.path.join(os.path.dirname(sys.argv[0]), "codex.stdin"), "a") as f:
    f.write(prompt + "\\n---\\n")
mode = os.environ.get("FAKE_CODEX", "ok")
def ev(**e):
    print(json.dumps(e), flush=True)
tid = argv[-2] if "resume" in argv else "01a19312-6afd-7340-a485-9a577ddfd14f"
ev(type="thread.started", thread_id=tid)
if mode == "ok":
    ev(type="item.completed", item={"id": "item_0", "type": "error", "message": "Model metadata not found"})
    ev(type="turn.started")
    ev(type="item.completed", item={"id": "item_1", "type": "agent_message", "text": "looking"})
    ev(type="item.completed", item={"id": "item_2", "type": "agent_message", "text": "hi: " + prompt})
    ev(type="turn.completed", usage={"input_tokens": 15, "cached_input_tokens": 10, "cache_write_input_tokens": 0,
                                     "output_tokens": 5, "reasoning_output_tokens": 0})
elif mode == "failed":
    ev(type="turn.started")
    msg = json.dumps({"type": "error", "status": 400, "error": {"type": "invalid_request_error",
                      "message": "The 'gpt-x' model is not supported when using Codex with a ChatGPT account."}})
    ev(type="error", message=msg)
    ev(type="turn.failed", error={"message": msg})
elif mode == "silent":
    ev(type="turn.started")
    ev(type="turn.completed", usage={})
elif mode == "crash":
    sys.stderr.write("Not logged in\\n")
    sys.exit(1)
elif mode == "slow":
    import time
    time.sleep(30)
"""

SETTINGS = {"codex": "codex", "model": "", "model_three": "", "sandbox": "", "config": [], "isolate": True}


def test_defaults_follow_castling_inference():
    m = load(FOLDER, CONTRACTS).data
    settings = m["settings"]
    assert "default" not in settings["model"]                      # System Two: Codex's own
    assert settings["model_three"]["default"] == "gpt-6-luna"     # "Luna for System One and maintenance"


def turn(tmp_path, settings=None, **args):
    m = load(FOLDER, CONTRACTS).data
    ctx = Ctx(m, ME, settings={**SETTINGS, **(settings or {})}, data_dir=tmp_path / "data", cap="turn")
    func = load_run(FOLDER, "scripts/codex.py:turn", {})
    return go(lambda: invoke(func, ctx, args))


def test_a_turn_and_its_resume(tmp_path, bins):
    bins.add("codex", CODEX)
    out = turn(tmp_path, prompt="hello", system='You are "pm".\nBe brief.')
    assert out == {"text": "hi: hello", "session": "01a19312-6afd-7340-a485-9a577ddfd14f",
                   "usage": {"input_tokens": 15, "cached_input_tokens": 10, "output_tokens": 5,
                             "reasoning_output_tokens": 0}}
    (call,) = bins.calls("codex")
    assert call["argv"] == ["exec", "--json", "--skip-git-repo-check",
                            "-c", 'developer_instructions="You are \\"pm\\".\\nBe brief."',
                            "--ignore-user-config", "--ignore-rules", "-"]
    assert (tmp_path / "data" / "work").is_dir()                   # its own work folder by default
    out = turn(tmp_path, {"model": "gpt-6-sol", "sandbox": "workspace-write", "isolate": False,
                          "config": ['mcp_servers.rook.url="http://hub:8751/mcp"']},
               prompt="again", session="sess-9", effort="high", cwd=str(tmp_path))
    assert out["session"] == "sess-9"
    assert bins.calls("codex")[1]["argv"] == [
        "exec", "resume", "--json", "--skip-git-repo-check", "-c", 'sandbox_mode="workspace-write"',
        "-m", "gpt-6-sol", "-c", 'model_reasoning_effort="high"',
        "-c", 'mcp_servers.rook.url="http://hub:8751/mcp"', "sess-9", "-"]
    assert (bins.dir / "codex.stdin").read_text() == "hello\n---\nagain\n---\n"


@pytest.mark.parametrize("model, settings, expect", [
    ("three", {"model": "gpt-6-sol", "model_three": "gpt-6-luna"}, ["-m", "gpt-6-luna"]),
    ("two", {"model": "gpt-6-sol"}, ["-m", "gpt-6-sol"]),
    ("three", {"model": "gpt-6-sol"}, []),                          # blank: Codex's own default
    ("o9", {}, ["-m", "o9"]),
])
def test_system_names_pick_the_settings_models(tmp_path, bins, model, settings, expect):
    bins.add("codex", CODEX)
    turn(tmp_path, settings, prompt="x", model=model)
    argv = bins.calls("codex")[0]["argv"]
    got = argv[argv.index("-m"):argv.index("-m") + 2] if "-m" in argv else []
    assert got == expect


@pytest.mark.parametrize("args", [{"session": "--dangerously-bypass-approvals-and-sandbox"},
                                  {"model": "--oss"}])
def test_session_and_model_are_never_options(tmp_path, bins, args):
    bins.add("codex", CODEX)
    with pytest.raises(KitError, match="bad args"):
        turn(tmp_path, prompt="hi", **args)
    assert not bins.calls("codex")


def test_config_lines_are_key_value_never_options(tmp_path, bins):
    bins.add("codex", CODEX)
    with pytest.raises(KitError, match="not key=value"):
        turn(tmp_path, {"config": ["--dangerously-bypass-approvals-and-sandbox"]}, prompt="hi")
    assert not bins.calls("codex")


@pytest.mark.parametrize("mode, error", [
    ("failed", "Codex: The 'gpt-x' model is not supported when using Codex with a ChatGPT account."),
    ("silent", "Codex answered without a reply"),
    ("crash", r"Codex failed \(exit 1\): Not logged in"),
])
def test_failures_say_why(tmp_path, bins, monkeypatch, mode, error):
    bins.add("codex", CODEX)
    monkeypatch.setenv("FAKE_CODEX", mode)
    with pytest.raises(KitError, match=error):
        turn(tmp_path, prompt="x")


def test_a_slow_turn_is_killed(tmp_path, bins, monkeypatch):
    bins.add("codex", CODEX)
    monkeypatch.setenv("FAKE_CODEX", "slow")
    with pytest.raises(KitError, match="took longer than 300ms") as e:
        turn(tmp_path, prompt="x", timeout="300ms")
    assert e.value.retry


def test_missing_program_and_folder(tmp_path, bins):
    with pytest.raises(KitError, match="nope not found: install Codex"):
        turn(tmp_path, {"codex": "nope"}, prompt="x")
    with pytest.raises(KitError, match="no folder"):
        turn(tmp_path, prompt="x", cwd=str(tmp_path / "missing"))


def test_it_provides_the_agent_backend_contract():
    m = load(FOLDER, CONTRACTS).data
    assert m["provides"] == {"agent.backend": "agents-codex.turn"}
    assert json.dumps(m["caps"]["agents-codex.turn"]["result"]["fields"]["session"]) == \
        json.dumps({"type": "str", "required": True})


def test_the_agents_package_offers_only_backends_that_exist():
    agents = load(PLUGINS / "agents", CONTRACTS).data
    enum = agents["caps"]["agents.create"]["args"]["backend"]["enum"]
    assert enum == ["default", "claude-code", "codex"]
    for name in enum[1:]:
        assert (PLUGINS / f"agents-{name}" / "plugin.yaml").is_file()
        assert f"agents-{name}.turn" in agents["uses"]
