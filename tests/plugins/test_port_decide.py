"""decide against a fake decision service (stdlib HTTP) and, for drive, fake
device contracts behind the kit ctx's dispatcher. Every test has a hard timeout."""

import base64
import http.server
import json
import struct
import threading

import pytest

from port_helpers import CONTRACTS, ME, PLUGINS, go
from rook.kit.ctx import Ctx, KitError, invoke, load_run
from rook.kit.manifest import check, load

M = load(PLUGINS / "decide", CONTRACTS).data


class Model:
    """The fake service: answers come from ``self.answer(question)``."""

    def __init__(self):
        self.requests, self.ready, self.batches = [], True, []
        self.answer = lambda q: {"noul": 0.1, "confidence": 0.99} if q["type"] == "noul" else \
            {"choice": list(q.get("options") or q.get("levels"))[0], "confidence": 0.95,
             "probabilities": {str(o): 0.5 for o in (q.get("options") or q.get("levels"))}}

    def __enter__(self):
        model = self

        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                model.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
                if self.path.startswith("/healthz"):
                    return self._send(200, {"ok": True, "ready": model.ready})
                return self._send(200, {"model": "diffucision-7b", "calibration": "none", "secret": "x"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                if not body["questions"]:
                    return self._send(422, {"detail": "no questions"})
                model.batches.append(len(body["questions"]))
                self._send(200, {"answers": [{"id": q["id"], **model.answer(q)} for q in body["questions"]],
                                 "model": "diffucision-7b"})

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        return self

    def __exit__(self, *a):
        self.srv.shutdown()


def run(func, settings, args, dispatch=None, secret=None):
    async def body():
        ctx = Ctx(M, ME, settings=settings, dispatch=dispatch, resolve_secret=secret, cap=func)
        return await invoke(load_run(PLUGINS / "decide", M["caps"][f"decide.{func}"]["run"], {}), ctx, args)
    return go(body, 30)


def test_manifest_is_clean():
    data, issues = check(PLUGINS / "decide", CONTRACTS)
    assert data is not None and [str(i) for i in issues] == []


def test_run_normalises_splits_and_sends_the_token():
    with Model() as m:
        qs = [{"id": f"q{i}", "type": "yes-no", "question": f"Is {i} odd?"} for i in range(30)]
        qs.append({"id": "c", "type": "choice", "question": "Colour?", "options": {"red": "warm", "blue": "cold"}})
        settings = {"endpoint": m.url + "/decide?adapter=web", "adapter": "decision-engine", "token": "tok-ref"}
        out = run("run", settings, {"state": "a screen", "questions": qs}, secret=lambda n: {"tok-ref": "s3"}[n])
        assert m.batches == [24, 7] and out["passes"] == 2          # decision-engine: 24 per pass
        assert out["answers"][0] == {"id": "q0", "type": "noul", "confidence": 0.99, "p": 0.1,
                                     "probabilities": {"true": 0.1, "false": 0.9}}
        assert out["answers"][-1]["choice"] == "red" and out["adapter"] == "decision-engine"
        assert {(r[1], r[2]) for r in m.requests} == {("/decide?adapter=web", "Bearer s3")}
        assert "temperature" not in m.requests[0][3]                # the engine takes none
        for bad, why in (([], "non-empty"), ([{"type": "choice", "question": "x", "options": ["a"]}], "at least 2"),
                         ([{"type": "maybe", "question": "x"}], "type must be")):
            with pytest.raises(KitError, match=why):
                run("run", settings, {"state": 1, "questions": bad}, secret=lambda n: "s3")


def test_status_and_errors():
    with Model() as m:
        out = run("status", {"endpoint": m.url}, {})
        assert out["ok"] and out["ready"] and out["model"] == {"model": "diffucision-7b", "calibration": "none"}
        assert out["max_questions"] == 64 and out["latency_ms"] >= 0
    out = run("status", {"endpoint": "http://127.0.0.1:9", "timeout": "2s"}, {})
    assert out["ok"] is False and "unreachable" in out["error"]
    with pytest.raises(KitError) as e:
        run("run", {"endpoint": "http://127.0.0.1:9", "timeout": "2s"},
            {"state": "x", "questions": [{"type": "noul", "question": "?"}]})
    assert e.value.retry


# -- drive -----------------------------------------------------------------------

def png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack("!I", 13) + b"IHDR" + struct.pack("!II", w, h) + b"\x08\x02\0\0\0"


class Devices:
    def __init__(self, ui=None, hold=None):
        self.calls, self.ui, self.hold = [], ui, hold

    async def __call__(self, cap, args, caller, target=None):
        self.calls.append((target, cap, args))
        if cap == "screen.capture":
            return {"name": "s.png", "mime": "image/png", "data": base64.b64encode(png(800, 600)).decode()}
        if cap == "ui.screen":
            if self.ui is None:
                raise KitError("unknown capability: ui.screen")
            return {"app": "x", "items": self.ui}
        if cap == self.hold:
            raise KitError("held for a person's answer (hold quiet-otter-lamp)")
        return {"ok": True}


def answers(action, target=None, done=0.0, confirm=0.0, conf=0.99):
    def answer(q):
        if q["type"] == "noul":
            p = {"done": done, "ready": 0.9, "needs_confirmation": confirm, "abort": 0.0}[q["id"]]
            return {"noul": p, "confidence": 0.99}
        pick = {"action": action, "target": target}.get(q["id"]) or (q.get("options") or q["levels"])[0]
        return {"choice": pick, "confidence": conf}
    return answer


BASE = {"settle": "1ms", "dry_run": False}


def test_drive_clicks_an_element_then_finishes():
    with Model() as m:
        dev = Devices(ui=[{"text": "OK", "x": 100, "y": 50}, {"text": "Cancel", "x": 300, "y": 50}])
        script, step = [answers("click", "OK"), answers("click", done=0.9)], [-1]

        def answer(q):
            if q["id"] == "action":         # first in every pass
                step[0] += 1
            return script[step[0]](q)
        m.answer = answer
        out = run("drive", {"endpoint": m.url, **BASE},
                  {"goal": "press OK", "screen_worker": "kaiju", "max_steps": 5}, dispatch=dev)
        assert out["state"] == "finished" and out["reason"] == "goal reached" and out["steps"] == 2
        hid = [c for c in dev.calls if c[1].startswith("hid.")]
        assert hid == [("kaiju", "hid.click", {"x": 100, "y": 50})]
        assert out["log"][0]["observation"] == "ui.screen" and not out["log"][0]["blind"]


def test_drive_stops_for_a_person_and_on_a_hold():
    with Model() as m:
        # Blind (no ui.screen, no perceiver): every action is gated.
        m.answer = answers("type")
        out = run("drive", {"endpoint": m.url, **BASE},
                  {"goal": "log in", "screen_worker": "tv", "texts": ["hello", "bye"]}, dispatch=Devices())
        assert out["state"] == "needs_confirmation" and "blind" in out["pending"]["gates"]
        assert out["pending"]["calls"] == [{"cap": "hid.type", "args": {"text": "hello"}}]

        dev = Devices(ui=[{"text": "Delete all", "x": 5, "y": 5}, {"text": "Keep", "x": 9, "y": 9}])
        m.answer = answers("click", "Delete all")
        out = run("drive", {"endpoint": m.url, **BASE}, {"goal": "tidy", "screen_worker": "pc"}, dispatch=dev)
        assert out["pending"]["gates"] == ["destructive"] and not [c for c in dev.calls if c[1] == "hid.click"]

        dev = Devices(ui=[{"text": "Next", "x": 5, "y": 5}, {"text": "Back", "x": 9, "y": 9}], hold="hid.click")
        m.answer = answers("click", "Next")
        out = run("drive", {"endpoint": m.url, **BASE},
                  {"goal": "next", "screen_worker": "pc", "input_worker": "kvm"}, dispatch=dev)
        assert out["state"] == "held" and out["hold"] == "quiet-otter-lamp"
        assert ("kvm", "hid.click", {"x": 5, "y": 5}) in dev.calls


def test_drive_dry_run_and_kill_switch():
    with Model() as m:
        dev = Devices(ui=[{"text": "Go", "x": 1, "y": 2}, {"text": "Stop", "x": 3, "y": 4}])
        m.answer = answers("key")
        out = run("drive", {"endpoint": m.url, "settle": "1ms"},          # dry_run defaults on: one step
                  {"goal": "save", "screen_worker": "pc", "keys": ["ctrl+s", "enter"]}, dispatch=dev)
        assert out["dry_run"] and out["state"] == "finished" and out["steps"] == 1
        assert out["log"][0]["calls"] == [{"cap": "hid.key", "args": {"key": "s", "mods": ["ctrl"]}}]
        assert not [c for c in dev.calls if c[1].startswith("hid.")]
        with pytest.raises(KitError, match="kill switch"):
            run("drive", {"endpoint": m.url, "halt": True}, {"goal": "x", "screen_worker": "pc"}, dispatch=dev)
