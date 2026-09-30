"""The settings framework end to end on an isolated test hub (opt-in).

* settings.* caps on worker "rook" through the MCP: source, locks, writes,
  history, live refresh of a hub plugin;
* the dashboard's Settings area through its account proxy: the dashboard's
  flags show as locks the MCP process could not see on its own;
* worker delivery: a worker-scoped setting and a worker secret pushed with a
  commit-confirmed restart; the worker fetches the secret from the hub at
  use (never on its disk) and masks it in env reads and config_get;
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.parse
from pathlib import Path

import pytest

SECRET = "it-secret-" + str(int(time.time()))


def _rook(hub, cap, **args):
    reply = hub.call("rook_call", cap=cap, worker="rook", args=args)
    assert isinstance(reply, dict) and reply.get("ok"), reply
    return reply["result"]


def test_settings_caps_through_mcp(hub):
    r = _rook(hub, "settings.get", key="core.mcp.listen")
    assert r["locked"] and r["env"] == "--bind", r            # the MCP's own flag
    res = _rook(hub, "settings.set", key="hub.motd", value="settings-it", note="it")
    assert res["ok"] and res["effective"]["source"] == "hub"
    assert _rook(hub, "hub.info")["motd"] == "settings-it"      # live, no restart
    hist = _rook(hub, "settings.history", key="hub.motd")
    assert hist[0]["new"] == "settings-it" and hist[0]["actor"].startswith("agent:")
    _rook(hub, "settings.reset", key="hub.motd")
    assert _rook(hub, "hub.info")["motd"] == ""
    bad = hub.call("rook_call", cap="settings.set", worker="rook",
                   args={"key": "core.mcp.listen", "value": "0.0.0.0:1"})
    assert not bad["ok"] and "environment" in json.dumps(bad)


def _login(hub) -> str:
    url = urllib.parse.urlparse(hub.env["ROOK_IT_DASHBOARD_URL"])
    secrets = Path(hub.env["ROOK_IT_DATA_DIR"]) / "secrets.env"
    pw = next(line.split("=", 1)[1] for line in secrets.read_text().splitlines()
              if line.startswith("ROOK_WEB_PASS="))
    c = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
    c.request("GET", "/account/login")
    r = c.getresponse()
    body = r.read().decode()
    form_cookie = re.search(r"rook_login_form=([^;]+)", r.getheader("Set-Cookie") or "").group(1)
    token = re.search(r'name="form_token" value="([^"]+)"', body).group(1)
    data = urllib.parse.urlencode({"form_token": token, "username": "operator", "password": pw})
    c.request("POST", "/account/login", body=data, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Cookie": f"rook_login_form={form_cookie}"})
    r = c.getresponse()
    r.read()
    m = re.search(r"rook_account=([^;]+)", r.getheader("Set-Cookie") or "")
    assert m, (r.status, r.getheader("Set-Cookie"))
    return m.group(1)


def _api(hub, cookie, params=None, body=None):
    url = urllib.parse.urlparse(hub.env["ROOK_IT_DASHBOARD_URL"])
    c = http.client.HTTPConnection(url.hostname, url.port, timeout=40)
    path = "/account/settings/api" + ("?" + urllib.parse.urlencode(params) if params else "")
    c.request("POST" if body is not None else "GET", path,
              body=json.dumps(body) if body is not None else None,
              headers={"Cookie": f"rook_account={cookie}", "Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_dashboard_settings_area(hub):
    if not hub.env.get("ROOK_IT_DASHBOARD_URL"):
        pytest.skip("test hub started without the dashboard")
    cookie = _login(hub)
    status, page = _api(hub, cookie, {"view": "hub"})
    assert status == 200, page
    rows = {r["key"]: r for g in page["groups"] for r in g["rows"]}
    domain = rows["core.hub.domain"]
    assert domain["locked"] and domain["env"] == "--domain"     # reported by the dashboard
    assert rows["core.dashboard.port"]["env"] == "--port"
    status, over = _api(hub, cookie)
    assert status == 200 and "mcp" in over["runtime"] and "dashboard" in over["runtime"]
    assert over["runtime"]["mcp"]["stores"]["chat_db"] == over["runtime"]["dashboard"]["stores"]["chat_db"]
    status, res = _api(hub, cookie, body={"csrf": page["csrf"], "action": "set",
                                          "key": "voice.whisper_model", "value": "base.en"})
    assert status == 200 and res["ok"], res
    status, hist = _api(hub, cookie, {"view": "history", "key": "voice.whisper_model"})
    assert hist["history"][0]["actor"] == "human:operator"
    _api(hub, cookie, body={"csrf": page["csrf"], "action": "reset", "key": "voice.whisper_model"})


def _wait(fn, timeout=90, every=3):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noqa: BLE001 - the worker restarts in between
            last = e
        time.sleep(every)
    raise AssertionError(f"timed out; last={last!r}")


def test_worker_settings_and_secret_at_use(hub):
    worker = hub.workers[0]
    _rook(hub, "settings.set", key="core.worker.log_level", value="info",
          scope="worker", target=worker)
    _rook(hub, "settings.set", key="pikvm.url", value="https://pikvm.invalid",
          scope="worker", target=worker)
    _rook(hub, "settings.set", key="pikvm.password", value=SECRET, scope="worker", target=worker)
    job = _rook(hub, "settings.apply_worker", worker=worker)
    assert job["ok"], job

    def confirmed():
        got = hub.call("rook_config_get", worker=worker)
        res = got.get("result") or {}
        cfg = res.get("config") or {}
        return got if (cfg.get("log_level") == "info" and not res.get("pending")) else None
    got = _wait(confirmed, timeout=150)
    text = json.dumps(got)
    assert SECRET not in text
    assert got["result"]["config"]["env"]["PIKVM_PASS"].startswith("{{secret:")

    def fetched():
        rep = hub.call("rook_call", cap="worker.settings_report", worker=worker)
        refs = (rep.get("result") or {}).get("secret_refs") or {}
        return rep if refs.get("PIKVM_PASS", {}).get("resolved") else None
    _wait(fetched, timeout=90)
    env = hub.call("rook_call", cap="shell.env.get", worker=worker, args={"name": "PIKVM_PASS"})
    assert env["result"] == "***"
    disk = hub.call("rook_call", cap="shell.exec", worker=worker,
                    args={"cmd": "cat ~/.rook-band-worker/config.json"})
    assert SECRET not in json.dumps(disk) and "{{secret:" in json.dumps(disk)

    # Undo: remove the keys and push again (they are sent as unset).
    for key in ("core.worker.log_level", "pikvm.url", "pikvm.password"):
        _rook(hub, "settings.reset", key=key, scope="worker", target=worker)
    _rook(hub, "settings.apply_worker", worker=worker)
    def unset():
        env = ((hub.call("rook_config_get", worker=worker).get("result") or {})
               .get("config") or {}).get("env") or {}
        return env.get("PIKVM_PASS", "missing") is None or None
    _wait(unset, timeout=150)
