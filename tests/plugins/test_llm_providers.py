"""The llm.complete providers against fakes: llm-openai and llm-anthropic
against local HTTP servers (fake_services), llm-cli against fake ``claude``
and ``codex`` programs on PATH. Each runs its plugin function with a kit Ctx,
under a hard timeout."""

import json

import pytest

from fake_services import FakeClaude, FakeOpenAI, serve
from port_helpers import CONTRACTS, ME, PLUGINS, go
from rook.kit.ctx import Ctx, KitError, invoke, load_run
from rook.kit.manifest import check, load

pytest.importorskip("aiohttp")

MSGS = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"}, {"role": "user", "content": "how are you?"}]


def run(plugin, func, settings, args, tmp_path, secret=None, fake=None):
    """Run ``plugin``'s ``func`` with ``args``; ``fake`` is served first and its
    URL replaces ``{url}`` in the settings."""
    m = load(PLUGINS / plugin, CONTRACTS).data
    script = m["caps"][f"{plugin}.{func}"]["run"]

    async def body():
        async def call(base=""):
            values = {k: v.replace("{url}", base) if isinstance(v, str) else v for k, v in settings.items()}
            ctx = Ctx(m, ME, settings=values, resolve_secret=secret, data_dir=tmp_path / "data", cap=func)
            return await invoke(load_run(PLUGINS / plugin, script, {}), ctx, args)
        if fake is None:
            return await call()
        async with serve(fake.app) as base:
            return await call(base)
    return go(body)


@pytest.mark.parametrize("name", ["llm-openai", "llm-anthropic", "llm-cli"])
def test_manifests_are_clean_providers(name):
    data, issues = check(PLUGINS / name, CONTRACTS)
    assert data is not None and [str(i) for i in issues] == []
    assert data["provides"] == {"llm.complete": f"{name}.complete"}


# -- llm-openai -----------------------------------------------------------------

def test_openai_completion_and_systems(tmp_path):
    fake = FakeOpenAI(key="sk-local")
    settings = {"url": "{url}/v1", "key": "lab-key", "model_one": "small", "model_two": "big", "timeout": "5s"}
    out = run("llm-openai", "complete", settings, {"messages": MSGS, "stop": ["\n\n"]}, tmp_path,
              secret=lambda name: {"lab-key": "sk-local"}[name], fake=fake)
    assert out == {"text": "echo: how are you?", "model": "big", "stop_reason": "end",
                   "usage": {"input": 7, "output": 3}}
    (req,) = fake.requests
    assert req["headers"]["authorization"] == "Bearer sk-local"
    assert req["body"] == {"messages": MSGS, "model": "big", "stop": ["\n\n"]}

    fake = FakeOpenAI()
    out = run("llm-openai", "complete", {**settings, "key": None},
              {"messages": MSGS[1:2], "model": "three", "max_tokens": 1, "temperature": 0.2}, tmp_path, fake=fake)
    assert (out["model"], out["stop_reason"]) == ("small", "length")      # three falls back to one
    assert fake.requests[0]["body"]["temperature"] == 0.2 and "authorization" not in fake.requests[0]["headers"]
    out = run("llm-openai", "models", {**settings, "key": None}, {}, tmp_path, fake=FakeOpenAI())
    assert out == {"models": ["llama", "qwen"], "systems": {"one": "small", "two": "big", "three": "small"}}


def test_openai_errors(tmp_path):
    settings = {"url": "{url}/v1", "timeout": "5s"}
    with pytest.raises(KitError) as e:
        run("llm-openai", "complete", settings, {"messages": MSGS, "model": "busy"}, tmp_path, fake=FakeOpenAI())
    assert e.value.retry and "HTTP 503: overloaded" in e.value.message
    with pytest.raises(KitError, match="HTTP 401: bad key"):
        run("llm-openai", "complete", settings, {"messages": MSGS}, tmp_path, fake=FakeOpenAI(key="x"))
    with pytest.raises(KitError) as e:          # nothing listening
        run("llm-openai", "complete", {"url": "http://127.0.0.1:9/v1", "timeout": "2s"}, {"messages": MSGS},
            tmp_path)
    assert e.value.retry


# -- llm-anthropic ----------------------------------------------------------------

def test_claude_completion(tmp_path):
    fake = FakeClaude(key="sk-ant-test")
    settings = {"url": "{url}", "key": "claude-api"}
    secret = {"claude-api": "sk-ant-test"}.__getitem__
    out = run("llm-anthropic", "complete", settings, {"messages": MSGS, "temperature": 0.7, "stop": ["END"]},
              tmp_path, secret=secret, fake=fake)
    assert out == {"text": "claude: how are you?", "model": "claude-opus-5-5", "stop_reason": "end",
                   "usage": {"input": 11, "output": 4}}
    (req,) = fake.requests
    assert req["headers"]["anthropic-version"] == "2026-10-21" and req["headers"]["x-api-key"] == "sk-ant-test"
    assert req["body"] == {"model": "claude-opus-5-5", "max_tokens": 16000, "system": "Be brief.",
                           "messages": MSGS[1:], "stop_sequences": ["END"]}      # no temperature
    out = run("llm-anthropic", "complete", settings, {"messages": MSGS[1:2], "model": "one", "max_tokens": 50},
              tmp_path, secret=secret, fake=FakeClaude(key="sk-ant-test"))
    assert out["model"] == "claude-haiku-5-5"
    out = run("llm-anthropic", "models", settings, {}, tmp_path, secret=secret, fake=FakeClaude(key="sk-ant-test"))
    assert out["models"] == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert out["systems"] == {"one": "claude-haiku-5-5", "two": "claude-opus-5-5", "three": "claude-haiku-5-5"}


def test_claude_errors(tmp_path):
    settings = {"url": "{url}", "key": "claude-api"}
    with pytest.raises(KitError) as e:
        run("llm-anthropic", "complete", settings, {"messages": MSGS, "model": "overloaded"}, tmp_path,
            secret=lambda n: "sk", fake=FakeClaude(key="sk"))
    assert e.value.retry and "HTTP 529: Overloaded" in e.value.message
    with pytest.raises(KitError, match="invalid x-api-key"):
        run("llm-anthropic", "complete", settings, {"messages": MSGS}, tmp_path,
            secret=lambda n: "wrong", fake=FakeClaude(key="sk"))
    with pytest.raises(KitError, match="key is not set"):
        run("llm-anthropic", "complete", {"url": "{url}"}, {"messages": MSGS}, tmp_path, fake=FakeClaude(key="sk"))
    with pytest.raises(KitError, match="no user message"):
        run("llm-anthropic", "complete", settings, {"messages": MSGS[:1]}, tmp_path, secret=lambda n: "sk")


# -- llm-cli ------------------------------------------------------------------------

CLAUDE = """
prompt = sys.stdin.read()
with open(os.path.join(os.path.dirname(sys.argv[0]), "claude.stdin"), "a") as f:
    f.write(prompt + "\\n---\\n")
if argv == ["--version"]:
    print("2.1.314 (Claude Code)")
elif os.environ.get("FAKE_CLI") == "crash":
    sys.stderr.write("not logged in\\n")
    sys.exit(1)
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "Fine, thanks. STOP and more", "usage": {"input_tokens": 9, "output_tokens": 5}}))
"""

CODEX = """
prompt = sys.stdin.read()
with open(os.path.join(os.path.dirname(sys.argv[0]), "codex.stdin"), "a") as f:
    f.write(prompt + "\\n---\\n")
out = argv[argv.index("-o") + 1]
with open(out, "w") as f:
    f.write("codex says hi\\n")
"""


def test_cli_claude(tmp_path, bins):
    bins.add("claude", CLAUDE)
    out = run("llm-cli", "complete", {}, {"messages": MSGS, "stop": ["STOP"], "max_tokens": 5}, tmp_path)
    assert out == {"text": "Fine, thanks. ", "model": "opus", "stop_reason": "stop",
                   "usage": {"input": 9, "output": 5}}
    (call,) = bins.calls("claude")
    assert call["argv"] == ["-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
                            "--no-session-persistence",
                            "--model", "opus", "--system-prompt", "Be brief."]
    prompt = (bins.dir / "claude.stdin").read_text()
    assert "User: hi\n\nAssistant: hello\n\nUser: how are you?" in prompt and "Be brief" not in prompt
    run("llm-cli", "complete", {"model_one": "sonnet"}, {"messages": MSGS[1:2], "model": "one"}, tmp_path)
    assert bins.calls("claude")[1]["argv"][-2:] == ["--model", "sonnet"]
    assert (bins.dir / "claude.stdin").read_text().endswith("---\nhi\n---\n")     # one message goes in as is
    out = run("llm-cli", "models", {}, {}, tmp_path)
    assert out == {"tool": "claude", "version": "2.1.314 (Claude Code)", "models": ["haiku", "opus"],
                   "systems": {"one": "haiku", "two": "opus", "three": "haiku"}}


def test_cli_codex(tmp_path, bins):
    bins.add("codex", CODEX)
    out = run("llm-cli", "complete", {"tool": "codex"}, {"messages": MSGS}, tmp_path)
    assert out == {"text": "codex says hi", "model": "", "stop_reason": "end"}
    (call,) = bins.calls("codex")
    assert call["argv"][:6] == ["exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-o"]
    assert call["argv"][-1] == "-" and "-m" not in call["argv"]
    assert (bins.dir / "codex.stdin").read_text().startswith("Be brief.\n\nContinue this conversation")
    run("llm-cli", "complete", {"tool": "codex", "model_two": "sol"}, {"messages": MSGS[1:2]}, tmp_path)
    assert bins.calls("codex")[1]["argv"][-3:] == ["-m", "sol", "-"]


def test_cli_failures(tmp_path, bins, monkeypatch):
    bins.add("claude", CLAUDE)
    monkeypatch.setenv("FAKE_CLI", "crash")
    with pytest.raises(KitError, match=r"claude failed \(exit 1\): not logged in"):
        run("llm-cli", "complete", {}, {"messages": MSGS}, tmp_path)
    with pytest.raises(KitError, match="not found"):
        run("llm-cli", "complete", {"program": str(tmp_path / "nope")}, {"messages": MSGS}, tmp_path)
    assert json.loads(json.dumps(bins.calls("claude")))      # the fake ran
