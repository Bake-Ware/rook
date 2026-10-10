"""The jobs hub plugin (rook/hub/plugins/jobs): placement on worker "rook",
read/write caps and the band ceiling, the rook_jobs MCP tool and its actions,
creator identity, settings, masking on read, and an end-to-end run through a
real hub node (hub caps in process, hub tools through mcp_tools)."""
import asyncio
import json

import pytest

from rook.band_mcp import secret_mask
from rook.band_mcp.vault import Vault
from rook.hub.authz import current_principal, hub_cap_for_tool
from rook.hub.node import HubNode
from rook.hub.policy import Principal
from rook.hub.settings_store import SettingsStore

AGENT = Principal("token:agent_j", "token", "agent", label="jobs-test")
OPERATOR = Principal("token:static", "token", "operator", label="static")
JOB = {"name": "info", "entry": "a",
       "triggers": [{"kind": "cron", "expr": "0 3 * * *"}],
       "steps": {"a": {"kind": "cap", "worker": "rook", "cap": "hub.info", "on": {"success": ["b"]}},
                 "b": {"kind": "tool", "tool": "rook_jobs", "args": {"action": "list"}}}}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ROOK_JOBS", "ROOK_JOBS_DB", "ROOK_HUB_BAND_MAX_RISK", "ROOK_JOB_TIMEZONE"):
        monkeypatch.delenv(k, raising=False)


def node(tmp_path, **kw):
    return HubNode(str(tmp_path), entry_points=False, build_version="1.test.node",
                   settings_store=SettingsStore(tmp_path / "settings.db"), **kw)


def tool_for(n, principal=AGENT):
    async def invoke(cap, args):
        tok = current_principal.set(principal)
        try:
            return await n.invoke(cap, args, principal.id)
        finally:
            current_principal.reset(tok)
    tools = {t.__name__: t for t in n.plugin("job").mcp_tools(invoke)}

    async def call(**kw):
        return json.loads(await tools["rook_jobs"](**kw))
    return call


def test_loads_on_the_hub_with_its_caps(tmp_path):
    n = node(tmp_path)
    p = n.plugin("job")
    assert p is not None and p.NAME == "jobs"
    assert p.store.path == tmp_path / "plugins" / "job" / "jobs.db"
    reg = n.host.registry
    assert reg.meta("job.read").risk == "read" and reg.meta("job.write").risk == "write"
    m = p.manifest()
    assert m["placement"] == {"where": "is_hub", "run": "one"} and m["migrations"] == "migrations"
    assert "job." in "".join(n.guidance_defaults())


def test_off_without_a_state_dir_or_when_disabled(tmp_path, monkeypatch):
    assert HubNode(None, entry_points=False,
                   settings_store=SettingsStore(tmp_path / "s.db")).plugin("job") is None
    monkeypatch.setenv("ROOK_JOBS", "0")
    assert node(tmp_path).plugin("job") is None


@pytest.mark.asyncio
async def test_band_reaches_reads_only(tmp_path):
    n = node(tmp_path)
    r = await n.dispatch("job.read", {"action": "list"}, "agent:x", source="band")
    assert r["ok"] and r["result"]["jobs"] == []
    w = await n.dispatch("job.write", {"action": "create", "data": JOB}, "agent:x", source="band")
    assert not w["ok"] and "not callable over the band" in w["error"]
    # A read cap never runs a write action, so the ceiling holds.
    r = await n.dispatch("job.read", {"action": "delete", "id": "x"}, "agent:x", source="band")
    assert not r["ok"] and "is a write" in r["error"]


def test_mcp_tool_maps_to_caps_for_authorization():
    assert hub_cap_for_tool("rook_jobs", {}) == "job.read"
    assert hub_cap_for_tool("rook_jobs", {"action": "runs"}) == "job.read"
    assert hub_cap_for_tool("rook_jobs", {"action": "run"}) == "job.write"
    assert hub_cap_for_tool("rook_jobs", {"action": "create"}) == "job.write"


@pytest.mark.asyncio
async def test_tool_lifecycle(tmp_path):
    n = node(tmp_path)
    jobs = tool_for(n)
    bad = await jobs(action="validate", data={**JOB, "entry": "zz"})
    assert bad["ok"] and bad["result"]["valid"] is False and "entry: no step 'zz'" in bad["result"]["errors"]
    rejected = await jobs(action="create", data={**JOB, "entry": "zz"})
    assert not rejected["ok"] and rejected["code"] == "ValidationError" and rejected["errors"]

    made = await jobs(action="create", data=JOB)
    assert made["ok"], made
    job = made["result"]
    assert job["owner"] == "token:agent_j" and job["definition"]["overlap"]["max_queue"] is None
    assert job["next"].endswith(("-04:00", "-05:00"))      # hub zone, America/Toronto
    assert (await jobs(action="create", data=JOB))["error"] == "a job named 'info' already exists"

    listed = (await jobs())["result"]
    assert [j["name"] for j in listed["jobs"]] == ["info"] and listed["timezone"] == "America/Toronto"
    assert (await jobs(action="get", id="info"))["result"]["id"] == job["id"]
    nxt = (await jobs(action="next", id="info", data={"count": 3}))["result"]
    assert len(nxt["triggers"][0]["next"]) == 3 and nxt["triggers"][0]["next"][0]["local"].startswith("20")
    assert (await jobs(action="next"))["result"]["upcoming"][0]["job"] == "info"

    upd = await jobs(action="update", id="info", data={"description": "hub facts", "revision": 1})
    assert upd["ok"] and upd["result"]["revision"] == 2
    stale = await jobs(action="update", id="info", data={"description": "x", "revision": 1})
    assert not stale["ok"] and "changed since revision 1" in stale["error"]

    off = await jobs(action="disable", id="info", data={"reason": "testing"})
    assert off["result"]["enabled"] is False and off["result"]["paused_reason"] == "testing"
    assert "disabled" in (await jobs(action="run", id="info"))["error"]
    assert (await jobs(action="enable", id="info"))["result"]["enabled"] is True

    started = await jobs(action="run", id="info", data={"vars": {"x": 1}})
    assert started["ok"] and started["result"]["state"] == "due"
    p = n.plugin("job")
    await p.scheduler.tick()
    while p.scheduler.active:
        await asyncio.gather(*list(p.scheduler.active.values()))
    run = (await jobs(action="run_get", id=started["result"]["id"]))["result"]
    assert run["state"] == "success", run
    assert run["identity_used"] == "token:agent_j" and run["vars"] == {"x": 1}
    assert run["steps"]["a"]["output"]["name"] == "rook"
    assert [j["name"] for j in run["steps"]["b"]["output"]["jobs"]] == ["info"]  # a hub tool, as the job
    runs = (await jobs(action="runs", id="info"))["result"]["runs"]
    assert [r["state"] for r in runs] == ["success"] and "steps" not in runs[0]

    queued = (await jobs(action="run", id="info"))["result"]
    cancelled = (await jobs(action="cancel", id=queued["id"]))["result"]["cancelled"]
    assert cancelled == [{"id": queued["id"], "state": "cancelled", "cancel_requested": False}]

    gone = await jobs(action="delete", id="info")
    assert gone["ok"] and (await jobs(action="get", id="info"))["code"] == "KeyError"


@pytest.mark.asyncio
async def test_actions_and_errors(tmp_path):
    jobs = tool_for(node(tmp_path))
    schema = (await jobs(action="describe_schema"))["result"]
    assert schema["title"] == "Rook job" and "cap" in schema["$defs"]["kinds"]
    out = await jobs(action="guardrails_preview")
    assert not out["ok"] and "data.defaults is required" in out["error"]
    out = await jobs(action="set_guardrails", data={"defaults": {"deny": []}})
    assert not out["ok"] and out["code"] == "PermissionError"   # defaults are the operator's
    out = await jobs(action="explode")
    assert not out["ok"] and out["error"].startswith("Actions: list, get")
    assert (await jobs(action="get"))["error"] == "id is required (a job id or name)"
    assert (await jobs(action="run_get", id="r_nope"))["code"] == "KeyError"


@pytest.mark.asyncio
async def test_settings_read_and_admin_write(tmp_path):
    n = node(tmp_path)
    agent = tool_for(n)
    got = (await agent(action="settings"))["result"]["settings"]
    assert got["timezone"] == "America/Toronto" and got["retention_days"] == 30
    denied = await agent(action="settings", data={"timezone": "Europe/Paris"})
    assert not denied["ok"] and denied["code"] == "PermissionError"
    op = tool_for(n, OPERATOR)
    assert (await op(action="settings", data={"timezone": "Mars/Base"}))["error"] == "unknown time zone 'Mars/Base'"
    assert "unknown job setting" in (await op(action="settings", data={"colour": 1}))["error"]
    out = await op(action="settings", data={"timezone": "Europe/Paris"})
    assert out["ok"] and out["result"]["settings"]["timezone"] == "Europe/Paris"
    assert (await agent())["result"]["timezone"] == "Europe/Paris"


@pytest.mark.asyncio
async def test_run_output_is_masked_on_read(tmp_path):
    vault = Vault(str(tmp_path / "vault.db"))
    n = node(tmp_path, vault=vault)
    jobs = tool_for(n)
    job = (await jobs(action="create", data=JOB))["result"]
    run = (await jobs(action="run", id=job["id"]))["result"]
    store = n.plugin("job").store
    # Output stored before the secret existed is masked when shown.
    store._db.execute("UPDATE runs SET steps=? WHERE id=?",
                      (json.dumps({"a": {"state": "success", "output": "key=sup3r-secret-value"}}), run["id"]))
    vault.set("api", "sup3r-secret-value", "test", "test")
    before = secret_mask.installed()
    secret_mask.install(vault)
    try:
        shown = (await jobs(action="run_get", id=run["id"]))["result"]
        assert shown["steps"]["a"]["output"] == "key={{secret:api}}"
    finally:
        secret_mask._installed = before
