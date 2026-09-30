"""decide.* hub plugin: adapters, transports and the drive loop, against a fake
decision server and fake screenshot / hid / ui caps (no model, no real input)."""

from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest

from rook.core.facts import NodeFacts
from rook.core.host import PluginHost
from rook.core.plugin import Candidate, parse_resource
from rook.hub.plugins import decide as decide_mod
from rook.hub.plugins.decide.client import (DecisionClient, DecisionError,
                                            normalise_questions)
from rook.hub.plugins.decide.drive import (Frame, Plan, Thresholds, input_calls,
                                           interpret, jpeg_size, looks_destructive)

HUB = NodeFacts(node_id="h", name="rook", roles=frozenset({"is_hub"}), hw={"os": "linux"})


def jpeg(w: int, h: int) -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 30, 40)).save(buf, format="JPEG", quality=40)
    return base64.b64encode(buf.getvalue()).decode()


# -- a fake decision server -------------------------------------------------------

class FakeModel:
    """Answers /decide with scripted choices. ``script`` is a list of dicts
    (one per request) mapping question id -> answer: an option label for a
    choice, a float P(yes) for noul. Unscripted questions get the first
    option / P=0.05. ``conf`` is the reported margin."""

    def __init__(self, dialect="diffucision", script=None, conf=0.95):
        self.dialect = dialect
        self.script = list(script or [])
        self.conf = conf
        self.requests: list[tuple[str, str, dict | None, dict]] = []

    def answer(self, body):
        plan = self.script.pop(0) if self.script else {}
        out = []
        for q in body["questions"]:
            want = plan.get(q["id"])
            if q["type"] == "noul":
                p = 0.05 if want is None else float(want)
                a = {"id": q["id"], "type": "noul", "noul": p, "confidence": self.conf}
                if self.dialect == "diffucision":
                    a["probabilities"] = {"true": p, "false": 1 - p}
            else:
                spec = q.get("options") or q.get("levels")
                labels = list(spec) if isinstance(spec, dict) else list(spec)
                pick = want if want is not None else labels[0]
                probs = {l: (0.9 if l == pick else 0.1 / (len(labels) - 1)) for l in labels}
                a = {"id": q["id"], "type": q["type"], "probabilities": probs,
                     "confidence": self.conf}
                if q["type"] == "choice":
                    a["choice"] = pick
                else:
                    a["level"], a["score"] = pick, 1.5
            out.append(a)
        return {"answers": out, "latency_ms": 4.2, "passes": 1, "model": "fake-model",
                "calibration": "temperature=1.0 (uncalibrated)"}

    async def http(self, method, url, body, headers, timeout):
        self.requests.append((method, url, body, headers))
        url = url.split("?")[0]
        if url.endswith("/healthz"):
            return 200, ({"ok": True} if self.dialect == "diffucision"
                         else {"status": "ok", "ready": True})
        if url.endswith("/info"):
            return 200, {"model": "fake-model", "adapter": "runs/x/final", "calibration": "none",
                         "max_questions": 64, "gpu_mem_gib": 13.7}
        if url.endswith("/decide"):
            return 200, self.answer(body)
        if url.endswith("/perceive"):
            return 200, {"text": "Inbox", "elements": [{"label": "Compose", "x": 10, "y": 10,
                                                        "w": 80, "h": 20}]}
        return 404, {"detail": "not found"}


# -- fake band --------------------------------------------------------------------

class FakeBand:
    def __init__(self, size=(800, 600)):
        self.calls: list[tuple[str, dict, str]] = []
        self.size = size
        self.frame = jpeg(*size)
        self.backend = {"pc": "xdotool", "phone": "android-accessibility"}
        self.fail = set()
        self.perceive_reply = None
        self.model: FakeModel | None = None

    def roster(self):
        return {
            "w1": {"name": "pc", "caps": ["screenshot.capture_preview", "hid.backend", "hid.type",
                                          "hid.mouse.click", "hid.key_combo"]},
            "w2": {"name": "phone", "caps": ["screenshot.capture_preview", "hid.backend",
                                             "hid.mouse.click", "hid.key_combo", "hid.mouse.drag",
                                             "ui.text"]},
            "w3": {"name": "gpu-box", "caps": ["cmd.decide-run", "cmd.decide-health"]},
            "w4": {"name": "eyes", "caps": ["vision.parse"]},
        }

    def hid_calls(self):
        return [c for c in self.calls if c[0].startswith("hid.") and c[0] != "hid.backend"]

    async def __call__(self, cap, args, target, timeout):
        self.calls.append((cap, dict(args), target))
        if cap in self.fail:
            return {"ok": False, "error": f"{cap} broke"}
        if cap == "screenshot.capture_preview":
            return {"ok": True, "format": "jpeg", "data": self.frame, "bytes": 100}
        if cap == "hid.backend":
            return {"backend": self.backend[target]}
        if cap.startswith("hid."):
            return {"ok": True}
        if cap == "ui.text":
            return {"ok": True, "text": "Settings\nWi-Fi\nBluetooth"}
        if cap == "vision.parse":
            return self.perceive_reply or {"text": "", "elements": []}
        if cap == "cmd.decide-run":
            body = json.loads(args["payload"])
            return {"ok": True, "code": 0, "stdout": json.dumps(self.model.answer(body))}
        if cap == "cmd.decide-health":
            return {"ok": True, "code": 0, "stdout": json.dumps({"ok": True})}
        raise LookupError(f"no {cap} on {target}")


def load(tmp_path, band, model, **settings):
    store = {"endpoint": "http://gpu-box:8911", **settings}
    host = PluginHost(facts=HUB, cap_caller=band, elect_one=lambda p: True,
                      stored_settings=lambda ns: store if ns == "decide" else {},
                      secrets=lambda key: "s3cret" if key == "plugin.decide.token" else None,
                      data_root=str(tmp_path / "plugins"))
    host.load([Candidate("decide", "test:decide", lambda: decide_mod.PLUGIN)])
    assert host.status["decide"]["state"] == "loaded", host.status
    plugin = host.plugins[0]
    plugin._http = model.http
    plugin._roster = band.roster
    band.model = model
    return host, plugin, store


async def call(host, cap, **args):
    body = await host.dispatch(cap, args, identity="agent:test")
    assert body["ok"], body
    return body["result"]


async def finish(plugin, run_id, timeout=5.0):
    run = plugin.drives[run_id]
    await asyncio.wait_for(asyncio.shield(run.task), timeout)
    return run


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# -- questions / answers ----------------------------------------------------------

def test_normalise_questions():
    qs = normalise_questions([
        {"id": "a", "type": "choice", "question": "pick", "options": ["x", "y"]},
        {"type": "yes-no", "text": "ok?"},
        {"id": "s", "type": "score", "question": "how much", "levels": {"0": "none", "1": "lots"}},
    ], 26)
    assert [q["type"] for q in qs] == ["choice", "noul", "score"]
    assert qs[1]["id"] == "q2" and qs[1]["instructions"] == "ok?"
    for bad, msg in [
        ([{"id": "a", "question": "q", "options": ["x", "y"]}] * 2, "unique"),
        ([{"question": "q", "options": ["x"]}], "at least 2"),
        ([{"question": "q", "options": [str(i) for i in range(27)]}], "at most 26"),
        ([{"question": "q", "type": "maybe", "options": ["x", "y"]}], "type must be"),
        ([{"type": "choice", "options": ["x", "y"]}], "text is required"),
        ([{"question": "q", "type": "choice"}], "needs a list"),
        ([], "non-empty"),
    ]:
        with pytest.raises(ValueError, match=msg):
            normalise_questions(bad, 26)


@pytest.mark.asyncio
async def test_run_diffucision_http(tmp_path):
    band, model = FakeBand(), FakeModel(script=[{"intent": "question", "urgent": 0.8}])
    host, plugin, _ = load(tmp_path, band, model, temperature=0.7)
    out = await call(host, "decide.run", state={"utterance": "what time is it"}, questions=[
        {"id": "intent", "type": "choice", "question": "Intent?", "options": ["command", "question"]},
        {"id": "urgent", "type": "noul", "question": "Urgent?"},
        {"id": "lvl", "type": "score", "question": "Level?", "levels": ["low", "mid", "high"]}])
    a = {x["id"]: x for x in out["answers"]}
    assert a["intent"]["choice"] == "question"
    assert a["urgent"]["p"] == 0.8 and a["urgent"]["probabilities"]["false"] == pytest.approx(0.2)
    assert a["lvl"]["level"] == "low" and "score" in a["lvl"]
    assert out["passes"] == 1 and out["model"] == "fake-model" and out["adapter"] == "diffucision"
    method, url, body, headers = model.requests[-1]
    assert (method, url) == ("POST", "http://gpu-box:8911/decide")
    assert body["temperature"] == 0.7
    assert headers["Authorization"] == "Bearer s3cret"
    # a named adapter on a serve-and-train endpoint rides along as a query string
    plugin.settings.refresh({"endpoint": "http://gpu-box:8911/decide?adapter=web"})
    await call(host, "decide.run", state="s", questions=[{"type": "noul", "question": "q?"}])
    await call(host, "decide.health")
    assert [r[1] for r in model.requests[-2:]] == [
        "http://gpu-box:8911/decide?adapter=web", "http://gpu-box:8911/healthz"]


@pytest.mark.asyncio
async def test_run_decision_engine_splits_and_normalises(tmp_path):
    band, model = FakeBand(), FakeModel(dialect="decision-engine")
    host, plugin, _ = load(tmp_path, band, model, adapter="decision-engine")
    qs = [{"id": f"n{i}", "type": "noul", "question": f"q{i}?"} for i in range(30)]
    out = await call(host, "decide.run", state="s", questions=qs, temperature=2.0)
    assert out["passes"] == 2 and len(out["answers"]) == 30
    assert all(a["probabilities"] == {"true": 0.05, "false": 0.95} for a in out["answers"])
    decides = [r for r in model.requests if r[1].endswith("/decide")]
    assert [len(r[2]["questions"]) for r in decides] == [24, 6]
    assert all("temperature" not in r[2] for r in decides)
    # 52 options is fine for the typed engine, 53 is not
    ok = [{"id": "c", "question": "c", "options": [f"o{i}" for i in range(52)]}]
    assert (await call(host, "decide.run", state="s", questions=ok))["answers"][0]["choice"] == "o0"


@pytest.mark.asyncio
async def test_cap_transport_wrapped_cli(tmp_path):
    band, model = FakeBand(), FakeModel(script=[{"x": 0.9}])
    host, plugin, _ = load(tmp_path, band, model, endpoint="cap://gpu-box/cmd.decide-run")
    out = await call(host, "decide.run", state="s", questions=[{"id": "x", "type": "noul",
                                                               "question": "x?"}])
    assert out["answers"][0]["p"] == 0.9
    cap, args, target = band.calls[-1]
    assert (cap, target) == ("cmd.decide-run", "gpu-box")
    assert json.loads(args["payload"])["questions"][0]["id"] == "x"
    health = await call(host, "decide.health")
    assert health["ok"] and health["ready"] and health["configured"]
    assert band.calls[-1][0] == "cmd.decide-health"


@pytest.mark.asyncio
async def test_health_and_errors(tmp_path):
    band, model = FakeBand(), FakeModel()
    host, plugin, store = load(tmp_path, band, model, endpoint="")
    h = await call(host, "decide.health")
    assert h == {"ok": False, "ready": False, "configured": False, "error": h["error"]}
    body = await host.dispatch("decide.run", {"state": "s", "questions": [
        {"question": "q", "type": "noul"}]})
    assert not body["ok"] and "not configured" in body["error"]
    plugin.settings.refresh({"endpoint": "http://gpu-box:8911"})
    assert (await call(host, "decide.health"))["ready"] is True
    info = await call(host, "decide.info")
    assert info["model"] == "fake-model" and info["client"]["max_options"] == 26

    async def down(*a):
        raise ConnectionRefusedError("nope")
    plugin._http = down
    h = await call(host, "decide.health")
    assert h["ok"] is False and "unreachable" in h["error"]


@pytest.mark.asyncio
async def test_real_http_server():
    """The default aiohttp transport against an in-process fake server."""
    from aiohttp import web
    model = FakeModel(script=[{"go": 0.7}])
    seen = {}

    async def decide(req):
        seen["auth"] = req.headers.get("Authorization")
        return web.json_response(model.answer(await req.json()))

    async def healthz(req):
        return web.json_response({"ok": True})

    async def bad(req):
        return web.json_response({"detail": "max 26 options"}, status=422)

    app = web.Application()
    app.add_routes([web.post("/decide", decide), web.get("/healthz", healthz),
                    web.get("/info", bad)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        client = DecisionClient(parse_resource(f"http://127.0.0.1:{port}/decide"), token="t")
        out = await client.decide("s", [{"id": "go", "type": "noul", "question": "go?"}])
        assert out["answers"][0]["p"] == 0.7 and seen["auth"] == "Bearer t"
        assert (await client.health())["ready"] is True
        with pytest.raises(DecisionError, match="HTTP 422"):
            await client.info()
    finally:
        await runner.cleanup()


# -- drive building blocks ---------------------------------------------------------

def test_jpeg_size_and_destructive():
    assert jpeg_size(base64.b64decode(jpeg(321, 123))) == (321, 123)
    assert jpeg_size(b"not a jpeg") is None
    words = ["send", "delete"]
    assert looks_destructive({"type": "click", "target": "Send now"}, words)
    assert looks_destructive({"type": "key", "key": "alt+F4"}, words)
    assert not looks_destructive({"type": "click", "target": "Compose"}, words)


def test_input_calls_per_backend():
    screen = (1000, 800)
    assert input_calls({"type": "key", "key": "ctrl+enter"}, "xdotool", screen) == [
        ("hid.key_combo", {"key": "Return", "modifiers": ["ctrl"]})]
    assert input_calls({"type": "key", "key": "enter"}, "win32", screen) == [
        ("hid.key_combo", {"key": "enter", "modifiers": []})]
    assert input_calls({"type": "double_click", "x": 5, "y": 6}, "win32", screen) == [
        ("hid.mouse.click", {"button": 1, "x": 5, "y": 6}), ("hid.mouse.click", {"button": 1})]
    assert input_calls({"type": "click", "x": 5, "y": 6}, "android-accessibility", screen) == [
        ("hid.mouse.click", {"x": 5, "y": 6})]
    assert input_calls({"type": "scroll_down"}, "android-accessibility", screen) == [
        ("hid.mouse.drag", {"x1": 500, "y1": 560, "x2": 500, "y2": 240, "duration": 0.3})]
    assert input_calls({"type": "key", "key": "escape"}, "android-accessibility", screen) == [
        ("hid.key_combo", {"keys": "back"})]
    with pytest.raises(ValueError):
        input_calls({"type": "right_click", "x": 1, "y": 1}, "android-accessibility", screen)


def test_interpret_grid_and_gates():
    plan = Plan(["click", "wait"], [], [], 4, 4)
    frame = Frame(400, 300, 1, "x")
    ans = [{"id": "action", "choice": "click", "confidence": 0.95},
           {"id": "target_row", "choice": "row 2", "confidence": 0.95},
           {"id": "target_col", "choice": "column 4 (right)", "confidence": 0.5},
           {"id": "done", "p": 0.1}, {"id": "ready", "p": 0.9},
           {"id": "needs_confirmation", "p": 0.05}, {"id": "abort", "p": 0.0}]
    d = interpret(plan, ans, [], frame, (800, 600), Thresholds(), blind=False)
    assert d["kind"] == "act" and d["action"]["x"] == 700 and d["action"]["y"] == 225
    assert d["gates"] == ["low_confidence"] and d["confidence"] == 0.5
    d = interpret(plan, ans, [], frame, (800, 600), Thresholds(min_confidence=0.4,
                                                              confirm="always"), blind=True)
    assert d["gates"] == ["policy", "blind"]
    ans[3] = {"id": "done", "p": 0.9}
    assert interpret(plan, ans, [], frame, (800, 600), Thresholds(), False)["kind"] == "done"
    ans[6] = {"id": "abort", "p": 0.9}
    assert interpret(plan, ans, [], frame, (800, 600), Thresholds(), False)["kind"] == "abort"


# -- the drive loop ----------------------------------------------------------------

CONFIDENT = {"action": "click", "target": "Compose", "ready": 0.95}


@pytest.mark.asyncio
async def test_drive_dry_run_default_sends_no_input(tmp_path):
    band, model = FakeBand(), FakeModel(script=[{"action": "click", "target_row": "row 1 (top)",
                                                 "target_col": "column 1 (left)", "ready": 0.9}])
    host, plugin, _ = load(tmp_path, band, model)
    snap = await call(host, "decide.drive", goal="open settings", screen_worker="pc")
    assert snap["dry_run"] is True and snap["max_steps"] == 1
    run = await finish(plugin, snap["run_id"])
    assert run.state == "finished" and run.reason == "step budget"
    assert band.hid_calls() == []
    detail = await call(host, "decide.runs", run_id=run.id)
    step = detail["recent_steps"][0]
    assert step["outcome"] == "dry_run" and step["blind"] is True and step["would_confirm"]
    assert step["intended_calls"][0]["cap"] == "hid.mouse.click"
    assert step["intended_calls"][0]["args"]["x"] == 50  # (0.5 * 800 / 8)
    listing = await call(host, "decide.runs")
    assert listing["items"][0]["id"] == run.id and listing["items"][0]["identity"] == "agent:test"


@pytest.mark.asyncio
async def test_drive_live_with_perceiver_acts_then_finishes(tmp_path):
    band, model = FakeBand(size=(400, 300)), FakeModel(script=[CONFIDENT, {"done": 0.9}])
    band.perceive_reply = {"text": "Inbox", "elements": [
        {"label": "Refresh", "x": 0, "y": 0, "w": 10, "h": 10},
        {"label": "Compose", "x": 100, "y": 50, "w": 40, "h": 20}]}
    host, plugin, _ = load(tmp_path, band, model, perceiver="cap://eyes/vision.parse",
                           settle_ms=0, min_confidence=0.5, screen_size="800x600")
    snap = await call(host, "decide.drive", goal="write an email", screen_worker="pc",
                      dry_run=False, wait=5)
    assert snap["state"] == "finished" and snap["reason"] == "goal reached", snap
    # element centre (120, 60) in a 400x300 frame, scaled to 800x600
    assert band.hid_calls() == [("hid.mouse.click", {"button": 1, "x": 240, "y": 120}, "pc")]
    # the model saw the element labels and the perceived text
    decides = [r for r in model.requests if r[1].endswith("/decide")]
    target_q = [q for q in decides[0][2]["questions"] if q["id"] == "target"][0]
    assert target_q["options"] == ["Refresh", "Compose"]
    assert decides[0][2]["state"]["screen_text"] == "Inbox"
    assert decides[1][2]["state"]["recent_actions"][0]["result"] == "ok"


@pytest.mark.asyncio
async def test_drive_confirmation_gate_approve_then_refuse(tmp_path):
    band = FakeBand()
    band.perceive_reply = {"text": "Draft", "elements": [
        {"label": "Body", "x": 0, "y": 0, "w": 100, "h": 100},
        {"label": "Send", "x": 200, "y": 0, "w": 50, "h": 20}]}
    model = FakeModel(script=[
        {"action": "type", "text": "hello", "ready": 0.9, "needs_confirmation": 0.5},
        {"action": "click", "target": "Send", "ready": 0.9}])
    host, plugin, _ = load(tmp_path, band, model, perceiver="cap://eyes/vision.parse",
                           settle_ms=0, min_confidence=0.5)
    snap = await call(host, "decide.drive", goal="reply", screen_worker="pc", dry_run=False,
                      texts=["hello", "bye"])
    run = plugin.drives[snap["run_id"]]
    await until(lambda: run.state == "awaiting_confirmation")
    detail = await call(host, "decide.runs", run_id=run.id)
    assert detail["pending"]["gates"] == ["model_flagged"]
    assert detail["pending"]["action"] == {"type": "type", "text": "hello"}
    assert band.hid_calls() == []
    await call(host, "decide.confirm", run_id=run.id, approve=True)
    await until(lambda: run.state == "awaiting_confirmation" and run.step == 2)
    assert band.hid_calls() == [("hid.type", {"text": "hello"}, "pc")]
    assert run.pending["gates"] == ["destructive"]
    await call(host, "decide.confirm", run_id=run.id, approve=False, note="not yet")
    await finish(plugin, run.id)
    assert run.state == "stopped" and run.reason == "confirmation refused"
    assert len(band.hid_calls()) == 1
    steps = (await call(host, "decide.runs", run_id=run.id))["recent_steps"]
    assert [s["outcome"] for s in steps] == ["acted", "refused"]
    assert steps[1]["confirmation"]["approve"] is False
    assert steps[0]["confirmation"]["by"] == "agent:test"


@pytest.mark.asyncio
async def test_drive_blind_needs_confirmation_and_kill_switch(tmp_path):
    band, model = FakeBand(), FakeModel(script=[{"action": "click", "ready": 0.9}] * 5)
    host, plugin, store = load(tmp_path, band, model, min_confidence=0.1, settle_ms=0)
    snap = await call(host, "decide.drive", goal="x", screen_worker="pc", dry_run=False)
    run = plugin.drives[snap["run_id"]]
    await until(lambda: run.state == "awaiting_confirmation")
    assert run.pending["gates"] == ["blind"]
    out = await call(host, "decide.stop")
    assert out["stopped"] == [run.id]
    await finish(plugin, run.id)
    assert run.state == "stopped" and run.reason.startswith("stopped by agent:test")
    assert band.hid_calls() == []
    # the halt setting refuses new drives and stops running ones
    plugin.settings.refresh({**store, "halt": True, "min_confidence": 0.1})
    body = await host.dispatch("decide.drive", {"goal": "x", "screen_worker": "pc"})
    assert not body["ok"] and "kill switch" in body["error"]
    plugin.settings.refresh({**store, "min_confidence": 0.1})
    snap = await call(host, "decide.drive", goal="x", screen_worker="pc", dry_run=False)
    run = plugin.drives[snap["run_id"]]
    await until(lambda: run.state == "awaiting_confirmation")
    plugin.settings.refresh({**store, "halt": True})
    await finish(plugin, run.id)
    assert run.state == "stopped" and "kill switch" in run.reason
    assert band.hid_calls() == []


@pytest.mark.asyncio
async def test_drive_confirmation_timeout(tmp_path):
    band, model = FakeBand(), FakeModel(script=[{"action": "click", "ready": 0.9}])
    host, plugin, _ = load(tmp_path, band, model, confirm="always", settle_ms=0,
                           confirm_timeout_s=5)
    snap = await call(host, "decide.drive", goal="x", screen_worker="pc", dry_run=False)
    run = plugin.drives[snap["run_id"]]
    await until(lambda: run.state == "awaiting_confirmation")
    run.pending["expires"] = 0  # the loop re-checks the pending deadline every 0.5 s
    await finish(plugin, run.id, timeout=8)
    assert run.state == "stopped" and run.reason == "confirmation timed out"
    assert band.hid_calls() == []


@pytest.mark.asyncio
async def test_drive_android_uses_ui_text_and_android_inputs(tmp_path):
    band = FakeBand(size=(1080, 2400))
    model = FakeModel(script=[{"action": "key", "key": "back", "ready": 0.9},
                              {"action": "scroll_down", "ready": 0.9},
                              {"action": "click", "target_row": "row 8 (bottom)",
                               "target_col": "column 1 (left)", "ready": 0.9},
                              {"done": 0.95}])
    host, plugin, _ = load(tmp_path, band, model, min_confidence=0.5, settle_ms=0)
    snap = await call(host, "decide.drive", goal="open wifi", screen_worker="phone",
                      dry_run=False, wait=5)
    assert snap["state"] == "finished", snap
    decides = [r for r in model.requests if r[1].endswith("/decide")]
    first = decides[0][2]
    assert first["state"]["observation_source"] == "ui.text"
    action_q = [q for q in first["questions"] if q["id"] == "action"][0]
    assert "right_click" not in action_q["options"]
    key_q = [q for q in first["questions"] if q["id"] == "key"][0]
    assert key_q["options"] == ["back", "home", "recents"]
    assert band.hid_calls() == [
        ("hid.key_combo", {"keys": "back"}, "phone"),
        ("hid.mouse.drag", {"x1": 540, "y1": 1680, "x2": 540, "y2": 720, "duration": 0.3}, "phone"),
        ("hid.mouse.click", {"x": 68, "y": 2250}, "phone")]


@pytest.mark.asyncio
async def test_drive_failure_budget_and_busy(tmp_path):
    band, model = FakeBand(), FakeModel(script=[{"action": "key", "ready": 0.9}] * 10)
    band.perceive_reply = {"text": "terminal", "elements": []}
    host, plugin, _ = load(tmp_path, band, model, perceiver="cap://eyes/vision.parse",
                           min_confidence=0.5, settle_ms=0)
    band.fail.add("hid.key_combo")
    snap = await call(host, "decide.drive", goal="x", screen_worker="pc", dry_run=False, wait=5)
    assert snap["state"] == "failed" and "hid.key_combo" in snap["reason"]
    band.fail.clear()
    snap = await call(host, "decide.drive", goal="x", screen_worker="pc", dry_run=False,
                      max_steps=3, wait=5)
    assert snap["state"] == "finished" and snap["reason"] == "step budget" and snap["step"] == 3
    assert len(band.hid_calls()) == 1 + 3
    # one live input drive per worker
    model.script = [{"action": "click", "ready": 0.9, "needs_confirmation": 0.9}] * 3
    snap = await call(host, "decide.drive", goal="x", screen_worker="pc", dry_run=False)
    await until(lambda: plugin.drives[snap["run_id"]].state == "awaiting_confirmation")
    body = await host.dispatch("decide.drive", {"goal": "y", "screen_worker": "pc",
                                                "dry_run": False})
    assert not body["ok"] and "already sending input" in body["error"]
    body = await host.dispatch("decide.drive", {"goal": "y", "screen_worker": "gpu-box"})
    assert not body["ok"] and "no screenshot.capture_preview" in body["error"]
    await call(host, "decide.stop", run_id=snap["run_id"])
    await finish(plugin, snap["run_id"])


@pytest.mark.asyncio
async def test_journal_survives_restart(tmp_path):
    band, model = FakeBand(), FakeModel(script=[{"action": "click", "ready": 0.9}])
    host, plugin, _ = load(tmp_path, band, model, settle_ms=0)
    snap = await call(host, "decide.drive", goal="x", screen_worker="pc", dry_run=False)
    await until(lambda: plugin.drives[snap["run_id"]].state == "awaiting_confirmation")
    plugin.drives[snap["run_id"]].task.cancel()
    await asyncio.gather(plugin.drives[snap["run_id"]].task, return_exceptions=True)
    plugin.journal.update_run(snap["run_id"], state="running")  # as if the process died
    host2, plugin2, _ = load(tmp_path, band, model)
    await plugin2.start()
    row = await call(host2, "decide.runs", run_id=snap["run_id"])
    assert row["state"] == "stopped" and row["reason"] == "hub restarted"


def test_settings_are_in_the_schema():
    from rook.hub.settings_schema import Schema
    schema = Schema(worker_package=None)
    for key in ("decide.endpoint", "decide.adapter", "decide.token", "decide.dry_run",
                "decide.halt", "decide.max_steps", "decide.min_confidence"):
        assert schema.get(key) is not None, key
    assert schema.get("decide.dry_run").setting.default is True
    assert schema.get("decide.token").setting.secret
    assert schema.plugins()["decide"]["title"] == "Decide (decision model)"
