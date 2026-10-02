"""Voice settings end to end on an isolated test hub (opt-in).

A fake voice process (``python -m services.voice.config --watch --json``: the
voice service's own settings loader, without the models) fetches its settings
from the hub with a minted agent token listed in
``core.settings.service_readers.voice``. An operator edits the Voice page of
the Settings UI (the dashboard's account API); the process picks the change up
on SIGHUP, reports its environment locks back to the hub, and on a restart
with the hub unreachable runs on its cached last-known-good copy, without
secrets.
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

from test_it_settings import _api, _login

REPO = Path(__file__).resolve().parents[2]
SECRET = "it-llm-key-" + str(int(time.time()))


def _mint_agent_token(hub, name: str) -> str:
    """Mint an agent token on the MCP's /tokens page (admin password login)."""
    url = urllib.parse.urlparse(hub.url)
    secrets = Path(hub.env["ROOK_IT_DATA_DIR"]) / "secrets.env"
    pw = next(line.split("=", 1)[1] for line in secrets.read_text().splitlines()
              if line.startswith("ROOK_MCP_AUTH_PASSWORD="))
    c = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
    c.request("POST", "/tokens/auth", body=urllib.parse.urlencode({"password": pw}),
              headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    m = re.search(r"rook_admin=([^;]+)", r.getheader("Set-Cookie") or "")
    assert m, (r.status, r.getheader("Set-Cookie"))
    c.request("POST", "/tokens/create", body=urllib.parse.urlencode({"name": name, "role": "agent"}),
              headers={"Content-Type": "application/x-www-form-urlencoded",
                       "Cookie": f"rook_admin={m.group(1)}"})
    r = c.getresponse()
    r.read()
    loc = r.getheader("Location") or ""
    shown = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("shown")
    assert shown, (r.status, loc)
    return shown[0]


def _rook(hub, cap, **args):
    reply = hub.call("rook_call", cap=cap, worker="rook", args=args)
    assert isinstance(reply, dict) and reply.get("ok"), reply
    return reply["result"]


class Voice:
    """The fake voice process: JSON line per load/refresh on stdout."""

    def __init__(self, env: dict, cache: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "services.voice.config", "--watch", "--json",
             "--interval", "600", "--cache", str(cache)],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.lines: queue.Queue = queue.Queue()
        self.log: list[str] = []
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, None), daemon=True).start()

    def _pump(self, stream, sink):
        for line in stream:
            if sink is not None:
                sink.put(line)
            else:
                self.log.append(line.rstrip())

    def next(self, timeout=60) -> dict:
        try:
            return json.loads(self.lines.get(timeout=timeout))
        except queue.Empty:
            raise AssertionError("fake voice process printed nothing; stderr:\n"
                                 + "\n".join(self.log[-30:])) from None

    def until(self, pred, timeout=60) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            ev = self.next(timeout=max(1, end - time.time()))
            if pred(ev):
                return ev
        raise AssertionError("condition not met; stderr:\n" + "\n".join(self.log[-30:]))

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("VOICE", "WHISPER_", "VLLM_", "ACP_", "MIN_", "MAX_NO_SPEECH",
                                 "ROOK_VOICE_", "ROOK_MCP_")))}
    env.update(PYTHONPATH=str(REPO), **extra)
    return env


def test_voice_fetches_hub_settings_and_follows_ui_edits(hub, tmp_path):
    if not hub.env.get("ROOK_IT_DASHBOARD_URL"):
        pytest.skip("test hub started without the dashboard")
    name = f"voice-it-{int(time.time())}"
    token = _mint_agent_token(hub, name)
    readers_before = _rook(hub, "settings.get", key="core.settings.service_readers")
    _rook(hub, "settings.set", key="core.settings.service_readers", value={"voice": [name]})
    cookie = _login(hub)
    status, page = _api(hub, cookie, {"view": "plugin", "target": "voice"})
    assert status == 200, page
    csrf = page["csrf"]
    assert page["service_readers"] == [name]

    def ui_set(key, value):
        status, res = _api(hub, cookie, body={"csrf": csrf, "action": "set", "key": key,
                                              "value": value})
        assert status == 200 and res.get("ok"), res

    cache = tmp_path / "voice-settings.json"
    voice = None
    try:
        ui_set("voice.assistant_name", "Ada")
        ui_set("voice.owner", "HubOwner")
        ui_set("voice.llm_api_key", SECRET)
        env = _env(ROOK_MCP_URL=hub.url, ROOK_MCP_TOKEN=token, ROOK_VOICE_OWNER="EnvOwner")
        voice = Voice(env, cache)
        first = voice.next()
        s = first["settings"]
        assert first["hub_ok"], first
        assert s["assistant_name"] == {"value": "Ada", "source": "hub"}
        assert s["owner"]["source"] == "env" and s["owner"]["value"] == "EnvOwner"
        assert s["llm_api_key"]["source"] == "hub" and SECRET not in json.dumps(first)
        assert s["whisper_model"]["source"] == "default"

        # The hub's Voice page shows the lock voice reported and its readers.
        def reported():
            status, page = _api(hub, cookie, {"view": "plugin", "target": "voice"})
            rows = {r["key"]: r for g in page["groups"] for r in g["rows"]}
            return page if (page.get("runtime") and rows["voice.owner"]["locked"]) else None
        end = time.time() + 30
        page = None
        while time.time() < end and not page:
            page = reported()
            time.sleep(1)
        assert page, "voice never reported its environment to the hub"
        rows = {r["key"]: r for g in page["groups"] for r in g["rows"]}
        assert rows["voice.owner"]["env"] == "ROOK_VOICE_OWNER"
        assert rows["voice.owner"]["conflict"]["hidden"] == "hub"
        assert page["runtime"]["reporter"] == name

        # Edit on the hub; voice picks it up when told to refresh.
        ui_set("voice.assistant_name", "Bea")
        ui_set("voice.whisper_model", "base.en")        # restart-apply: waits for a restart
        voice.proc.send_signal(signal.SIGHUP)
        ev = voice.until(lambda e: e["event"] == "refreshed"
                         and e["settings"]["assistant_name"]["value"] == "Bea")
        assert ev["settings"]["assistant_name"]["source"] == "hub"
        assert ev["settings"]["whisper_model"] == {"value": "small.en", "source": "default",
                                                   "pending": "restart"}
        voice.stop()

        # Restart with the hub unreachable: the cache supplies the last-known-good
        # values, secrets are not in it.
        assert SECRET not in cache.read_text()
        assert cache.stat().st_mode & 0o777 == 0o600
        voice = Voice(_env(ROOK_MCP_URL="http://127.0.0.1:9/mcp", ROOK_MCP_TOKEN=token), cache)
        off = voice.next()
        assert not off["hub_ok"] and off["error"]
        assert off["settings"]["assistant_name"] == {"value": "Bea", "source": "cache"}
        assert off["settings"]["whisper_model"] == {"value": "base.en", "source": "cache"}
        assert off["settings"]["llm_api_key"]["value"] is None
    finally:
        if voice is not None:
            voice.stop()
        for key in ("voice.assistant_name", "voice.owner", "voice.llm_api_key",
                    "voice.whisper_model"):
            _api(hub, cookie, body={"csrf": csrf, "action": "reset", "key": key})
        if readers_before.get("source") == "hub":
            _rook(hub, "settings.set", key="core.settings.service_readers",
                  value=readers_before["value"])
        else:
            _rook(hub, "settings.reset", key="core.settings.service_readers")
