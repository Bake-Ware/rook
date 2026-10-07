"""Hub routes of the Sessions page: POST /account/work/session/<op>
(docs/design/sessions.md §3.6). The worker is scripted; the browser side is
tests/browser_sessions.py."""
import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_band_management import portal  # noqa: F401 — fixture
from rook.remote import work_web

C1 = "6f1c0a52-0000-4000-8000-00000000a001"
X1 = "019a2b3c-0000-7000-8000-00000000c002"
TERM_CAPS = ["work.stream.open", "work.stream.read", "work.stream.write", "work.stream.close",
             "work.stream.list"]


class Band:
    def __init__(self):
        now = time.time()
        self.workers = {
            "w1": dict(worker_id="w1", name="new-host", band="test", last_seen=now,
                       caps=["sessions.list", "sessions.mirror", "sessions.follow", "sessions.send",
                             "sessions.stop", *TERM_CAPS],
                       hb={"work": {"harnesses": ["shell", "claude"]}}),
            "w2": dict(worker_id="w2", name="old-host", band="test", last_seen=now,
                       caps=["claude-history.follow", "claude-history.send", "claude-history.pull"]),
        }
        self.calls = []
        self.replies = {}
        self.hold = None

    async def call(self, cap, args, target, timeout, identity=None):
        self.calls.append((target, cap, dict(args), timeout, identity))
        if self.hold is not None and cap == "sessions.mirror":
            await self.hold.wait()
        reply = self.replies.get(cap, {"ok": True})
        if isinstance(reply, Exception):
            raise reply
        return {"ok": True, "from": target, "result": reply(args) if callable(reply) else reply}


@pytest.fixture
def page(portal):  # noqa: F811
    portal.server._band = Band()
    # Task writes go to the knowledge service's account API: recorded here,
    # never sent anywhere.
    portal.knowledge = []

    async def knowledge_call(request, user, payload):
        portal.knowledge.append(payload)
        return {"task": payload.get("id"), "claim": "cl_1"} if payload.get("action") == "claim" else {}
    portal.account.work_web.knowledge_call = knowledge_call
    return portal


def body(p, **kw):
    return {"csrf": p.csrf, "worker": "w1", "agent": "claude", "native_id": C1, **kw}


@pytest.mark.asyncio
async def test_auth_origin_csrf_and_unknown_ops(page):
    p = page
    async with TestClient(TestServer(p.app)) as client:
        assert (await client.post("/account/work/session/send", json=body(p, text="x"))).status == 401
        r = await client.post("/account/work/session/send", json=body(p, text="x"),
                              headers={**p.headers, "Origin": "https://evil.example"})
        assert r.status == 403
        r = await client.post("/account/work/session/send", json={**body(p, text="x"), "csrf": "nope"},
                              headers=p.headers)
        assert r.status == 403 and not (await r.json())["ok"]
        assert (await client.post("/account/work/session/explode", json=body(p), headers=p.headers)).status == 404
        r = await client.post("/account/work/session/send", data="[]", headers=p.headers)
        assert r.status == 400
        guest = p.store.create_local("guest", "long enough test password")
        token = p.store.new_session(guest)
        r = await client.post("/account/work/session/send", json=body(p, text="x"),
                              headers={"Cookie": "rook_account=" + token, "Origin": p.account.origin})
        assert r.status == 403
        # Unknown host, unknown agent, bad text.
        r = await client.post("/account/work/session/send", json=body(p, text="x", worker="w9"), headers=p.headers)
        assert r.status == 400 and "not connected" in (await r.json())["error"]
        r = await client.post("/account/work/session/send", json=body(p, text="x", agent="vim"), headers=p.headers)
        assert r.status == 400
        r = await client.post("/account/work/session/send", json=body(p, text=" "), headers=p.headers)
        assert r.status == 400
    # Nothing reached a host (history discovery runs on its own).
    assert [c for c in p.server._band.calls if not c[1].endswith(".pull")] == []


@pytest.mark.asyncio
async def test_mirror_relay_is_masked_bounded_and_long_polls(page, monkeypatch):
    p = page
    band = p.server._band
    events = [{"seq": 1, "type": "prompt", "text": "token sk-123"}, "junk"]
    band.replies["sessions.mirror"] = {"ok": True, "events": events, "cursor": 1, "done": False, "exists": True}

    async def mask(obj):
        import json
        return json.loads(json.dumps(obj).replace("sk-123", "{{secret:api}}"))
    p.server._bridge_mask = mask
    async with TestClient(TestServer(p.app)) as client:
        r = await client.post("/account/work/session/mirror", json=body(p, cursor=0, wait=99, max_events=9999),
                              headers=p.headers)
        out = await r.json()
        assert r.status == 200 and "no-store" in r.headers["Cache-Control"]
        assert out == {"ok": True, "events": [{"seq": 1, "type": "prompt", "text": "token {{secret:api}}"}],
                       "cursor": 1, "done": False, "exists": True}
        _, cap, args, timeout, identity = band.calls[-1]
        assert cap == "sessions.mirror" and args["wait"] == work_web.MIRROR_WAIT_MAX
        assert args["max_events"] == work_web.MIRROR_EVENTS_MAX and timeout > args["wait"]
        assert identity.startswith("human:")
        # Held long-polls are capped hub-wide.
        monkeypatch.setattr(work_web, "MIRROR_WATCHERS", 1)
        band.hold = asyncio.Event()
        first = asyncio.ensure_future(client.post("/account/work/session/mirror", json=body(p, cursor=1, wait=5),
                                                  headers=p.headers))
        for _ in range(50):
            if p.account.work_web.watching:
                break
            await asyncio.sleep(0.02)
        r = await client.post("/account/work/session/mirror", json=body(p, cursor=1, wait=5), headers=p.headers)
        assert r.status == 429
        # A non-waiting read is still allowed.
        band.hold.set()
        assert (await first).status == 200
        band.hold = None
        assert p.account.work_web.watching == 0
        # Host failures are 502 with the host's message.
        band.replies["sessions.mirror"] = {"ok": False, "error": "unsupported agent"}
        r = await client.post("/account/work/session/mirror", json=body(p), headers=p.headers)
        assert r.status == 502 and (await r.json())["error"] == "unsupported agent"
        # A worker without the cap is told to update.
        r = await client.post("/account/work/session/mirror", json=body(p, worker="w2"), headers=p.headers)
        assert r.status == 400 and "update its worker" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_follow_and_send_with_fallbacks_for_older_workers(page):
    p = page
    band = p.server._band
    band.replies["sessions.follow"] = {"ok": True, "version": "v1", "replace_from": 0, "messages": [
        {"index": 0, "content_offset": 0, "role": "user", "content": "hi"}], "truncated": False,
        "total_messages": 1, "agent": "claude", "native_id": C1, "snapshot": "tok"}
    band.replies["claude-history.follow"] = {"ok": True, "unchanged": True, "version": "v2"}
    band.replies["sessions.send"] = {"ok": True, "delivery": "held", "note": "Waiting", "detail": "queued",
                                     "native_id": C1}
    band.replies["claude-history.send"] = {"ok": True, "delivery": "accepted", "note": "Sent"}
    async with TestClient(TestServer(p.app)) as client:
        out = await (await client.post("/account/work/session/follow", json=body(p, offset=0, version=""),
                                        headers=p.headers)).json()
        assert out["messages"][0]["content"] == "hi" and out["version"] == "v1" and "snapshot" not in out
        assert band.calls[-1][1:3] == ("sessions.follow", {"agent": "claude", "native_id": C1, "offset": 0, "version": ""})
        out = await (await client.post("/account/work/session/follow", json=body(p, worker="w2", offset=3, version="v2"),
                                        headers=p.headers)).json()
        assert out == {"ok": True, "unchanged": True, "version": "v2"}
        assert band.calls[-1][1:3] == ("claude-history.follow", {"session_id": C1, "offset": 3, "version": "v2"})
        r = await client.post("/account/work/session/follow", json=body(p, agent="shell"), headers=p.headers)
        assert r.status == 400
        out = await (await client.post("/account/work/session/send", json=body(p, text="go on", command_id="cmd-1"),
                                        headers=p.headers)).json()
        assert out == {"ok": True, "delivery": "held", "note": "Waiting", "detail": "queued", "native_id": C1}
        assert band.calls[-1][2] == {"agent": "claude", "native_id": C1, "text": "go on", "command_id": "cmd-1"}
        out = await (await client.post("/account/work/session/send", json=body(p, worker="w2", text="go on"),
                                        headers=p.headers)).json()
        assert out["delivery"] == "turn" and out["detail"] == "accepted"
        assert band.calls[-1][1] == "claude-history.send" and band.calls[-1][2]["command_id"]
        band.replies["sessions.send"] = {"ok": False, "error": "This session is closed. Resume it first."}
        r = await client.post("/account/work/session/send", json=body(p, text="x"), headers=p.headers)
        assert r.status == 502 and "Resume it first" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_new_session_launches_a_terminal_with_task_and_token(page, monkeypatch):
    p = page
    band = p.server._band
    work = p.account.work_web
    band.replies["work.stream.open"] = {"ok": True, "id": "t7"}
    minted = []

    async def fake_token(request, user, payload):
        minted.append(payload)
        return {"id": "tokid", "token": "secret-token"}
    monkeypatch.setattr(work, "token_call", fake_token)
    req = dict(csrf=p.csrf, id="new-session-0001", worker="w1", harness="claude", cwd="C:\\src\\app",
               model="opus", persona="reviewer", task="t_0123abcd", mcp=True, cols=100, rows=30)
    async with TestClient(TestServer(p.app)) as client:
        r = await client.post("/account/work/session/new", json=req, headers=p.headers)
        out = await r.json()
        assert r.status == 200 and out["terminal"] == "t7" and out["title"] == "claude · app"
        s = work.store.get(out["session"], p.uid)
        assert s["task"] == "t_0123abcd" and s["term_running"] and s["harness"] == "claude"
        opened = next(c[2] for c in band.calls if c[1] == "work.stream.open")
        assert opened["cwd"] == "C:\\src\\app" and opened["mcp_token"] == "secret-token"
        assert opened["persona"] == "reviewer" and opened["session"] == out["session"]
        assert minted[0]["scopes"] == ["rook", "work-session:" + out["session"]]
        # The same command id is idempotent.
        again = await (await client.post("/account/work/session/new", json=req, headers=p.headers)).json()
        assert again["session"] == out["session"]
        assert sum(1 for c in band.calls if c[1] == "work.stream.open") == 1
        # Only harnesses the host reports; absolute folders only; task format.
        for bad in ({"harness": "codex"}, {"cwd": "relative/path"}, {"task": "no spaces please"}):
            r = await client.post("/account/work/session/new", json={**req, "id": "new-session-x" + str(len(bad)),
                                                                     "mcp": False, **bad}, headers=p.headers)
            assert r.status == 400
        # A host that fails to start reports 502.
        band.replies["work.stream.open"] = {"ok": False, "error": "claude is not installed"}
        r = await client.post("/account/work/session/new", json={**req, "id": "new-session-0002", "mcp": False},
                              headers=p.headers)
        assert r.status == 502 and "not installed" in (await r.json())["error"]
        # The task shows on the merged list through the Work session link.
        band.replies["sessions.list"] = {"ok": True, "harnesses": ["shell", "claude"], "total": 1, "items": [dict(
            agent="claude", native_id=C1, title="x", cwd="C:\\src\\app", state="live", origin="rook", updated=1,
            view={"terminal": "t7", "mirror": False, "transcript": True}, input="pty", inbox_policy="unknown",
            links={}, resumable=False)]}
        listed = await (await client.get("/account/work/sessions?worker=w1", headers=p.headers)).json()
        assert listed["sessions"][0]["links"] == {"work_session": out["session"], "task": "t_0123abcd"}


@pytest.mark.asyncio
async def test_a_session_for_a_task_claims_it_and_notes_its_end(page):
    """docs/design/sessions.md §4.F: New session with a task claims the task
    for the operator and links the terminal; Stop leaves a note asking for a
    handoff. Nothing closes the task."""
    p = page
    band = p.server._band
    work = p.account.work_web
    band.replies["work.stream.open"] = {"ok": True, "id": "t21"}
    band.replies["sessions.stop"] = {"ok": True, "stopped": "terminal", "terminal": "t21", "exit_code": 0}
    req = dict(csrf=p.csrf, id="task-session-0001", worker="w1", harness="shell", cwd="/srv/app", task="fix-login")
    async with TestClient(TestServer(p.app)) as client:
        out = await (await client.post("/account/work/session/new", json=req, headers=p.headers)).json()
        claim, link = p.knowledge[:2]
        assert claim["action"] == "claim" and claim["id"] == "fix-login" and claim["kind"] == "task"
        assert claim["data"] == {"provider_session": out["session"]}
        assert link["action"] == "link" and link["data"]["kind"] == "session"
        assert link["data"]["ref"] == "w1/shell/t21"
        s = work.store.get(out["session"], p.uid)
        assert s["task_claim"] == "claimed"
        # This host's terminals predate commands: the task is not sent to it.
        assert "task" not in next(c[2] for c in band.calls if c[1] == "work.stream.open")
        await client.post("/account/work/session/stop", json=body(p, agent="shell", native_id="t21"),
                          headers=p.headers)
        note = p.knowledge[-1]
        assert note["action"] == "note" and note["id"] == "fix-login"
        assert note["data"]["session_end"] == "w1/shell/t21" and "handoff" in note["data"]["text"]
        assert not work.store.get(out["session"], p.uid).get("task_end_pending")
        assert not any(k["action"] in ("update", "release") for k in p.knowledge)
        # A host whose terminals take commands records the task on the terminal.
        band.workers["w1"]["hb"]["work"]["commands"] = 1
        band.replies["work.stream.open"] = {"ok": True, "id": "t22"}
        await client.post("/account/work/session/new", json={**req, "id": "task-session-0002"}, headers=p.headers)
        assert [c[2].get("task") for c in band.calls if c[1] == "work.stream.open"][-1] == "fix-login"


@pytest.mark.asyncio
async def test_a_failed_claim_does_not_fail_the_launch_and_ends_are_retried(page):
    p = page
    work = p.account.work_web
    p.server._band.replies["work.stream.open"] = {"ok": True, "id": "t31"}
    calls = []

    async def down(request, user, payload):
        calls.append(payload)
        raise ValueError("Task service is unavailable.")
    work.knowledge_call = down
    req = dict(csrf=p.csrf, id="task-session-0003", worker="w1", harness="shell", cwd="/srv/app", task="t_9")
    async with TestClient(TestServer(p.app)) as client:
        out = await (await client.post("/account/work/session/new", json=req, headers=p.headers)).json()
        assert out["ok"] and work.store.get(out["session"], p.uid)["task_claim"] == "Task service is unavailable."
        async with work.lock(out["session"]):
            work.mark_term_done(out["session"], 1)
        user = {"id": p.uid, "csrf": p.csrf}
        await work.flush_task_ends(None, user, force=True)
        assert work.store.get(out["session"], p.uid)["task_end_pending"]   # kept for a later request
        sent = []

        async def up(request, user, payload):
            sent.append(payload)
            return {}
        work.knowledge_call = up
        await work.flush_task_ends(None, user, force=True)
        assert sent[0]["action"] == "note" and "exit code 1" in sent[0]["data"]["text"]
        assert not work.store.get(out["session"], p.uid)["task_end_pending"]


@pytest.mark.asyncio
async def test_resume_stop_attach_and_link(page):
    p = page
    band = p.server._band
    work = p.account.work_web
    band.replies["work.stream.open"] = {"ok": True, "id": "t8"}
    async with TestClient(TestServer(p.app)) as client:
        req = body(p, id="resume-cmd-0001", cwd="/srv/app", title="Release notes")
        out = await (await client.post("/account/work/session/resume", json=req, headers=p.headers)).json()
        assert out["ok"] and out["terminal"] == "t8"
        opened = next(c[2] for c in band.calls if c[1] == "work.stream.open")
        assert opened["resume"] == C1 and opened["harness"] == "claude" and opened["cwd"] == "/srv/app"
        s = work.store.get(out["session"], p.uid)
        assert s["imported"] and s["source_id"] == C1 and s["term_running"]
        # Resuming again while it runs returns the same terminal, without a second launch.
        again = await (await client.post("/account/work/session/resume", json={**req, "id": "resume-cmd-0002"},
                                          headers=p.headers)).json()
        assert again["session"] == out["session"]
        assert sum(1 for c in band.calls if c[1] == "work.stream.open") == 1
        r = await client.post("/account/work/session/resume", json=body(p, worker="w2", id="resume-cmd-0003"),
                              headers=p.headers)
        assert r.status == 400 and "Rook terminals" in (await r.json())["error"]
        r = await client.post("/account/work/session/resume", json=body(p, agent="shell", id="resume-cmd-0004"),
                              headers=p.headers)
        assert r.status == 400
        # Stop ends it on the host and closes the hub's Work session (token revoke follows).
        band.replies["sessions.stop"] = {"ok": True, "stopped": "terminal", "terminal": "t8", "exit_code": 0}
        out2 = await (await client.post("/account/work/session/stop", json=body(p), headers=p.headers)).json()
        assert out2 == {"ok": True, "stopped": "terminal", "terminal": "t8", "exit_code": 0}
        assert not work.store.get(out["session"], p.uid)["term_running"]
        band.replies["sessions.stop"] = {"ok": False, "error": "This session was started outside Rook."}
        r = await client.post("/account/work/session/stop", json=body(p), headers=p.headers)
        assert r.status == 502
        # Attach: a running Rook terminal the hub has no Work session for.
        band.replies["work.stream.list"] = {"ok": True, "terminals": [
            {"id": "t9", "harness": "claude", "title": "moved here", "cwd": "/srv/app", "running": True},
            {"id": "t10", "harness": "shell", "running": False}]}
        att = await (await client.post("/account/work/session/attach", json=body(p, terminal="t9"),
                                        headers=p.headers)).json()
        s = work.store.get(att["session"], p.uid)
        assert s["term_id"] == "t9" and s["term_running"] and s["title"] == "moved here"
        same = await (await client.post("/account/work/session/attach", json=body(p, terminal="t9"),
                                         headers=p.headers)).json()
        assert same["session"] == att["session"]
        r = await client.post("/account/work/session/attach", json=body(p, terminal="t10"), headers=p.headers)
        assert r.status == 502 and "ended" in (await r.json())["error"]
        # Link to a task by catalog key; an empty task unlinks.
        out3 = await (await client.post("/account/work/session/link", json=body(p, agent="codex", native_id=X1,
                                                                                task="fix-login"),
                                         headers=p.headers)).json()
        assert out3 == {"ok": True, "links": {"task": "fix-login"}}
        band.replies["sessions.list"] = {"ok": True, "total": 1, "items": [dict(
            agent="codex", native_id=X1, title="x", cwd="/x", state="closed", origin="external", updated=1,
            view={"terminal": None, "mirror": False, "transcript": True}, input="none", inbox_policy="unknown",
            links={}, resumable=True)]}
        listed = await (await client.get("/account/work/sessions?worker=w1", headers=p.headers)).json()
        assert listed["sessions"][0]["links"] == {"task": "fix-login"}
        await client.post("/account/work/session/link", json=body(p, agent="codex", native_id=X1, task=""),
                          headers=p.headers)
        listed = await (await client.get("/account/work/sessions?worker=w1", headers=p.headers)).json()
        assert listed["sessions"][0]["links"] == {}
        r = await client.post("/account/work/session/link", json=body(p, task="bad task"), headers=p.headers)
        assert r.status == 400
        # Linking through a Work session stores the task on it too.
        await client.post("/account/work/session/link", json=body(p, task="t_1", work_session=att["session"]),
                          headers=p.headers)
        assert work.store.get(att["session"], p.uid)["task"] == "t_1"
