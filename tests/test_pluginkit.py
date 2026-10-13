"""rook.core.pluginkit / rook.core.paths: the shared plugin helpers, the worker
state directory (ROOK_WORKER_HOME) and plugins gated through settings."""
import ast
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rook.core import pluginkit as kit
from rook.core.facts import detect_facts, wire_facts
from rook.core.host import PluginHost
from rook.core.plugin import Candidate, SettingsView
from rook.core.registry import CapabilityRegistry

ROOT = Path(__file__).resolve().parent.parent


# -- run ----------------------------------------------------------------------

def test_run_returns_decoded_output_and_code():
    res = asyncio.run(kit.run([sys.executable, "-c",
                               "import sys; sys.stdout.buffer.write(b'hi \\xff'); "
                               "sys.stderr.write('oops'); sys.exit(3)"], 10))
    code, out, err = res
    assert code == 3 and not res.ok
    assert out == "hi �"          # bad bytes replaced, never a decode error
    assert err == "oops" and res.error() == "oops"


def test_run_feeds_stdin_and_overlays_env():
    res = asyncio.run(kit.run([sys.executable, "-c",
                               "import os,sys; print(sys.stdin.read() + os.environ['KIT_X'] + "
                               "str('PATH' in os.environ))"],
                              10, stdin="in-", env={"KIT_X": "env"}))
    assert res.stdout.strip() == "in-envTrue"


def test_run_kills_on_timeout():
    with pytest.raises(kit.RunTimeout) as e:
        asyncio.run(kit.run([sys.executable, "-c", "import time; time.sleep(30)"], 0.3))
    assert "timed out after 0.3s" in str(e.value)
    # Still a TimeoutError, so callers that caught asyncio.TimeoutError keep working.
    assert isinstance(e.value, TimeoutError) and isinstance(e.value, kit.CapError)


def test_run_missing_binary_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        asyncio.run(kit.run(["definitely-not-a-rook-binary"], 5))


# -- platform -----------------------------------------------------------------

@pytest.mark.parametrize("env,expect", [
    ({}, False),
    ({"TERMUX_VERSION": "0.118"}, True),
    ({"PREFIX": "/data/data/com.termux/files/usr"}, True),
    ({"PREFIX": "/usr"}, False),
])
def test_is_termux(monkeypatch, env, expect):
    for k in ("TERMUX_VERSION", "PREFIX"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert kit.is_termux() is expect
    if expect:
        assert kit.is_android()


def test_termux_fact(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "0.118")
    assert detect_facts()["termux"] is True
    monkeypatch.delenv("TERMUX_VERSION")
    monkeypatch.delenv("PREFIX", raising=False)
    facts = detect_facts()
    assert facts["termux"] is False and "termux" not in wire_facts(facts)  # absent == false


def test_which_finds_alternatives(tmp_path, monkeypatch):
    exe = tmp_path / ("tool-b.exe" if os.name == "nt" else "tool-b")
    exe.write_text("")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert kit.which("tool-a") is None
    assert Path(kit.which("tool-a", "tool-b")).name == exe.name


def test_which_tries_cmd_shims_on_windows(tmp_path, monkeypatch):
    (tmp_path / "claude.cmd").write_text("")
    monkeypatch.setattr(kit, "is_windows", lambda: True)
    monkeypatch.setattr(kit.shutil, "which",
                        lambda n: str(tmp_path / n) if (tmp_path / n).exists() else None)
    assert kit.which("claude") == str(tmp_path / "claude.cmd")


# -- argument checks ----------------------------------------------------------

def test_require_str():
    assert kit.require_str("  x ", "a") == "x"
    assert kit.require_str("  x ", "a", strip=False) == "  x "
    assert kit.require_str("", "a", empty=True) == ""
    for bad in (None, 3, "   "):
        with pytest.raises(kit.ArgError):
            kit.require_str(bad, "a")
    with pytest.raises(kit.ArgError, match="at most 2"):
        kit.require_str("abc", "a", max_len=2)


def test_require_int_and_opt_int():
    assert kit.require_int("42", "n") == 42 and kit.require_int(7.0, "n") == 7
    for bad in (True, 1.5, "x", None):
        with pytest.raises(kit.ArgError):
            kit.require_int(bad, "n")
    with pytest.raises(kit.ArgError, match="at least 1"):
        kit.require_int(0, "n", min=1)
    assert kit.opt_int(None, "n", 5) == 5 and kit.opt_int("6", "n", 5) == 6
    assert kit.require_choice("a", "c", ("a", "b")) == "a"
    with pytest.raises(kit.ArgError, match="one of a, b"):
        kit.require_choice("z", "c", ("a", "b"))


def test_failures_reach_the_caller_as_a_top_level_error():
    """fail() / ArgError come back as the reply's own ok:false + error, the
    shape every consumer already handles (no inner ok:false to unwrap)."""
    from rook.core.plugin import Plugin, capability

    class P(Plugin):
        NAMESPACE = "kit"

        @capability("boom")
        def boom(self) -> dict:
            kit.fail("disk is on fire")

        @capability("arg")
        def arg(self, n=None) -> dict:
            return {"n": kit.require_int(n, "n")}

    host = PluginHost(CapabilityRegistry(), check_placement=False)
    host.load([Candidate("kit", "test", lambda: P)])
    boom = asyncio.run(host.dispatch("kit.boom", {}))
    # The message is the whole error: no "CapError: " type-name prefix.
    assert boom == {"ok": False, "error": "disk is on fire"}
    arg = asyncio.run(host.dispatch("kit.arg", {"n": "x"}))
    assert arg == {"ok": False, "error": "bad args: n must be an integer"}


# -- the worker state directory -----------------------------------------------

def test_state_dir_honours_rook_worker_home(tmp_path, monkeypatch):
    from rook.core import paths
    from rook import paths as hub_paths
    monkeypatch.setenv("ROOK_WORKER_HOME", str(tmp_path / "w"))
    assert kit.state_dir() == paths.worker_state_dir() == tmp_path / "w"
    assert kit.state_path("a", "b") == tmp_path / "w" / "a" / "b"
    assert hub_paths.worker_state_dir() == tmp_path / "w"
    assert paths.worker_install_dir() == Path.home() / ".rook-band-worker"
    monkeypatch.delenv("ROOK_WORKER_HOME")
    assert kit.state_dir() == Path.home() / ".rook-band-worker"


def test_every_worker_state_file_follows_rook_worker_home(tmp_path):
    """Module-level paths are resolved at import, so check them in a fresh
    interpreter started with ROOK_WORKER_HOME (as a service would be)."""
    home = tmp_path / "state"
    probe = r"""
import json, os
from rook.worker import admin, audit, core, wconfig, enroll, session_mirror
from rook.worker.plugins import chat, msg, wake, selfupdate, work
from rook.core import authz
seen = {
  "worker_id": core._WORKER_ID_FILE, "plugins": admin._PLUGIN_STATE,
  "custom": admin._CUSTOM_STATE, "audit": audit._AUDIT_PATH,
  "config": wconfig._ACTIVE, "enrollment": enroll.storage_path(),
  "mirror": session_mirror.mirror_root(), "chat": chat._CHATDIR,
  "inbox": msg._INBOX, "wake": wake._STATE, "banned": selfupdate._BANNED,
  "hold": selfupdate._HOLD, "update_state": selfupdate._STATE,
  "su_worker_id": selfupdate._WORKER_ID_FILE, "work_db": work.WorkPlugin.db_path(),
}
print(json.dumps({k: str(v) for k, v in seen.items()}))
"""
    env = {**os.environ, "ROOK_WORKER_HOME": str(home), "PYTHONPATH": str(ROOT)}
    for var in ("ROOK_ENROLLMENT_FILE", "ROOK_WORK_DB"):
        env.pop(var, None)
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                         text=True, timeout=60, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    for key, value in json.loads(out.stdout).items():
        assert Path(value).is_relative_to(home), (key, value)


def test_no_hard_coded_worker_dir_left_in_worker_code():
    """Every worker-side path goes through rook.core.paths; only the installer
    (rook/remote/bootstrap.py) and the bundle location may name the dir."""
    def literals(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                yield n.value

    files = [*ROOT.joinpath("rook", "worker").rglob("*.py"), *ROOT.joinpath("rook", "core").glob("*.py"),
             ROOT / "android" / "app" / "src" / "main" / "python" / "worker_entry.py"]
    bad = []
    for f in files:
        if f.name == "paths.py":
            continue
        for s in literals(f):
            if s == ".rook-band-worker" or s.lstrip("~/").startswith(".rook-band-worker/"):
                bad.append(f"{f.relative_to(ROOT)}: {s!r}")
    assert not bad, bad


def test_kit_ships_with_rook_core():
    """The kit lives in rook.core, which both bundle builders copy whole."""
    for script in ("rook/remote/build_band_worker.py", "android/stage_worker.py"):
        assert '"core"' in (ROOT / script).read_text(encoding="utf-8")
    assert (ROOT / "rook" / "core" / "pluginkit.py").exists()
    assert (ROOT / "rook" / "core" / "paths.py").exists()


# -- plugins gated through settings, always exported ---------------------------

def _settings(plugin, stored):
    plugin.__dict__["_settings"] = SettingsView(plugin, stored)
    return plugin


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("CEC_HOST", "CEC_PORT", "CEC_TIMEOUT", "PIKVM_URL", "PIKVM_USER",
                "PIKVM_PASS", "PIKVM_INSECURE", "ROOK_WAKE_CMD", "ROOK_WAKE_AGENT",
                "ROOK_MEMORY_VAULT", "ROOK_DONGLE_PORT", "ROOK_PIKVM_URL", "ROOK_CEC_HOST",
                "ROOK_AGENT_WAKE_COMMAND", "ROOK_MEMORY_NOTES_DIR"):
        monkeypatch.delenv(var, raising=False)


def test_gated_plugins_always_export_for_schema_tooling(clean_env):
    from rook.hub.settings_schema import _plugin_classes
    from rook.worker.plugins import cec, hermes
    assert cec.PLUGIN is cec.CecPlugin and hermes.PLUGIN is hermes.HermesPlugin
    assert "cec" in {cls.NAMESPACE for _, cls in _plugin_classes("rook.worker.plugins")}


def test_cec_reads_settings_and_gates_on_host(clean_env, monkeypatch):
    from rook.worker.plugins import cec
    p = cec.CecPlugin()
    assert not p.available()
    _settings(p, {"host": "pico.lan", "port": 9000, "timeout": 1.5})
    assert p.available()
    got = []
    monkeypatch.setattr(cec, "_exchange", lambda line, *cfg: got.append((line, cfg)) or "pong 0 0")
    assert asyncio.run(p._ping())["ok"]
    assert got == [("ping", ("pico.lan", 9000, 1.5))]
    monkeypatch.setenv("CEC_HOST", "env.lan")     # the env var still wins
    assert p.settings.get("host") == "env.lan"


def test_pikvm_uses_settings_and_loads_only_when_configured(clean_env, monkeypatch):
    from rook.worker.plugins import pikvm
    p = pikvm.PiKvmPlugin()
    assert not p.available()
    _settings(p, {"url": "https://kvm.lan/", "user": "ops", "insecure": False})
    assert p.available()
    seen = []
    monkeypatch.setattr(pikvm, "_request_sync",
                        lambda cfg, *a: seen.append(cfg) or {"ok": True, "status": 200})
    asyncio.run(p._power_status())
    assert seen == [("https://kvm.lan", "ops", "admin", False)]   # password: old default
    monkeypatch.setenv("PIKVM_URL", "https://other")
    assert pikvm.PiKvmPlugin().available()


@pytest.mark.parametrize("raw,want", [
    ("kvm.lan", "https://kvm.lan"), ("kvm.lan:8443/", "https://kvm.lan:8443"),
    ("10.0.0.5", "https://10.0.0.5"), ("http://kvm.lan", "http://kvm.lan"),
    ("ftp://kvm.lan", None), ("https://", None), ("kvm.lan:notaport", None), ("ws://kvm", None),
])
def test_pikvm_url_without_a_scheme_is_https_and_garbage_is_refused(clean_env, monkeypatch, caplog,
                                                                      raw, want):
    """A scheme-less PIKVM_URL used to load the plugin (source "env") while the
    url check silently swapped in https://localhost."""
    import logging
    from rook.worker.plugins import pikvm
    monkeypatch.setenv("PIKVM_URL", raw)
    p = pikvm.PiKvmPlugin()
    with caplog.at_level(logging.WARNING, logger="rook.worker.plugins.pikvm"):
        assert p.available() is (want is not None)
    if want is None:
        assert any("not an http(s) URL" in r.getMessage() for r in caplog.records)
        return
    seen = []
    monkeypatch.setattr(pikvm, "_request_sync",
                        lambda cfg, *a: seen.append(cfg) or {"ok": True, "status": 200})
    asyncio.run(p._power_status())
    assert seen[0][0] == want


def test_wake_command_from_settings(clean_env, tmp_path, monkeypatch):
    from rook.worker.plugins import wake
    monkeypatch.setattr(wake, "_STATE", tmp_path / "wake-last.txt")
    p = wake.WakePlugin()
    assert not p.available()
    assert p._wake_info()["can_wake"] is False
    with pytest.raises(kit.CapError, match="no wake command"):
        p._wake(room="r1")
    _settings(p, {"wake_command": f"{sys.executable} -c pass", "wake_agent": "agent:x"})
    assert p.available() and p._wake_info() == {"ok": True, "can_wake": True,
                                                  "agent": "agent:x", "cmd_set": True}
    res = p._wake(room="r1")
    assert res["action"] == "woken" and res["agent"] == "agent:x"
    assert p._wake(room="r1")["action"] == "already-attending"


def test_memory_vault_from_settings(clean_env, tmp_path):
    from rook.worker.plugins import memory
    p = memory.MemoryPlugin()
    assert not p.available()
    _settings(p, {"notes_dir": str(tmp_path / "vault")})
    assert p.available()

    async def go():
        await p.start()
        try:
            assert p._put("shared/hello", "hi")["ok"]
            assert p._get("shared/hello")["text"] == "hi"
            with pytest.raises(kit.CapError, match="no such note"):
                p._get("shared/missing")
            with pytest.raises(kit.ArgError):
                p._note("a claim", kind="rumour")
        finally:
            await p.stop()
    asyncio.run(go())


def test_hermes_gates_in_available_and_logs(clean_env, monkeypatch, caplog):
    import logging
    from rook.worker.plugins import hermes
    monkeypatch.setattr(hermes, "_config_path", lambda: None)
    assert not hermes.HermesPlugin().available()
    monkeypatch.setattr(hermes, "_config_path", lambda: "/x/config.yaml")
    monkeypatch.setattr(hermes, "which", lambda name: "/bin/hermes")
    assert hermes.HermesPlugin().available()
    with caplog.at_level(logging.INFO, logger="rook.worker.plugins.hermes"):
        asyncio.run(hermes.HermesPlugin().start())
    assert "hermes plugin loaded" in caplog.text


def test_file_ops_raises_with_the_path(tmp_path):
    from rook.worker.plugins.file_ops import FilePlugin
    p = FilePlugin()
    with pytest.raises(kit.CapError, match="file not found: .*nope"):
        p._read(str(tmp_path / "nope"))
    with pytest.raises(kit.CapError, match="not a directory"):
        (tmp_path / "f").write_text("x")
        p._list(str(tmp_path / "f"))
    with pytest.raises(kit.ArgError):
        p._read(None)
    assert p._read(str(tmp_path / "f"))["content"] == "x"


def test_deluge_refuses_ids_that_could_inject_console_commands():
    from rook.worker.plugins import deluge
    assert deluge._torrent_id("*") == "*"
    assert deluge._torrent_id("abc123 def456") == "abc123 def456"
    for bad in ("abc; rm *", '"', "a'b", "a\\b", "line\nrm *", "--remove_data", ""):
        with pytest.raises(kit.CapError):
            deluge._torrent_id(bad)


def _console_split(command: str) -> list[list[str]]:
    """How deluge-console (2.x ConsoleCommandParser._split_args) reads its
    one command argument: split on ';' first, then shlex each piece."""
    import shlex
    return [shlex.split(part.strip()) for part in command.split(";")]


@pytest.mark.parametrize("name", ["Some Linux ISO", "x", "ubuntu-24.04*", "*1080p*",
                                  "Movie (2024) [1080p]", "a $HOME `id` b"])
def test_deluge_accepts_torrent_names_as_one_argument(name):
    """deluge-console matches torrent names as well as ids; a name with spaces
    reaches it as one argument of one command."""
    from rook.worker.plugins import deluge
    command = f"rm --remove_data {deluge._torrent_id(name)}"
    assert _console_split(command) == [["rm", "--remove_data", name]]


@pytest.mark.asyncio
async def test_deluge_pause_passes_a_name_to_the_console(monkeypatch):
    from rook.worker.plugins import deluge
    seen = []

    async def fake_console(command, timeout=30.0):
        seen.append(command)
        return 0, "ok", ""
    monkeypatch.setattr(deluge, "_console", fake_console)
    p = deluge.DelugePlugin()
    assert (await p._pause("Some Linux ISO"))["ok"]
    assert (await p._files("Some Linux ISO"))["torrent_id"] == "Some Linux ISO"
    assert _console_split(seen[0]) == [["pause", "Some Linux ISO"]]
    assert _console_split(seen[1]) == [["info", "-v", "Some Linux ISO"]]
    # add: a ';' inside the double quotes master used still split the command.
    path = 'C:\\Torrents\\it\'s "here".torrent'
    assert (await p._add(path))["ok"]
    assert _console_split(seen[2]) == [["add", path]]
    for bad in ("magnet:?xt=urn:btih:abc;rm *", "a\nrm *"):
        with pytest.raises(kit.CapError):
            await p._add(bad)
