"""The `rook` command on a worker machine (rook.worker.localcli) and the
running worker's local control link (rook.worker.control)."""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from rook.cli import band_tui
from rook.worker import admin, control, localcli


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A worker state dir and a dashboard-login file of our own."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROOK_WORKER_HOME", str(state))
    monkeypatch.delenv("ROOK_ENROLLMENT_FILE", raising=False)
    for var in ("ROOK_WEB_URL", "ROOK_WEB_USER", "ROOK_WEB_PASS", "ROOK_BAND_PSK", "ROOK_HUB",
                "ROOK_TOKEN", "ROOK_MCP_URL",
                "ROOK_SERVER", "ROOK_YES", "ROOK_BAND", "ROOK_INSTALLER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(band_tui, "_CONF", tmp_path / "band.conf")
    monkeypatch.setattr(localcli, "token_file", lambda: tmp_path / "cli-token.json")
    monkeypatch.setattr(admin, "_WORKER_DIR", state)
    monkeypatch.setattr(admin, "_PLUGIN_STATE", state / "plugins.json")
    return state


def enroll_file(state, **extra):
    data = {"server": "http://192.0.2.10:7005", "active_band": "b1",
            "bands": [{"id": "b1", "name": "home", "hub": "192.0.2.10:7005", "psk": "x", "epoch": 1}],
            "device": {"device_id": "d" * 32, "certificate": "c", "private_key": "k",
                       "expires_at": time.time() + 30 * 86400},
            "last_verified": time.time(), **extra}
    (state / "enrollment.json").write_text(json.dumps(data))
    os.chmod(state / "enrollment.json", 0o600)
    return data


def run(argv, capsys):
    code = localcli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


# -- status / doctor -----------------------------------------------------------------

def test_status_before_pairing(home, capsys):
    code, out, _ = run(["status"], capsys)
    assert code == 0
    assert "not running" in out and "not joined yet (rook pair <code>)" in out


def test_status_json_reads_enrollment_and_update_result(home, capsys):
    enroll_file(home)
    (home / "update_result.json").write_text(json.dumps(
        {"result": "ok", "build": 512, "check": "band round trip", "at": time.time() - 7200}))
    (home / "plugins.json").write_text(json.dumps({"disabled": ["hid"]}))
    code, out, _ = run(["status", "--json"], capsys)
    st = json.loads(out)
    assert st["band"]["name"] == "home" and st["band"]["enrolled"] is True
    assert st["plugins"] == {"disabled": ["hid"]}
    code, out, _ = run(["status"], capsys)
    assert "home via 192.0.2.10:7005" in out
    assert "build 512 confirmed healthy 2 h ago (band round trip)" in out
    assert "device certificate good for" in out


def _probe(skew=0.0, error=None):
    return lambda url: (None if error else time.time() - skew, error, 0.05)


def test_doctor_says_how_to_pair_and_start(home, capsys, monkeypatch):
    monkeypatch.setattr(localcli, "_http_probe", _probe())
    checks = {c.name: c for c in localcli.run_checks(probe=_probe(), tcp=lambda h, p: None)}
    assert checks["Band membership"].status == "fail"
    assert "rook pair <code>" in checks["Band membership"].fix
    assert checks["Worker running"].status == "fail"
    assert checks["Worker running"].fix.startswith("Start it")
    code, out, _ = run(["doctor"], capsys)
    assert code == 1 and "FAIL" in out and "Fix: " in out and "2 problems to fix" in out


def test_doctor_clock_skew_hub_down_and_ban(home):
    enroll_file(home)
    c = {x.name: x for x in localcli.run_checks(probe=_probe(skew=400), tcp=lambda h, p: None)}
    assert c["Clock"].status == "fail" and "ahead of the hub" in c["Clock"].detail
    c = {x.name: x for x in localcli.run_checks(probe=_probe(skew=-90), tcp=lambda h, p: None)}
    assert c["Clock"].status == "warn" and "behind the hub" in c["Clock"].detail
    c = {x.name: x for x in localcli.run_checks(probe=_probe(skew=2), tcp=lambda h, p: None)}
    assert c["Clock"].status == "ok" and c["Band link"].status == "ok"
    c = {x.name: x for x in localcli.run_checks(
        probe=_probe(error="192.0.2.10 refused the connection"),
        tcp=lambda h, p: f"{h}:{p} refused the connection")}
    assert c["Hub reachable"].status == "fail" and "Clock" not in c
    assert c["Band link"].status == "warn"
    (home / "banned").write_text("{}")
    c = {x.name: x for x in localcli.run_checks(probe=_probe(), tcp=lambda h, p: None)}
    assert c["Band membership"].status == "fail" and "removed" in c["Band membership"].detail


def test_doctor_flags_readable_keys_and_expired_certificate(home):
    data = enroll_file(home)
    data["device"]["expires_at"] = time.time() - 10
    (home / "enrollment.json").write_text(json.dumps(data))
    if os.name == "posix":
        os.chmod(home / "enrollment.json", 0o644)
    c = {x.name: x for x in localcli.run_checks(probe=_probe(), tcp=lambda h, p: None)}
    assert c["Device key"].status == "fail" and "rook pair" in c["Device key"].fix
    if os.name == "posix":
        assert c["Key file"].status == "warn" and "chmod 600" in c["Key file"].fix


def test_doctor_names_live_band_trouble(home, monkeypatch):
    enroll_file(home)
    monkeypatch.setattr(localcli, "live_status", lambda: {
        "name": "kitchen-pc", "uptime_s": 30, "plugins": ["info"],
        "plugins_failed": [{"module": "hid", "reason": "no uinput"}],
        "band": {"connected": False, "last_heard_s": None, "hub": "192.0.2.10:7005",
                 "ws_url": "ws://192.0.2.10:7005/band"}})
    c = {x.name: x for x in localcli.run_checks(probe=_probe(), tcp=lambda h, p: None)}
    assert c["Worker running"].status == "ok"
    assert c["Band link"].status == "fail" and "ws://192.0.2.10:7005/band" in c["Band link"].detail
    assert c["Plugin"].status == "warn" and "hid" in c["Plugin"].detail


# -- the hub: ask, reads -------------------------------------------------------------

def test_ask_over_the_band_explains_the_ceiling(home, capsys, monkeypatch):
    monkeypatch.setattr(control, "request", lambda op, wait=10, **kw: {
        "ok": False, "error": "home.ask is not callable over the band on the hub (risk above "
                              "'read'); call it through the MCP bridge"})
    code, _, err = run(["ask", "hello?"], capsys)
    assert code == 1 and "rook login" in err and "band_max_risk" in err


def test_ask_without_worker_or_login(home, capsys):
    code, _, err = run(["ask", "hello?"], capsys)
    assert code == 1 and "worker isn't running" in err and "rook login" in err


def test_ask_through_the_saved_login(home, capsys, monkeypatch):
    band_tui._save_conf("http://192.0.2.10:7005", "admin", "pw")
    seen = {}

    def call(self, cap, worker_id=None, args=None, timeout=15.0):
        seen.update(cap=cap, worker=worker_id, args=args, url=self.url)
        return {"ok": True, "result": {"answer": "  The deck has 3 tasks.  ", "model": "m"}}
    monkeypatch.setattr(band_tui.BandHTTP, "call", call)
    code, out, _ = run(["ask", "what", "is", "on", "the", "deck?"], capsys)
    assert code == 0 and out == "The deck has 3 tasks.\n"
    assert seen == {"cap": "home.ask", "worker": "rook", "url": "http://192.0.2.10:7005",
                    "args": {"question": "what is on the deck?", "context": ""}}
    # --via band never uses the login.
    code, _, err = run(["ask", "--via", "band", "x"], capsys)
    assert code == 1 and "worker isn't running" in err


def test_ask_prefers_the_api_token(home, capsys, monkeypatch):
    from rook.worker import mcp_client
    band_tui._save_conf("http://192.0.2.10:7005", "admin", "pw")
    monkeypatch.setattr(band_tui.BandHTTP, "call", lambda *a, **k: pytest.fail("used the login"))
    (home.parent / "cli-token.json").write_text(json.dumps(
        {"url": "http://192.0.2.10:7005", "mcp_url": "http://192.0.2.10:7005/mcp", "token": "t0k"}))
    seen = {}

    def rook_call(url, token, cap, args, worker="rook", timeout=30.0):
        seen.update(url=url, token=token, cap=cap)
        return {"ok": True, "result": {"answer": "Yes."}}
    monkeypatch.setattr(mcp_client, "rook_call", rook_call)
    code, out, _ = run(["ask", "ok?"], capsys)
    assert code == 0 and out == "Yes.\n"
    assert seen == {"url": "http://192.0.2.10:7005/mcp", "token": "t0k", "cap": "home.ask"}
    code, out, _ = run(["status"], capsys)
    assert "Hub link  your API token" in out


def test_ask_through_the_login_hits_the_band_ceiling(home, capsys, monkeypatch):
    """The dashboard's call API sends hub calls over the band, so write-tier
    hub caps (home.ask) need the API token."""
    band_tui._save_conf("http://192.0.2.10:7005", "admin", "pw")
    monkeypatch.setattr(band_tui.BandHTTP, "call", lambda *a, **k: {
        "ok": False, "error": "home.ask is not callable over the band on the hub (risk above "
                              "'read'); call it through the MCP bridge"})
    code, _, err = run(["ask", "hi"], capsys)
    assert code == 1 and "rook login --token" in err and "Tokens page" in err


def test_login_token_is_checked_and_saved_privately(home, capsys, monkeypatch):
    from rook.worker import mcp_client
    enroll_file(home)
    monkeypatch.setenv("ROOK_TOKEN", "t0k")
    monkeypatch.setattr(mcp_client, "rook_call", lambda url, token, cap, args, worker="rook",
                        timeout=30.0: {"ok": True, "result": {"enabled": False}})
    restricted = []
    monkeypatch.setattr(control, "restrict_to_owner", restricted.append)
    code, out, _ = run(["login", "--token"], capsys)
    assert code == 0 and "Saved" in out
    assert restricted == [home.parent / "cli-token.json"]     # owner-only ACL on Windows
    saved = json.loads((home.parent / "cli-token.json").read_text())
    assert saved == {"url": "http://192.0.2.10:7005", "mcp_url": "http://192.0.2.10:7005/mcp",
                     "token": "t0k"}
    if os.name == "posix":
        assert (home.parent / "cli-token.json").stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(mcp_client, "rook_call", lambda *a, **k: (_ for _ in ()).throw(
        mcp_client.McpError("the hub refused the API token")))
    code, _, err = run(["login", "--token"], capsys)
    assert code == 1 and "refused the API token" in err


def test_login_defaults_a_bare_public_host_to_https():
    assert localcli._with_scheme("hub.example.com") == "https://hub.example.com"
    assert localcli._with_scheme("hub.example.com:7005") == "https://hub.example.com:7005"
    for lan in ("192.168.1.5:7005", "10.0.0.2", "100.100.1.2:7005", "[fd00::1]:7005",
                "localhost:7005", "rookhub:7005", "rookhub.local", "hub.lan:7005"):
        assert localcli._with_scheme(lan) == "http://" + lan, lan
    assert localcli._with_scheme("http://hub.example.com") == "http://hub.example.com"


class _Tty:
    def __init__(self, answer):
        self.answer = answer

    def isatty(self):
        return True

    def readline(self):
        return self.answer + "\n"


def test_login_never_sends_a_secret_over_public_http_unasked(home, capsys, monkeypatch):
    from rook.worker import mcp_client
    sent = []
    monkeypatch.setattr(mcp_client, "rook_call", lambda url, token, *a, **k: sent.append(url)
                        or {"ok": True, "result": {}})
    monkeypatch.setattr(band_tui.BandHTTP, "check", lambda self: sent.append(self) or None)
    monkeypatch.setenv("ROOK_TOKEN", "t0k")
    monkeypatch.setenv("ROOK_WEB_PASS", "pw")
    # No terminal: refused before anything is sent.
    for argv in (["login", "--token", "--url", "http://hub.example.com"],
                 ["login", "--token", "--url", "192.0.2.10:7005", "--mcp-url",
                  "http://hub.example.com/mcp"],
                 ["login", "--url", "http://hub.example.com"]):
        code, _, err = run(argv, capsys)
        assert code == 1 and "unencrypted" in err and "https://" in err, argv
    assert sent == []
    # At a terminal it asks; no means no.
    monkeypatch.setattr("sys.stdin", _Tty("n"))
    code, _, err = run(["login", "--token", "--url", "http://hub.example.com"], capsys)
    assert code == 1 and "Not sent" in err and sent == []
    monkeypatch.setattr("sys.stdin", _Tty("y"))
    code, _, _ = run(["login", "--token", "--url", "http://hub.example.com"], capsys)
    assert code == 0 and sent == ["http://hub.example.com/mcp"]
    # https and LAN addresses never ask.
    monkeypatch.setattr("sys.stdin", _Tty("n"))
    sent.clear()
    assert run(["login", "--token", "--url", "hub.example.com"], capsys)[0] == 0
    assert run(["login", "--token", "--url", "192.168.1.5:7005"], capsys)[0] == 0
    assert sent == ["https://hub.example.com/mcp", "http://192.168.1.5:7005/mcp"]


def test_chat_carries_earlier_turns(home, capsys, monkeypatch):
    asked = []

    def ask(q, context, via, timeout):
        asked.append((q, context))
        return f"answer {len(asked)}"
    monkeypatch.setattr(localcli, "_ask", ask)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("first\n\nsecond\n/exit\n"))
    code, out, _ = run(["chat"], capsys)
    assert code == 0 and "home> answer 2" in out
    assert asked[0] == ("first", "")
    assert "Person: first\nYou: answer 1" in asked[1][1]


def test_read_views(home, capsys, monkeypatch):
    replies = {
        "task.read": {"ok": True, "result": {"deck": [{"project": {"title": "Garden"},
                      "in_progress": [{"title": "Fix the gate", "slug": "fix-gate"}],
                      "todo": [{"title": f"Plant {i}", "slug": f"p{i}"} for i in range(10)]}]}},
        "chat.read": {"ok": True, "result": {"rooms": [{"room": "r1", "title": "Kitchen",
                      "unread": 2, "last_sender": "human:admin", "last_text": "dinner?"}]}},
        "knowledge.read": {"ok": True, "result": {"results": [
            {"title": "Router setup", "slug": "router", "excerpt": "The router  lives in the hall."}]}},
    }
    monkeypatch.setattr(localcli, "hub_call", lambda cap, args, timeout=20.0, via="auto":
                        (replies[cap], "band"))
    _, out, _ = run(["tasks"], capsys)
    assert "Garden" in out and "doing    Fix the gate  (fix-gate)" in out and "... and 3 more" in out
    _, out, _ = run(["rooms"], capsys)
    assert "Kitchen  2 new  (r1)" in out and "human:admin: dinner?" in out
    _, out, _ = run(["knowledge", "search", "router"], capsys)
    assert "Router setup  (router)" in out and "The router lives in the hall." in out
    replies["knowledge.read"] = {"ok": False, "error": "the knowledge store is not open"}
    code, _, err = run(["knowledge", "search", "x"], capsys)
    assert code == 1 and "not open" in err


# -- plugins ---------------------------------------------------------------------------

def test_plugins_offline_switches_persist(home, capsys):
    code, out, _ = run(["plugins", "disable", "hid"], capsys)
    assert code == 0 and "off when the worker starts" in out
    assert json.loads((home / "plugins.json").read_text())["disabled"] == ["hid"]
    code, out, _ = run(["plugins", "list"], capsys)
    assert "hid" in out and "switched off" in out and "isn't running" in out
    code, out, _ = run(["plugins", "enable", "hid"], capsys)
    assert json.loads((home / "plugins.json").read_text())["disabled"] == []


def test_plugins_respect_the_hubs_protected_set(home, capsys):
    for module in ("selfupdate", "config", "enrollment", "info"):
        code, _, err = run(["plugins", "disable", module], capsys)
        assert code == 1 and "stays on" in err
    code, _, err = run(["plugins", "disable", "no_such_plugin"], capsys)
    assert code == 1 and "No plugin called" in err


def test_plugins_live_go_through_the_running_worker(home, capsys, monkeypatch):
    sent = []

    def request(op, wait=10, **kw):
        sent.append((op, kw))
        if op == "status":
            return {"ok": True, "status": {"name": "x"}}
        if kw["cap"] == "worker.plugin.list":
            return {"ok": True, "result": {"plugins": [
                {"module": "hid", "loaded": False, "disabled": False, "reason": "no display"},
                {"module": "wake", "loaded": False, "disabled": True}]}}
        return {"ok": True, "result": {"ok": True, "module": kw["args"]["module"]}}
    monkeypatch.setattr(control, "request", request)
    enroll_file(home, plugin_profile={"disable": ["wake"]})
    _, out, _ = run(["plugins"], capsys)
    assert "unavailable  no display" in out
    assert "switched off by the band's plugin choices at pairing" in out
    code, out, _ = run(["plugins", "enable", "wake"], capsys)
    assert code == 0 and "on now" in out
    assert sent[-1] == ("call", {"cap": "worker.plugin.enable", "args": {"module": "wake"},
                                 "local": True})


# -- logs, pair, completion, dispatch ------------------------------------------------

def test_logs_from_the_running_worker(home, capsys, monkeypatch):
    monkeypatch.setattr(control, "request", lambda op, wait=10, **kw: {
        "ok": True, "lines": [{"seq": 1, "level": "WARNING", "text": "WS connect failed"}], "last": 1})
    code, out, _ = run(["logs"], capsys)
    assert code == 0 and out == "WS connect failed\n"


def test_logs_without_a_worker_or_service(home, capsys, monkeypatch):
    monkeypatch.setattr(localcli, "_service_log_command", lambda n: None)
    monkeypatch.setattr(localcli, "worker_install_dir", lambda: home / "nothing")
    code, _, err = run(["logs"], capsys)
    assert code == 1 and "rook doctor" in err


def test_pair_needs_a_hub_and_uses_the_enrollment(home, capsys, monkeypatch):
    code, _, err = run(["pair", "abc123"], capsys)
    assert code == 1 and "--hub" in err
    got = {}
    from rook.worker import enroll

    def fake(server, code, name=None, band=None, out=None):
        got.update(server=server, code=code, name=name)
        return {"worker": name, "band": "home"}
    monkeypatch.setattr(enroll, "enroll", fake)
    code, out, _ = run(["pair", "abc123", "--hub", "192.0.2.10:7005", "--name", "den"], capsys)
    assert code == 0 and got == {"server": "http://192.0.2.10:7005", "code": "abc123", "name": "den"}


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_completion_scripts(shell, capsys):
    code, out, _ = run(["completion", shell], capsys)
    assert code == 0 and "status" in out and "doctor" in out
    out.encode("ascii")
    if shell == "bash" and shutil.which("bash"):
        subprocess.run(["bash", "-n"], input=out, text=True, check=True)
        # Completing `rook pl` and `rook plugins dis` in a real bash.
        probe = out + ('COMP_WORDS=(rook pl); COMP_CWORD=1; _rook; echo "${COMPREPLY[@]}";'
                       'COMP_WORDS=(rook plugins dis); COMP_CWORD=2; _rook; echo "${COMPREPLY[@]}"')
        res = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True)
        assert res.stdout.split("\n")[:2] == ["plugins", "disable"]
    if shell == "powershell":
        assert "Register-ArgumentCompleter" in out


def test_both_launchers_route_local_commands(home, monkeypatch, capsys):
    from rook import __main__ as pkg_main
    from rook.worker import desktop
    for entry in (pkg_main.main, desktop.main):
        monkeypatch.setattr(sys, "argv", ["rook", "status", "--json"])
        with pytest.raises(SystemExit) as exc:
            entry()
        assert exc.value.code == 0
        assert json.loads(capsys.readouterr().out)["worker"]["running"] is False
    monkeypatch.setattr(sys, "argv", ["rook", "--help"])
    desktop.main()
    out = capsys.readouterr().out
    assert "doctor" in out and "rook worker --help" in out


def test_status_reports_the_running_workers_version(home, capsys, monkeypatch):
    monkeypatch.setattr(localcli, "live_status", lambda: {
        "name": "den", "version": "512.calm.otter", "build": 512, "uptime_s": 7200,
        "plugins": ["info"], "plugins_failed": [],
        "band": {"connected": True, "last_heard_s": 3, "ws": True, "hub": "192.0.2.10:7005"}})
    enroll_file(home)
    code, out, _ = run(["status"], capsys)
    assert "den  v512.calm.otter  (running for 2 h)" in out
    assert "home via 192.0.2.10:7005 (WebSocket)" in out and "connected, last heard 3 s ago" in out


def test_powershell_completion_completes(tmp_path, capsys):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is not installed")
    run(["completion", "powershell"], capsys)
    script = tmp_path / "comp.ps1"
    script.write_text(localcli.completion_script("powershell"))
    probe = (f". '{script}'; "
             "foreach ($s in @('rook pl', 'rook plugins dis', 'rook plugins disable hi')) {"
             " (TabExpansion2 -inputScript $s -cursorColumn $s.Length).CompletionMatches |"
             " % CompletionText }")
    res = subprocess.run([pwsh, "-NoProfile", "-Command", probe], capture_output=True,
                         text=True, timeout=120)
    assert res.stdout.split() == ["plugins", "disable", "hid"], (res.stdout, res.stderr)
