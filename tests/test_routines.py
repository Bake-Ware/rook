"""Ready-made jobs (rook/hub/plugins/jobs/catalog.py) and the routines behind
them (rook/hub/plugins/routines, docs/design/routines.md): the template
catalog and install, the security scan, the attention digest and camera
review, each reporting quietly and only on change."""
from __future__ import annotations

import asyncio
import io
import json
import time
from types import SimpleNamespace

import pytest

from rook.band_mcp.journal import Journal
from rook.core.pluginkit import CapError
from rook.hub.authz import current_principal
from rook.hub.node import HubNode
from rook.hub.plugins.routines import cameras as cams
from rook.hub.plugins.routines.digest import compose, imap_new, is_mail
from rook.hub.policy import Principal
from rook.hub.settings_store import SettingsStore

OPERATOR = Principal("token:static", "token", "operator", label="static")
OWNER = Principal("human:bake", "human", "owner", ("human:owner",), label="Bake")
AGENT = Principal("token:agent_x", "token", "agent", label="some agent")
PHONE = "w-phone"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ROOK_JOBS", "ROOK_JOBS_DB", "ROOK_HUB_BAND_MAX_RISK", "ROOK_JOB_TIMEZONE"):
        monkeypatch.delenv(k, raising=False)


class FakeAccess:
    def __init__(self):
        self.rows = []

    def history(self, limit=50):
        return list(reversed(self.rows))[:limit]


class FakeTokens:
    def __init__(self):
        self.tokens = [{"id": "k1", "agent_id": "agent_a", "name": "laptop"}]

    def list_api_tokens(self):
        return list(self.tokens)


class FakeChat:
    def __init__(self):
        self.presence = {"agent:claude": time.time()}
        self.rooms = []

    def online(self):
        now = time.time()
        return [{"identity": i, "last_seen_age_secs": round(now - t, 1), "online": now - t < 90}
                for i, t in self.presence.items()]

    def rooms_for(self, identity, limit=50, include_all=False):
        return {"ok": True, "rooms": [{**r, "member": identity in r["participants"]} for r in self.rooms
                                      if include_all or identity in r["participants"]]}


class FakeConsoles:
    def __init__(self):
        self.live = []

    def list(self, state=None, limit=50):
        return {"ok": True, "rooms": list(self.live)}


class FakeBand:
    """A band client: workers, scripted worker caps, hub calls in process."""

    def __init__(self, node):
        self.node = node
        self.workers = {PHONE: {"name": "pixel", "last_seen": time.time(),
                                "caps": ["attention.post", "attention.update", "attention.poll",
                                         "attention.feed"]}}
        self.authz = SimpleNamespace(access=FakeAccess())
        self.caps = {}
        self.calls = []
        self.posted = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None, principal=None):
        self.calls.append((cap, target, identity))
        if target == self.node.worker_id:
            tok = current_principal.set(principal)
            try:
                body = await self.node.dispatch(cap, args or {}, identity)
            finally:
                current_principal.reset(tok)
            return body
        if cap == "attention.post":
            self.posted.append(dict(args))
            return {"ok": True, "result": {"ok": True, "id": args["id"]}}
        fn = self.caps.get(cap)
        if fn is None:
            return {"ok": False, "error": f"{cap}: not on {target}"}
        return {"ok": True, "result": fn(args or {}, target)}


@pytest.fixture
def hub(tmp_path):
    n = HubNode(str(tmp_path), entry_points=False, build_version="1.test.node",
                settings_store=SettingsStore(tmp_path / "settings.db"))
    band = FakeBand(n)
    n.client = band
    n.journal = Journal(str(tmp_path / "journal.db"))
    n.tokens = FakeTokens()
    n.chat = FakeChat()
    n.consoles = FakeConsoles()
    n.settings.set("attention.devices", ["pixel"], actor="human:op")
    tok = current_principal.set(OPERATOR)
    yield SimpleNamespace(node=n, band=band, plugin=n.plugin("routine"), tmp=tmp_path)
    current_principal.reset(tok)


def log_rows(hub):
    return hub.node.plugin("attention").service.eventlog.recent(50)


async def jobs(hub, principal=OPERATOR, **kw):
    async def invoke(cap, args):
        tok = current_principal.set(principal)
        try:
            return await hub.node.invoke(cap, args, principal.id)
        finally:
            current_principal.reset(tok)
    tool = {t.__name__: t for t in hub.node.plugin("job").mcp_tools(invoke)}["rook_jobs"]
    return json.loads(await tool(**kw))


# -- the catalog -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_templates_are_listed_and_none_is_installed_by_default(hub):
    out = await jobs(hub, action="templates")
    assert out["ok"], out
    rows = {t["template"]: t for t in out["result"]["templates"]}
    assert set(rows) == {"security-scan", "attention-digest", "camera-review"}
    assert rows["security-scan"]["zero_config"] and rows["security-scan"]["schedule"] == "*/5 * * * *"
    assert set(rows["camera-review"]["needs"]) == {"cameras", "provider"}
    assert all(t["available"] and t["installed"] == [] for t in rows.values())
    assert (await jobs(hub, action="list"))["result"]["jobs"] == []


@pytest.mark.asyncio
async def test_install_makes_an_ordinary_job_the_caller_owns(hub):
    made = await jobs(hub, action="install", id="security-scan")
    assert made["ok"], made
    job = made["result"]
    assert job["owner"] == "token:static" and job["enabled"] and job["template"] == "security-scan"
    d = job["definition"]
    assert d["steps"]["run"] == {**d["steps"]["run"], "kind": "cap", "worker": "rook",
                                 "cap": "routine.security_scan", "args": {}}
    assert d["overlap"]["mode"] == "skip" and d["vars"]["template"] == "security-scan"
    assert d["access"] == {"read": "human:owner", "edit": "human:owner", "run": "human:owner"}
    peek = await jobs(hub, principal=AGENT, action="get", id="security-scan")
    assert not peek["ok"] and peek["code"] == "PermissionError"   # its runs are owners' only
    assert not job["blocked_by_guardrail"]          # the default guardrails let it run
    again = await jobs(hub, action="install", id="security-scan")
    assert not again["ok"] and "already exists" in again["error"]
    other = await jobs(hub, action="install", id="security-scan",
                       data={"name": "scan-hourly", "schedule": "@hourly", "enabled": False,
                             "config": {"urgent_refusals": 0}})
    assert other["ok"] and other["result"]["enabled"] is False
    assert other["result"]["triggers"] == ["cron @hourly"]
    listed = {t["template"]: t for t in (await jobs(hub, action="templates"))["result"]["templates"]}
    assert {j["name"] for j in listed["security-scan"]["installed"]} == {"security-scan", "scan-hourly"}


@pytest.mark.asyncio
async def test_install_says_plainly_what_a_template_needs(hub):
    out = await jobs(hub, action="install", id="camera-review")
    assert not out["ok"] and "camera-review needs cameras:" in out["error"] and "provider:" in out["error"]
    out = await jobs(hub, action="install", id="security-scan", data={"config": {"cameras": []}})
    assert not out["ok"] and "does not take cameras" in out["error"]
    out = await jobs(hub, action="install", id="nope")
    assert not out["ok"] and "no template 'nope'" in out["error"]
    secret = {"cameras": [{"name": "porch", "url": "{{secret:porch-url}}"}], "provider": "lab"}
    out = await jobs(hub, action="install", id="camera-review", data={"config": secret})
    assert out["ok"], out
    # Placeholders stay placeholders in the saved job.
    assert out["result"]["definition"]["steps"]["run"]["args"]["cameras"][0]["url"] == "{{secret:porch-url}}"


@pytest.mark.asyncio
async def test_only_owners_install(hub):
    out = await jobs(hub, principal=AGENT, action="install", id="security-scan")
    assert not out["ok"] and out["code"] == "PermissionError" and "hub owners" in out["error"]
    assert (await jobs(hub, principal=AGENT, action="templates"))["ok"]


# -- the security scan ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_first_scan_is_a_silent_baseline_then_only_changes_are_told(hub):
    p = hub.plugin
    first = await p.security_scan()
    assert first["baseline"] and not first["changed"] and "delivered" not in first
    assert hub.band.posted == [] and log_rows(hub) == []
    quiet = await p.security_scan()
    assert not quiet["changed"] and hub.band.posted == []

    hub.band.workers["w-new"] = {"name": "stranger", "caps": []}
    hub.node.tokens.tokens.append({"id": "k2", "agent_id": "agent_b", "name": "new-laptop"})
    hub.node.chat.presence["agent:unknown"] = time.time()
    hub.node.consoles.live.append({"room": "c1", "title": "build the hub", "worker": "cachyrig"})
    hub.node.journal.record(cap="shell.exec", worker="kaiju", identity="agent:x", args=None,
                            reply={"ok": False, "error": "denied"}, authz={"decision": "deny"})
    hub.band.authz.access.rows.append({"ts": time.time(), "action": "grant", "actor": "human:bake",
                                       "user_id": "u2", "ref": "g1"})
    out = await p.security_scan()
    kinds = {f["kind"] for f in out["findings"]}
    assert kinds == {"workers", "keys", "agents", "consoles", "denied", "access"}, out["findings"]
    assert not out["urgent"] and out["delivered"]["phone"] and out["delivered"]["logged"]
    post, = hub.band.posted
    assert post["channel"] == "digest" and post["urgency"] == "normal" and post["kind"] == "alert"
    row, = log_rows(hub)
    assert row["outcome"] == "tell" and row["source"] == "security-scan"
    assert row["what"].startswith("Security scan: ") and "new-laptop" not in row["what"]
    again = await p.security_scan()
    assert not again["changed"] and len(hub.band.posted) == 1, again["findings"]


@pytest.mark.asyncio
async def test_a_burst_of_refused_keys_is_the_one_urgent_case(hub):
    p = hub.plugin
    await p.security_scan()
    for _ in range(3):
        hub.node.journal.record(cap="audit.denied", worker=None, identity="unauthenticated",
                                args=None, reply={"ok": False, "error": "bad token"})
    out = await p.security_scan(urgent_refusals=3)
    assert out["urgent"] and out["findings"][0]["level"] == "alert"
    assert hub.band.posted[-1]["urgency"] == "urgent" and hub.band.posted[-1]["channel"] == "urgent"
    assert log_rows(hub)[0]["outcome"] == "ask_now"
    for _ in range(3):
        hub.node.journal.record(cap="audit.denied", worker=None, identity="unauthenticated",
                                args=None, reply={"ok": False, "error": "bad token"})
    calm = await p.security_scan(urgent_refusals=0)
    assert calm["changed"] and not calm["urgent"]


@pytest.mark.asyncio
async def test_dry_run_saves_and_sends_nothing_and_missing_sources_are_skipped(hub):
    hub.node.consoles = None
    hub.node.accounts = None
    out = await hub.plugin.security_scan(dry_run=True)
    assert out["dry_run"] and out["baseline"]
    assert set(out["skipped"]) == {"consoles", "logins"}
    assert (await hub.plugin.security_scan(dry_run=True))["baseline"]   # nothing was saved
    assert hub.band.posted == []


@pytest.mark.asyncio
async def test_no_phone_set_up_still_logs_the_line(hub):
    hub.node.settings.set("attention.devices", [], actor="human:op")
    await hub.plugin.security_scan()
    hub.node.tokens.tokens.append({"id": "k9", "name": "x"})
    out = await hub.plugin.security_scan()
    assert out["delivered"]["phone"] is False and "attention.devices" in out["delivered"]["reason"]
    assert log_rows(hub)[0]["how"] == "logged only: no phone set up"


@pytest.mark.asyncio
async def test_routines_are_for_owners(hub):
    tok = current_principal.set(AGENT)
    try:
        for fn in (hub.plugin.security_scan, hub.plugin.attention_digest):
            with pytest.raises(CapError, match="hub owners"):
                await fn()
    finally:
        current_principal.reset(tok)
    tok = current_principal.set(OWNER)
    try:
        assert (await hub.plugin.security_scan())["baseline"]
    finally:
        current_principal.reset(tok)


# -- the attention digest ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_digest_folds_phones_chats_and_mail_into_one_quiet_message(hub):
    now = time.time()
    feed = [{"id": "1", "ts": now - 60, "app": "WhatsApp", "priority": "family", "summary": "Mom: dinner?"},
            {"id": "2", "ts": now - 50, "app": "WhatsApp", "priority": "normal", "summary": "Group: lol"},
            {"id": "3", "ts": now - 40, "app": "Gmail", "priority": "normal", "summary": "Bob: invoice"},
            {"id": "4", "ts": (now - 3 * 86400) * 1000, "app": "Old", "priority": "normal", "summary": "old"}]
    hub.band.caps["attention.feed"] = lambda args, wid: {"ok": True, "entries": feed}
    hub.node.chat.rooms = [{"room": "r1", "name": "brave-otter-lamp", "title": "Family", "unread": 3,
                            "last_activity_age_secs": 30, "last_sender": "telegram:mom",
                            "participants": ["user:operator"]},
                           {"room": "r2", "title": "stale", "unread": 1,
                            "last_activity_age_secs": 5 * 3600, "participants": ["user:operator"]},
                           {"room": "r3", "title": "Discord general", "unread": 0,
                            "last_activity_age_secs": 20, "last_sender": "discord:42",
                            "participants": ["agent:sojourn"]},
                           {"room": "r4", "title": "agents only", "unread": 0,
                            "last_activity_age_secs": 20, "last_sender": "agent:claude",
                            "participants": ["agent:claude"]}]
    out = await hub.plugin.attention_digest()
    assert out["changed"] and out["counts"] == {"notifications": 2, "email": 1, "chat_rooms": 2}, out
    post, = hub.band.posted
    assert post["channel"] == "digest" and post["urgency"] == "normal"
    row, = log_rows(hub)
    assert row["what"] == "Attention digest: 2 phone notifications, 1 email, 2 chat rooms"
    assert "Mom" not in row["what"] + row["why"]     # the log keeps counts, never content
    # The feed was read as the caller, journaled without its content.
    assert ("attention.feed", PHONE, "token:static") in hub.band.calls
    j = hub.node.journal.query(cap_prefix="attention.feed", include_reply=True)
    assert j and j[0]["reply"]["result"] == "[sensitive: not journaled]"
    for r in hub.node.chat.rooms:
        r["last_activity_age_secs"] = 9999
    nothing = await hub.plugin.attention_digest()
    assert not nothing["changed"] and len(hub.band.posted) == 1


def test_compose_reads_plainly():
    got = {"notifications": [{"app": "Signal", "text": "Ann: hi", "priority": "normal"}] * 2,
           "phone_mail": [], "email": [{"from": "Bob", "subject": "Invoice 7"}],
           "chats": [{"room": "rook-beta", "unread": 4, "from": "agent:x", "via": ""},
                     {"room": "Family", "unread": 0, "from": "discord:1", "via": "discord"}]}
    text, line, count = compose(got, since_label="2:00 PM")
    assert text == ("Since 2:00 PM:\nPhone: Signal 2 (Ann: hi)\nEmail (1): Bob: Invoice 7\n"
                    "Chats: rook-beta 4; Family new from discord:1 via discord")
    assert line == "Attention digest: 2 phone notifications, 1 email, 2 chat rooms" and count == 8
    assert compose({"notifications": [], "phone_mail": [], "email": [], "chats": []},
                   since_label="x") == ("", "", 0)
    assert is_mail("Gmail") and is_mail("com.microsoft.office.outlook") and not is_mail("Signal")


@pytest.mark.asyncio
async def test_email_from_a_worker_cap_and_imap_needs_resolved_secrets(hub):
    hub.band.workers["w-mail"] = {"name": "mailbox", "caps": ["cmd.mail"]}
    hub.band.caps["attention.feed"] = lambda args, wid: {"ok": True, "entries": []}
    hub.band.caps["cmd.mail"] = lambda args, wid: {"messages": [{"from": "Ann", "subject": "Hi"}]}
    out = await hub.plugin.attention_digest(email={"cap": {"worker": "mailbox", "cap": "cmd.mail"}})
    assert out["counts"]["email"] == 1 and out["errors"] == []
    with pytest.raises(ValueError, match="not resolved"):
        imap_new({"host": "h", "user": "u", "password": "{{secret:imap}}"}, 0)
    with pytest.raises(ValueError, match="host, user and password"):
        imap_new({"host": "h"}, 0)
    bad = await hub.plugin.attention_digest(email={"imap": {"host": "h"}}, dry_run=True)
    assert any(e.startswith("email: ValueError") for e in bad["errors"])


# -- camera review ---------------------------------------------------------------------------

def jpeg(shade: int, box: bool = False) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (320, 180), (shade, shade, shade))
    if box:
        ImageDraw.Draw(img).rectangle((100, 40, 220, 160), fill=(250, 250, 250))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class FakeModel:
    def __init__(self, reply):
        self.reply = reply
        self.asked = []
        self.model = "vision-1"

    async def chat(self, messages, **kw):
        self.asked.append(messages)
        return {"content": self.reply}


@pytest.fixture
def camera(hub, monkeypatch):
    frames = {"porch": jpeg(90)}

    async def http(url, headers, timeout):
        return frames[url.rsplit("/", 1)[-1]]
    hub.plugin.http = http
    model = FakeModel('{"level": "notable", "what": "A person left a package at the door."}')
    monkeypatch.setattr(hub.node.plugin("llm"), "client", lambda *a, **kw: model)
    hub.frames, hub.model = frames, model
    return hub


CAMS = [{"name": "Porch", "url": "http://cam.local/porch"}]


@pytest.mark.asyncio
async def test_cameras_ask_the_model_only_about_changed_frames(camera):
    p = camera.plugin
    first = await p.camera_review(cameras=CAMS, provider="lab")
    assert first["checked"] == [{"camera": "Porch", "diff": None}] and not first["changed"]
    same = await p.camera_review(cameras=CAMS, provider="lab")
    assert same["asked"] == 0 and camera.model.asked == [] and camera.band.posted == []
    camera.frames["porch"] = jpeg(90, box=True)
    out = await p.camera_review(cameras=CAMS, provider="lab")
    assert out["asked"] == 1 and out["events"] == [{"camera": "Porch", "level": "notable",
                                                     "what": "A person left a package at the door."}]
    content = camera.model.asked[0][1]["content"]
    assert [c["type"] for c in content] == ["text", "image_url", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    post, = camera.band.posted
    assert post["channel"] == "digest" and post["urgency"] == "normal"
    assert log_rows(camera)[0]["what"] == "Camera review: 1 notable event (Porch)"


@pytest.mark.asyncio
async def test_an_urgent_camera_event_rings_only_when_allowed(camera):
    p = camera.plugin
    camera.model.reply = 'Sure! {"level": "urgent", "what": "Smoke in the garage."}'
    await p.camera_review(cameras=CAMS, provider="lab")
    camera.frames["porch"] = jpeg(200)
    quiet = await p.camera_review(cameras=CAMS, provider="lab")
    assert quiet["events"][0]["level"] == "urgent" and not quiet["urgent"]
    assert camera.band.posted[-1]["urgency"] == "normal"
    camera.frames["porch"] = jpeg(30)
    loud = await p.camera_review(cameras=CAMS, provider="lab", may_ring=True)
    assert loud["urgent"] and camera.band.posted[-1]["urgency"] == "urgent"


@pytest.mark.asyncio
async def test_camera_sources_from_workers_and_their_errors(camera):
    camera.band.workers["w-nas"] = {"name": "nas", "caps": ["shell.exec", "camera.snap"]}
    import base64
    b64 = base64.b64encode(jpeg(60)).decode()
    seen = {}

    def shell(args, wid):
        seen["argv"] = args["argv"]
        return {"ok": True, "code": 0, "stdout": b64, "stderr": ""}
    camera.band.caps["shell.exec"] = shell
    camera.band.caps["camera.snap"] = lambda args, wid: {"ok": True, "data": b64}
    out = await camera.plugin.camera_review(cameras=[
        {"name": "garage", "worker": "nas", "rtsp": "rtsp://u:p@10.0.0.9/live"},
        {"name": "desk", "worker": "nas", "camera": 0},
        {"name": "gone", "worker": "offline-box", "camera": 0}], provider="lab")
    assert [c["camera"] for c in out["checked"]] == ["garage", "desk"]
    assert out["errors"] == ["gone: worker 'offline-box' is not on the band now"]
    # The stream address is an argument, never part of the shell text.
    assert seen["argv"][:2] == ["sh", "-c"] and seen["argv"][-1] == "rtsp://u:p@10.0.0.9/live"
    assert "rtsp://" not in seen["argv"][2]
    with pytest.raises(CapError, match="no camera answered"):
        await camera.plugin.camera_review(cameras=[{"name": "x", "worker": "nobody", "camera": 0}],
                                          provider="lab")


def test_camera_config_is_checked_plainly():
    for bad, msg in (([], "a list of cameras"), ([{"url": "http://x"}], "needs a name"),
                     ([{"name": "a", "url": "http://x", "camera": 0}], "exactly one of"),
                     ([{"name": "a", "rtsp": "rtsp://x"}], "needs the worker"),
                     ([{"name": "a", "url": "ftp://x"}], "must be http"),
                     ([{"name": "a", "url": "http://x"}, {"name": "A", "url": "http://y"}], "two cameras")):
        with pytest.raises(ValueError, match=msg):
            cams.check_cameras(bad)
    assert cams.parse_verdict("no json") == {"level": "nothing", "what": ""}
    assert cams.difference(b"\x00" * 4, b"\xff" * 4) == 1.0


# -- end to end through the jobs engine --------------------------------------------------------

@pytest.mark.asyncio
async def test_an_installed_scan_runs_as_its_creator(hub):
    made = (await jobs(hub, action="install", id="security-scan"))["result"]
    started = (await jobs(hub, action="run", id=made["id"]))["result"]
    sched = hub.node.plugin("job").scheduler
    await sched.tick()
    while sched.active:
        await asyncio.gather(*list(sched.active.values()))
    run = (await jobs(hub, action="run_get", id=started["id"]))["result"]
    assert run["state"] == "success", run
    assert run["identity_used"] == "token:static"
    assert run["steps"]["run"]["output"]["baseline"] is True
