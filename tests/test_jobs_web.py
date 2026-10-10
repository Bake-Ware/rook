"""The Jobs page and the `rook band` jobs panel (docs/design/jobs.md 8).

* the bridge's ``/jobs/account-api`` (rook/hub/plugins/jobs/web.py): session
  + CSRF, JSON 401 when signed out, every action through job.read/job.write
  as the account, the editor's draft-trigger preview, error shapes, and the
  internal-token path the dashboard uses for the terminal panel;
* the dashboard's ``/account/jobs`` proxy and assets, and ``/api/band/jobs``
  (rook/remote/jobs_web.py);
* the TUI's jobs client and panel (rook/cli/band_tui.py).
"""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.error

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette

from rook.cli import band_tui as tui
from rook.hub.node import HubNode
from rook.hub.plugins.jobs import web as jobs_web
from rook.hub.plugins.jobs.service import NotAvailable
from rook.hub.settings_store import SettingsStore

ROOT = Path(__file__).resolve().parents[1]
JOB = {"name": "nightly", "entry": "a", "triggers": [{"kind": "cron", "expr": "0 3 * * *"}],
       "steps": {"a": {"kind": "noop"}}}
TOKEN = "t" * 64


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ROOK_JOBS", "ROOK_JOBS_DB", "ROOK_HUB_BAND_MAX_RISK", "ROOK_JOB_TIMEZONE"):
        monkeypatch.delenv(k, raising=False)


def _node(tmp_path):
    return HubNode(str(tmp_path), entry_points=False, build_version="1.test.node",
                   settings_store=SettingsStore(tmp_path / "settings.db"))


ACCOUNTS = SimpleNamespace(session=lambda c: {
    "admin": {"id": "u1", "username": "operator", "name": "Operator", "csrf": "k", "admin": True},
    "member": {"id": "u2", "username": "guest", "name": "Guest", "csrf": "m", "admin": False},
}.get(c))


def _client(get_node, token=TOKEN):
    app = Starlette(routes=jobs_web.routes(get_node, ACCOUNTS, token))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


ADMIN = {"Cookie": "rook_account=admin"}
MEMBER = {"Cookie": "rook_account=member"}


@pytest.mark.asyncio
async def test_signed_out_gets_json_401_and_the_page_boots(tmp_path):
    n = _node(tmp_path)
    async with _client(lambda: n) as c:
        r = await c.get(jobs_web.PATH)
        assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
        assert r.json() == {"error": "Sign in to use Jobs."} and "no-store" in r.headers["cache-control"]
        assert (await c.post(jobs_web.PATH, json={"action": "list"})).status_code == 401
        r = await c.get(jobs_web.PATH, headers=ADMIN)
        d = r.json()
        assert r.status_code == 200 and d["csrf"] == "k" and d["admin"] is True
        assert d["timezone"] == "America/Toronto" and d["principal"] == "human:u1"
        assert (await c.get(jobs_web.PATH, headers=MEMBER)).json()["admin"] is False


@pytest.mark.asyncio
async def test_actions_run_as_the_account_with_csrf(tmp_path):
    n = _node(tmp_path)
    async with _client(lambda: n) as c:
        body = {"action": "create", "data": JOB}
        r = await c.post(jobs_web.PATH, headers=ADMIN, json=body)
        assert r.status_code == 403 and "reload" in r.json()["error"]
        r = await c.post(jobs_web.PATH, headers=ADMIN, json=body | {"csrf": "k"})
        job = r.json()["result"]
        assert r.status_code == 200 and job["owner"] == "human:u1" and job["revision"] == 1
        # A member may read and run (default access "*").
        r = await c.post(jobs_web.PATH, headers=MEMBER, json={"csrf": "m", "action": "list"})
        assert [j["name"] for j in r.json()["result"]["jobs"]] == ["nightly"]
        r = await c.post(jobs_web.PATH, headers=MEMBER, json={"csrf": "m", "action": "run", "id": "nightly"})
        assert r.status_code == 200 and r.json()["result"]["state"] in ("due", "queued")
        # Update with a stale revision is refused; with the right one it saves.
        upd = {"csrf": "k", "action": "update", "id": job["id"], "data": {"description": "x", "revision": 1}}
        r = await c.post(jobs_web.PATH, headers=ADMIN, json=upd)
        assert r.status_code == 200 and r.json()["result"]["revision"] == 2
        r = await c.post(jobs_web.PATH, headers=ADMIN, json=upd)
        assert r.status_code >= 400 and r.json()["ok"] is False
        # Disable / enable / delete.
        for action in ("disable", "enable"):
            r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": action, "id": job["id"]})
            assert r.json()["result"]["enabled"] is (action == "enable")
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "delete", "id": job["id"]})
        assert r.status_code == 200
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "get", "id": job["id"]})
        assert r.status_code == 404 and r.json()["code"] == "KeyError"


@pytest.mark.asyncio
async def test_validation_errors_come_back_inline(tmp_path):
    n = _node(tmp_path)
    bad = {"name": "bad", "entry": "missing", "steps": {"a": {"kind": "nope"}}}
    async with _client(lambda: n) as c:
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "validate", "data": bad})
        v = r.json()["result"]
        assert r.status_code == 200 and v["valid"] is False and v["errors"]
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "create", "data": bad})
        d = r.json()
        assert r.status_code == 400 and d["code"] == "ValidationError" and d["errors"] == v["errors"]
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "describe_schema"})
        assert r.json()["result"]["title"] == "Rook job"


@pytest.mark.asyncio
async def test_cron_helper_previews_an_unsaved_trigger(tmp_path):
    n = _node(tmp_path)
    async with _client(lambda: n) as c:
        data = {"trigger": {"kind": "cron", "expr": "30 9 * * 1-5", "tz": "Europe/Paris"}, "count": 5}
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "next", "data": data})
        out = r.json()["result"]
        assert r.status_code == 200 and out["zone"] == "Europe/Paris" and out["timezone"] == "America/Toronto"
        assert len(out["next"]) == 5 and out["reads"]
        first = out["next"][0]
        assert "T09:30:00+0" in first["local"] and first["hub"].endswith(("-04:00", "-05:00"))
        bad = {"trigger": {"kind": "cron", "expr": "61 * * * *"}}
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "next", "data": bad})
        assert r.status_code == 400 and r.json()["ok"] is False
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "next",
                                                            "data": {"trigger": {"kind": "cron", "expr": "@daily",
                                                                                 "tz": "Mars/Base"}}})
        assert r.status_code == 400 and "Mars/Base" in r.json()["error"]


@pytest.mark.asyncio
async def test_settings_writes_are_for_the_operator(tmp_path):
    n = _node(tmp_path)
    async with _client(lambda: n) as c:
        r = await c.post(jobs_web.PATH, headers=MEMBER, json={"csrf": "m", "action": "settings"})
        assert r.status_code == 200 and r.json()["result"]["settings"]["timezone"] == "America/Toronto"
        r = await c.post(jobs_web.PATH, headers=MEMBER, json={"csrf": "m", "action": "settings",
                                                             "data": {"retention_days": 7}})
        assert r.status_code == 403 and r.json()["error"].startswith("denied:")
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "settings",
                                                            "data": {"timezone": "Europe/Paris"}})
        assert r.status_code == 200 and r.json()["result"]["settings"]["timezone"] == "Europe/Paris"
        assert (await c.get(jobs_web.PATH, headers=ADMIN)).json()["timezone"] == "Europe/Paris"


@pytest.mark.asyncio
async def test_guardrails_not_available_is_a_clear_501(tmp_path):
    assert jobs_web._status(NotAvailable("x")) == 501
    n = _node(tmp_path)
    async with _client(lambda: n) as c:
        r = await c.post(jobs_web.PATH, headers=ADMIN, json={"csrf": "k", "action": "guardrails_preview",
                                                            "data": {"deny": [], "allow": []}})
        # Until the guardrails workstream lands the action answers NotAvailable.
        if r.status_code != 200:
            assert r.status_code == 501 and r.json()["code"] == "NotAvailable"


@pytest.mark.asyncio
async def test_internal_token_path_for_the_terminal_panel(tmp_path):
    n = _node(tmp_path)
    who = json.dumps({"id": "human:dashboard", "role": "owner", "label": "dashboard"})
    async with _client(lambda: n) as c:
        h = {"Authorization": "Bearer " + TOKEN, "X-Rook-Principal": who}
        r = await c.post(jobs_web.PATH, headers=h, json={"action": "create", "data": JOB})
        assert r.status_code == 200 and r.json()["result"]["owner"] == "human:dashboard"
        r = await c.post(jobs_web.PATH, headers=h, json={"action": "settings", "data": {"retention_days": 9}})
        assert r.status_code == 200
        bad = dict(h, Authorization="Bearer " + "x" * 64)
        assert (await c.post(jobs_web.PATH, headers=bad, json={"action": "list"})).status_code == 401
        # Only a dashboard human can be forwarded, and never as system.
        for p in ({"id": "system:jobs", "role": "system"}, {"role": "owner"}, "nope"):
            hh = dict(h, **{"X-Rook-Principal": json.dumps(p)})
            assert (await c.post(jobs_web.PATH, headers=hh, json={"action": "list"})).status_code == 401
        member = dict(h, **{"X-Rook-Principal": json.dumps({"id": "human:u2", "role": "system"})})
        r = await c.post(jobs_web.PATH, headers=member, json={"action": "settings", "data": {"retention_days": 9}})
        assert r.status_code == 403
    async with _client(lambda: n, token=None) as c:
        h = {"Authorization": "Bearer " + TOKEN, "X-Rook-Principal": who}
        assert (await c.post(jobs_web.PATH, headers=h, json={"action": "list"})).status_code == 401


@pytest.mark.asyncio
async def test_no_jobs_plugin_is_503(tmp_path):
    async with _client(lambda: None) as c:
        r = await c.get(jobs_web.PATH, headers=ADMIN)
        assert r.status_code == 503 and r.json()["error"] == jobs_web.UNAVAILABLE


# -- the dashboard side ----------------------------------------------------------

@pytest.fixture
def portal(tmp_path, monkeypatch):
    from rook.remote.account_web import AccountWeb
    from rook.remote.enrollment import EnrollmentStore
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.delenv("ROOK_GOOGLE_CLIENT_FILE", raising=False)
    enrollment = EnrollmentStore(tmp_path / "enrollment.db")
    enrollment.register("Home", "original-key", "hub.example.com:443", primary=True)
    server = SimpleNamespace(_enrollment=enrollment, web_user="operator", web_pass="operator-password",
                             domain="rook.example.com", hub_public="hub.example.com:443", _band=None,
                             _ban_match=lambda *a: False)
    account = AccountWeb(server)
    app = web.Application()
    account.install(app)
    return app


@pytest.mark.asyncio
async def test_dashboard_page_assets_and_signed_out_json(portal):
    async with TestClient(TestServer(portal)) as client:
        r = await client.get("/account/jobs/api", allow_redirects=False)
        assert r.status == 401 and (await r.json())["error"]
        r = await client.get("/account/jobs/api", headers={"Sec-Fetch-Mode": "navigate"}, allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/account/login"
        for name in ("jobs.js", "jobs.css"):
            r = await client.get("/account/jobs/assets/" + name)
            assert r.status == 200 and "no-cache" in r.headers["Cache-Control"]
        assert (await client.get("/account/jobs/assets/other.js")).status == 404


@pytest.mark.asyncio
async def test_band_jobs_forwards_the_admitted_principal(tmp_path, monkeypatch):
    from rook.hub.authz import current_principal, principal_for_user
    from rook.remote.jobs_web import JobsWeb
    seen = []

    async def upstream(request):
        seen.append((dict(request.headers), await request.json()))
        return web.json_response({"ok": True, "result": {"jobs": []}})
    up = web.Application()
    up.router.add_post("/jobs/account-api", upstream)
    async with TestServer(up) as ts:
        principal = {"p": None}

        @web.middleware
        async def admit(request, handler):
            tok = current_principal.set(principal["p"])
            try:
                return await handler(request)
            finally:
                current_principal.reset(tok)
        app = web.Application(middlewares=[admit])
        jw = JobsWeb(SimpleNamespace(server=SimpleNamespace()))
        jw.url = str(ts.make_url("/jobs/account-api"))
        jw.install(app)
        token_file = tmp_path / "mask.token"
        monkeypatch.setenv("ROOK_MASK_TOKEN_FILE", str(token_file))
        monkeypatch.setattr(jw, "_token_paths", lambda: [str(token_file)])
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/api/band/jobs", json={"action": "list"})
            assert r.status == 401
            principal["p"] = principal_for_user(None, True)
            r = await client.post("/api/band/jobs", json={"action": "list"})
            assert r.status == 503   # no token file yet
            token_file.write_text(TOKEN)
            r = await client.post("/api/band/jobs", json={"action": "list", "csrf": "ignored", "id": None})
            assert r.status == 200 and (await r.json())["ok"]
    headers, body = seen[-1]
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert json.loads(headers["X-Rook-Principal"]) == {"id": "human:dashboard", "role": "owner",
                                                       "label": "dashboard"}
    assert body == {"action": "list", "id": None, "query": None, "data": None}


def test_index_has_the_jobs_view():
    html = (ROOT / "rook/web/index.html").read_text(encoding="utf-8")
    assert 'id="tab-jobs"' in html and 'id="view-jobs"' in html
    assert "/account/jobs/assets/jobs.js" in html and "jobsUI?.deactivate()" in html
    js = (ROOT / "rook/web/jobs.js").read_text(encoding="utf-8")
    # Never JSON.parse a page that is not JSON (a signed-out redirect).
    assert "content-type" in js and "application/json" in js
    assert "/account/jobs/api" in js and "guardrails_preview" in js and "set_guardrails" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_cron_words_and_builder(tmp_path):
    src = (ROOT / "rook/web/jobs.js").read_text(encoding="utf-8")
    first = src.index("\n", src.index("import {h"))
    (tmp_path / "jobs.mjs").write_text("const h=()=>{},ago=0,useCss=0,btn=0,chips=0,table=0,two=0,dialog=0,menu=0;"
                                       + src[first:], encoding="utf-8")
    (tmp_path / "t.mjs").write_text(
        "import {describeCron,buildCron} from './jobs.mjs';\n"
        "console.log(JSON.stringify([describeCron('*/15 * * * *'),describeCron('0 9 * * 1-5'),"
        "describeCron('@daily'),buildCron({mode:'weekly',time:'07:30',days:[1,3,5]}),"
        "buildCron({mode:'hours',n:3,minute:15}),buildCron({mode:'monthly',dom:40,time:'1:05'})]));\n",
        encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == ["Every 15 minutes.", "At 09:00 on Monday through Friday.", "At 00:00 every day.",
                   "30 7 * * 1,3,5", "15 */3 * * *", "5 1 31 * *"]


# -- the terminal panel ------------------------------------------------------------

def test_band_http_jobs_client(monkeypatch):
    band = tui.BandHTTP("https://example.com", "test", "test")
    req = Mock(return_value={"ok": True, "result": {"jobs": []}})
    monkeypatch.setattr(band, "_req", req)
    assert band.jobs("list", data={"limit": 5})["ok"]
    req.assert_called_once_with("/api/band/jobs", "POST",
                                {"action": "list", "id": None, "query": "", "data": {"limit": 5}}, timeout=30)
    req.side_effect = urllib.error.HTTPError("u", 404, "missing", {}, None)
    assert "no jobs API" in band.jobs("list")["error"]

    class Err(urllib.error.HTTPError):
        def read(self):
            return b'{"ok": false, "error": "denied: nope", "code": "PermissionError"}'
    req.side_effect = Err("u", 403, "Forbidden", {}, None)
    r = band.jobs("settings", data={"timezone": "UTC"})
    assert r == {"ok": False, "error": "denied: nope", "code": "PermissionError"}
    req.side_effect = None
    req.return_value = {"error": "Sign in to use Jobs."}
    assert band.jobs("list") == {"ok": False, "error": "Sign in to use Jobs."}


def test_jobs_panel_lists_runs_now_and_toggles():
    job = {"id": "j_1", "name": "nightly", "enabled": True, "triggers": ["cron 0 3 * * *"],
           "next": "2026-10-11T03:00:00-04:00", "last_run": {"id": "r_0", "state": "success"}}
    run = {"id": "r_1", "job": "nightly", "state": "failure", "trigger": "manual", "missed": False,
           "started": "2026-10-10T08:00:00-04:00", "finished": "2026-10-10T08:01:05-04:00",
           "steps": {"a": {"state": "failure", "attempts": 2, "exit_code": 1, "output": "boom",
                           "started": "2026-10-10T08:00:00-04:00", "finished": "2026-10-10T08:01:00-04:00"}}}
    calls = []

    def jobs(action, id=None, data=None, query=""):
        calls.append((action, id))
        return {"list": {"ok": True, "result": {"jobs": [job], "timezone": "America/Toronto"}},
                "run": {"ok": True, "result": {"id": "r_2", "state": "due"}},
                "disable": {"ok": True, "result": dict(job, enabled=False)},
                "runs": {"ok": True, "result": {"runs": [run]}},
                "get": {"ok": True, "result": {"revision": 3, "definition": {"name": "nightly"}}},
                }[action]
    ui = tui.UI(SimpleNamespace(overview=lambda: {"workers": [], "chats": []}, jobs=jobs), "test")
    try:
        ui.draw = lambda scr: None
        picks = iter([0,            # job list -> nightly
                      1,            # run now
                      2, 1,         # disable -> confirm yes
                      0, 0, None,   # runs -> the run -> back
                      3,            # view JSON
                      None, None])  # back out of the menu and the list
        titles, popups = [], []
        ui.picker = lambda scr, title, items: (titles.append((title, items)), next(picks))[1]
        ui.popup = lambda scr, title, text: popups.append((title, text))
        ui.act_jobs(None)
    finally:
        ui.close()
    assert [c[0] for c in calls] == ["list", "run", "disable", "runs", "get", "list"]
    assert titles[0][0] == "jobs (1) · times in America/Toronto"
    assert titles[0][1][0].startswith("[x] nightly") and "10-11 03:00-04:00" in titles[0][1][0]
    assert "enable" in titles[4][1]   # after disabling, the menu offers enable
    assert popups[0] == ("run now", "queued run r_2 (due)")
    run_text = popups[1][1]
    assert "failure" in run_text and "boom" in run_text and "exit 1" in run_text and "1m00s" in run_text
    assert popups[2][0] == "nightly · revision 3" and '"name": "nightly"' in popups[2][1]


def test_jobs_panel_shows_a_refusal():
    ui = tui.UI(SimpleNamespace(overview=lambda: {"workers": [], "chats": []},
                                jobs=lambda *a, **k: {"ok": False, "error": "Sign in to use Jobs."}), "test")
    try:
        ui.draw = lambda scr: None
        popups = []
        ui.popup = lambda scr, title, text: popups.append(text)
        ui.act_jobs(None)
    finally:
        ui.close()
    assert popups == ["Sign in to use Jobs."]
