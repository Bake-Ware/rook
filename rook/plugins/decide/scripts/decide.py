"""decide.*: a one-pass decision model (Diffucision or the typed decision
engine) answering a batch of typed questions about one state.

Ported from 0.4 ``rook/hub/plugins/decide``: ``decide.run`` and one
``decide.status`` (0.4's health + info). The screen-driving loop
(drive/runs/confirm/stop) is binned (castling-caps: a harness), and so is the
``cap://`` transport: a model served from a worker is reached by its URL.
Sync functions (the host runs them in a thread), stdlib HTTP.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from rook.kit._types import parse_duration
from rook.kit.ctx import KitError

#: Per-dialect request limits (from each service's request validation).
LIMITS = {
    "diffucision": {"max_questions": 64, "max_options": 26, "temperature": True},
    "decision-engine": {"max_questions": 24, "max_options": 52, "temperature": False},
}
_NOUL = {"noul", "yes-no", "yesno", "yes_no", "bool", "boolean"}
TYPES = ("choice", "score", "noul")
_INFO = ("model", "adapter", "lora_path", "temperature", "calibration", "dtype", "quant",
         "max_questions", "max_options", "max_tokens", "calls")


def run(ctx, state, questions, temperature=None) -> dict:
    limits = LIMITS[ctx.settings["adapter"]]
    try:
        qs = normalise_questions(questions, limits["max_options"])
    except ValueError as e:
        raise KitError(str(e)) from None
    if temperature is None:
        temperature = ctx.settings.get("temperature")
    t0 = time.perf_counter()
    answers: list[dict] = []
    model = calibration = None
    passes = 0
    for chunk in split(qs, limits["max_questions"]):
        body: dict[str, Any] = {"state": state, "questions": chunk}
        if temperature is not None and limits["temperature"]:
            body["temperature"] = float(temperature)
        data = _http(ctx, "/decide", body)
        if not isinstance(data, dict) or not isinstance(data.get("answers"), (list, dict)):
            raise KitError("the decision model's answer has no answers")
        raw = data["answers"]
        by_id = raw if isinstance(raw, dict) else {str(a.get("id")): a for a in raw if isinstance(a, dict)}
        for q in chunk:
            a = by_id.get(q["id"])
            if not isinstance(a, dict):
                raise KitError(f"the decision model did not answer {q['id']}")
            answers.append(normalise_answer(q, a, ctx.settings["adapter"]))
        passes += int(data.get("passes") or 1)
        model = model or data.get("model")
        calibration = calibration or data.get("calibration")
    return {"answers": answers, "passes": passes, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "model": model, "calibration": calibration or "uncalibrated", "adapter": ctx.settings["adapter"]}


def status(ctx) -> dict:
    """Reachable and loaded, and what the model says about itself."""
    t0 = time.perf_counter()
    out: dict[str, Any] = {"adapter": ctx.settings["adapter"], **{
        k: v for k, v in LIMITS[ctx.settings["adapter"]].items() if k != "temperature"}}
    try:
        health = _http(ctx, "/healthz")
    except KitError as e:
        return {**out, "ok": False, "ready": False, "error": e.message}
    out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["ready"] = bool(isinstance(health, dict) and health.get(
        "ready", health.get("ok", health.get("status") == "ok")))
    out["ok"] = True
    try:
        info = _http(ctx, "/info")
    except KitError as e:
        out["info_error"] = e.message
    else:
        if isinstance(info, dict):
            out["model"] = {k: info[k] for k in _INFO if k in info}
    return out


def _url(base: str, path: str) -> str:
    """``base + path``, keeping the endpoint's query string on /decide and
    /info (a serve-and-train Diffucision picks an adapter with ?adapter=)."""
    url, _, query = base.partition("?")
    url = url.rstrip("/")
    for suffix in ("/decide", "/healthz", "/info"):
        url = url.removesuffix(suffix)
    return url + path + (f"?{query}" if query and path != "/healthz" else "")


def _http(ctx, path: str, body: dict | None = None) -> Any:
    return _post(ctx, _url(str(ctx.settings["endpoint"]), path), body, ctx.settings.get("token"))


def _post(ctx, url: str, body: dict | None, token: str | None = None) -> Any:
    """GET (no body) or POST JSON; the JSON answer."""
    req = urllib.request.Request(url, method="POST" if body else "GET",
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    what = "the perceiver" if url == ctx.settings.get("perceiver") else "the decision model"
    try:
        with urllib.request.urlopen(req, timeout=parse_duration(ctx.settings["timeout"])) as r:
            text = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode("utf-8", "replace")
        raise KitError(f"{what} answered HTTP {e.code}: {detail}", retry=e.code >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise KitError(f"{what} is unreachable: {getattr(e, 'reason', None) or type(e).__name__}",
                       retry=True) from None
    try:
        return json.loads(text) if text else None
    except ValueError:
        raise KitError(f"{what}: the answer is not JSON") from None


# -- questions and answers (0.4 client.py, unchanged) ---------------------------

def normalise_questions(questions: Any, max_options: int) -> list[dict]:
    """Validate a batch and return it in the wire shape both services accept:
    ``{id, type, instructions, options|levels}``. Raises ``ValueError`` with a
    message naming the bad question."""
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be a non-empty list")
    out: list[dict] = []
    seen: set[str] = set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            raise ValueError(f"question {i + 1} must be an object")
        qid = str(q.get("id") or f"q{i + 1}")
        if qid in seen:
            raise ValueError(f"{qid}: question ids must be unique")
        seen.add(qid)
        kind = str(q.get("type") or "choice").strip().lower()
        if kind in _NOUL:
            kind = "noul"
        if kind not in TYPES:
            raise ValueError(f"{qid}: type must be choice, score or noul (yes-no), got {kind!r}")
        text = str(q.get("question") or q.get("instructions") or q.get("text") or "").strip()
        if not text:
            raise ValueError(f"{qid}: question text is required")
        item: dict[str, Any] = {"id": qid, "type": kind, "instructions": text}
        if kind == "noul":
            crit = q.get("criteria")
            if isinstance(crit, dict) and set(crit) <= {"true", "false"}:
                item["criteria"] = {k: str(v) for k, v in crit.items()}
        else:
            key = "levels" if kind == "score" else "options"
            spec = q.get(key)
            if spec is None:
                spec = q.get("options") if kind == "score" else q.get("criteria")
            if spec is None and kind == "score":
                spec = q.get("criteria")
            if isinstance(spec, dict):
                labels = [str(k) for k in spec]
                spec = {str(k): str(v) for k, v in spec.items()}
            elif isinstance(spec, (list, tuple)):
                labels = [str(v) for v in spec]
                spec = labels
            else:
                raise ValueError(f"{qid}: {kind} needs a list (or label->description map) of {key}")
            if len(labels) < 2:
                raise ValueError(f"{qid}: needs at least 2 {key}")
            if len(labels) > max_options:
                raise ValueError(f"{qid}: at most {max_options} {key} for this model")
            if len(set(labels)) != len(labels) or any(not s.strip() for s in labels):
                raise ValueError(f"{qid}: {key} must be unique and non-empty")
            item[key] = spec
        out.append(item)
    return out


def _labels(q: dict) -> list[str]:
    spec = q.get("levels") if q["type"] == "score" else q.get("options")
    if isinstance(spec, dict):
        return list(spec)
    return [str(s) for s in spec or []]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # NaN guard


def normalise_answer(q: dict, a: dict, adapter: str) -> dict:
    """One answer in the plugin's shape:

    * choice: ``{id, type, choice, probabilities, confidence}``
    * noul:   ``{id, type, p, probabilities: {true, false}, confidence}``
    * score:  ``{id, type, level, score, probabilities, confidence}``

    ``confidence`` is the model's top-two margin (uncalibrated)."""
    kind = q["type"]
    out: dict[str, Any] = {"id": q["id"], "type": kind,
                           "confidence": round(_num(a.get("confidence")), 4)}
    probs = a.get("probabilities") if isinstance(a.get("probabilities"), dict) else None
    if kind == "noul":
        p = a.get("noul", a.get("p"))
        if p is None and probs:
            p = probs.get("true")
        p = min(1.0, max(0.0, _num(p)))
        out["p"] = round(p, 4)
        out["probabilities"] = {"true": round(p, 4), "false": round(1 - p, 4)}
        return out
    labels = _labels(q)
    probs = {str(k): round(_num(v), 4) for k, v in (probs or {}).items()}
    out["probabilities"] = probs
    if kind == "choice":
        choice = a.get("choice")
        if choice is None and probs:
            choice = max(probs, key=probs.get)
        out["choice"] = None if choice is None else str(choice)
        return out
    level = a.get("level")
    if level is None and probs:
        level = max(probs, key=probs.get)
    # diffucision indexes list levels 0..n-1; map back to the label when it can.
    if isinstance(spec := q.get("levels"), list) and str(level).isdigit() \
            and int(level) < len(spec) and str(level) not in labels:
        level = spec[int(level)]
    out["level"] = None if level is None else str(level)
    out["score"] = round(_num(a.get("score", a.get("expected"))), 4)
    return out


def split(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


# -- drive helpers (0.4 drive.py, unchanged) -------------------------------------

#: Default words that make an action look destructive (matched, lower-cased,
#: against the target label, the text to type and the key).
DESTRUCTIVE_WORDS = ["delete", "remove", "erase", "wipe", "format", "uninstall", "send",
                     "submit", "pay", "purchase", "buy", "order", "transfer", "confirm",
                     "publish", "post", "shutdown", "shut down", "restart", "reboot",
                     "log out", "logout", "sign out", "discard", "overwrite", "reset"]
#: Key combos that are destructive on their own.
DESTRUCTIVE_KEYS = {"delete", "shift+delete", "alt+f4", "ctrl+w", "ctrl+q", "ctrl+shift+w",
                    "ctrl+shift+q", "super+l", "ctrl+alt+delete"}
DEFAULT_KEYS = ["enter", "escape", "tab", "backspace"]
ANDROID_KEYS = ["back", "home", "recents"]


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker (or a PNG's IHDR), stdlib only."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                      0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + seg
    return None



@dataclass
class Plan:
    """What the per-frame question batch looks like for this run."""

    actions: list[str]
    texts: list[str]
    keys: list[str]
    rows: int
    cols: int

    def grid_labels(self) -> tuple[list[str], list[str]]:
        rows = [f"row {i + 1}" + (" (top)" if i == 0 else " (bottom)" if i == self.rows - 1 else "")
                for i in range(self.rows)]
        cols = [f"column {i + 1}" + (" (left)" if i == 0 else " (right)" if i == self.cols - 1 else "")
                for i in range(self.cols)]
        return rows, cols


def element_labels(elements: list[dict], limit: int) -> list[str]:
    """Unique option labels for the first ``limit`` elements."""
    out: list[str] = []
    for i, e in enumerate(elements[:limit]):
        label = " ".join(str(e.get("label") or e.get("text") or e.get("role") or
                             f"element {i + 1}").split())[:80] or f"element {i + 1}"
        base, n = label, 2
        while label in out:
            label = f"{base} #{n}"
            n += 1
        out.append(label)
    return out


def build_questions(plan: Plan, elements: list[dict], max_options: int) -> list[dict]:
    qs: list[dict] = [{
        "id": "action", "type": "choice",
        "question": "Which single input action brings the screen closest to the goal right now?",
        "options": plan.actions}]
    if elements:
        qs.append({"id": "target", "type": "choice",
                   "question": "Which on-screen element should the action be applied to?",
                   "options": element_labels(elements, max_options)})
    else:
        rows, cols = plan.grid_labels()
        qs.append({"id": "target_row", "type": "choice",
                   "question": f"The screen is split into a {plan.rows}x{plan.cols} grid. "
                               "Which row holds the thing to act on?", "options": rows})
        qs.append({"id": "target_col", "type": "choice",
                   "question": "Which column holds the thing to act on?", "options": cols})
    if len(plan.texts) >= 2:
        qs.append({"id": "text", "type": "choice",
                   "question": "If typing, which prepared text should be typed?",
                   "options": plan.texts[:max_options]})
    if len(plan.keys) >= 2:
        qs.append({"id": "key", "type": "choice",
                   "question": "If pressing a key, which key?", "options": plan.keys[:max_options]})
    qs += [
        {"id": "done", "type": "noul", "question": "Is the goal already fully achieved on this screen?"},
        {"id": "ready", "type": "noul",
         "question": "Is the screen settled and ready for input (not loading or animating)?"},
        {"id": "needs_confirmation", "type": "noul",
         "question": "Would the next action be irreversible or risky (send, delete, pay, "
                     "submit, close unsaved work), so a human should confirm it first?"},
        {"id": "abort", "type": "noul",
         "question": "Is something wrong (error, unexpected dialog, logged out, wrong app) "
                     "so the run should stop and hand back?"},
    ]
    return qs



@dataclass
class Thresholds:
    confirm: str = "gated"          # always | gated
    min_confidence: float = 0.9
    confirm_p: float = 0.2
    done_p: float = 0.5
    abort_p: float = 0.5
    ready_p: float = 0.5
    destructive_words: list = field(default_factory=lambda: list(DESTRUCTIVE_WORDS))


def interpret(plan: Plan, answers: list[dict], elements: list[dict], frame: Frame,
              screen: tuple[int, int], th: Thresholds, blind: bool) -> dict:
    """Turn one pass of answers into ``{kind, action?, gates, confidence}``;
    kind is ``done``, ``abort``, ``wait`` or ``act``."""
    a = {x["id"]: x for x in answers}
    p = lambda qid: float(a.get(qid, {}).get("p", 0.0))  # noqa: E731
    summary = {"done": p("done"), "ready": p("ready"),
               "needs_confirmation": p("needs_confirmation"), "abort": p("abort")}
    if summary["abort"] >= th.abort_p:
        return {"kind": "abort", "signals": summary, "gates": []}
    if summary["done"] >= th.done_p:
        return {"kind": "done", "signals": summary, "gates": []}
    if summary["ready"] < th.ready_p:
        return {"kind": "wait", "signals": summary, "gates": []}
    kind = a["action"]["choice"]
    used = [a["action"]]
    action: dict[str, Any] = {"type": kind}
    sx, sy = screen[0] / max(1, frame.width), screen[1] / max(1, frame.height)
    if kind in ("click", "double_click", "right_click"):
        if elements and "target" in a:
            labels = element_labels(elements, len(elements))
            idx = labels.index(a["target"]["choice"]) if a["target"]["choice"] in labels else 0
            e = elements[idx]
            x = float(e.get("x", 0)) + float(e.get("w", 0)) / 2
            y = float(e.get("y", 0)) + float(e.get("h", 0)) / 2
            action.update(target=labels[idx], x=round(x * sx), y=round(y * sy))
            used.append(a["target"])
        else:
            rows, cols = plan.grid_labels()
            r = rows.index(a["target_row"]["choice"]) if a["target_row"]["choice"] in rows else 0
            c = cols.index(a["target_col"]["choice"]) if a["target_col"]["choice"] in cols else 0
            action.update(target=f"cell r{r + 1}c{c + 1}",
                          x=round((c + 0.5) * screen[0] / plan.cols),
                          y=round((r + 0.5) * screen[1] / plan.rows))
            used += [a["target_row"], a["target_col"]]
    elif kind == "type":
        if "text" in a:
            action["text"] = a["text"]["choice"]
            used.append(a["text"])
        else:
            action["text"] = plan.texts[0]
    elif kind == "key":
        if "key" in a:
            action["key"] = a["key"]["choice"]
            used.append(a["key"])
        else:
            action["key"] = plan.keys[0]
    confidence = min(float(u.get("confidence", 0.0)) for u in used)
    gates: list[str] = []
    if th.confirm == "always":
        gates.append("policy")
    if blind:
        gates.append("blind")
    if confidence < th.min_confidence:
        gates.append("low_confidence")
    if summary["needs_confirmation"] >= th.confirm_p:
        gates.append("model_flagged")
    if looks_destructive(action, th.destructive_words):
        gates.append("destructive")
    return {"kind": "act", "action": action, "confidence": round(confidence, 4),
            "signals": summary, "gates": gates}


def looks_destructive(action: dict, words: list[str]) -> bool:
    key = str(action.get("key", "")).lower().replace(" ", "")
    if key and key in DESTRUCTIVE_KEYS:
        return True
    hay = " ".join(str(action.get(k, "")) for k in ("target", "text", "key")).lower()
    return any(w and str(w).lower() in hay for w in words)


# -- decide.drive: screenshot -> one decision pass -> input (0.4 drive.py) ------

DEFAULT_KEYS = ["enter", "escape", "tab", "backspace"]
TOUCH_KEYS = ["back", "home"]
OBSERVATION_CHARS = 4000     # keeps the prompt well inside the model's token budget
HISTORY = 5                  # previous actions shown to the model
KEEP_STEPS = 20              # steps in the reply


@dataclass
class Frame:
    width: int
    height: int


def actions_for(touch: bool, texts: list[str], keys: list[str]) -> list[str]:
    acts = ["click"] if touch else ["click", "double_click", "right_click"]
    if texts:
        acts.append("type")
    if keys:
        acts.append("key")
    return acts + ["scroll_down", "scroll_up", "wait"]


def input_calls(action: dict, touch: bool, screen: tuple[int, int]) -> list[tuple[str, dict]]:
    """The device-contract calls (core.yaml hid.*) that perform action."""
    kind = action["type"]
    if kind in ("click", "double_click", "right_click"):
        if touch and kind != "click":
            raise ValueError(f"{kind} isn't a touch-screen action")
        at = {"x": int(action["x"]), "y": int(action["y"])}
        if kind == "right_click":
            return [("hid.click", {**at, "button": "right"})]
        return [("hid.click", at)] * (2 if kind == "double_click" else 1)
    if kind == "type":
        return [("hid.type", {"text": str(action["text"])})]
    if kind == "key":
        *mods, key = [p.strip().lower() for p in str(action["key"]).split("+") if p.strip()]
        mods = [{"control": "ctrl", "super": "meta", "cmd": "meta", "win": "meta"}.get(m, m) for m in mods]
        return [("hid.key", {"key": key, **({"mods": mods} if mods else {})})]
    if kind in ("scroll_down", "scroll_up"):
        if touch:
            x, lo, hi = screen[0] // 2, int(screen[1] * 0.7), int(screen[1] * 0.3)
            y1, y2 = (lo, hi) if kind == "scroll_down" else (hi, lo)
            return [("hid.drag", {"x1": x, "y1": y1, "x2": x, "y2": y2, "duration": "300ms"})]
        return [("hid.key", {"key": "pagedown" if kind == "scroll_down" else "pageup"})]
    if kind == "wait":
        return []
    raise ValueError(f"unknown action {kind!r}")


def _brief(action: dict) -> str:
    kind = action["type"]
    if kind in ("click", "double_click", "right_click"):
        return f"{kind} {action.get('target')} at ({action.get('x')},{action.get('y')})"
    if kind == "type":
        return f"type {str(action.get('text'))[:40]!r}"
    if kind == "key":
        return f"key {action.get('key')}"
    return kind


async def drive(ctx, goal, screen_worker, input_worker=None, touch=False, dry_run=None, max_steps=None,
                max_seconds=120.0, texts=None, keys=None, screen_size=None) -> dict:
    """The loop runs inside this call. It ends on done, abort, a budget, the
    kill switch, a failed action, an action its gates say a person should
    confirm (returned as pending: do it yourself through hid.*, or ask
    first, then drive again), or an input call the hub holds (hold)."""
    s = ctx.settings
    if s.get("halt"):
        raise KitError("decide.halt (the kill switch) is on; turn it off to drive")
    input_worker = input_worker or screen_worker
    dry = s["dry_run"] if dry_run is None else bool(dry_run)
    steps_max = max_steps or (1 if dry else s["max_steps"])
    limits = LIMITS[s["adapter"]]
    rows, _, cols = str(s["grid"]).partition("x")
    grid = (int(rows), int(cols))
    if not all(2 <= n <= limits["max_options"] for n in grid):
        raise KitError(f"decide.grid sides must be 2..{limits['max_options']} for this model")
    size = tuple(int(n) for n in screen_size.split("x")) if screen_size else None
    texts = [str(t) for t in (texts or []) if str(t)][:limits["max_options"]]
    if keys is None:
        keys = TOUCH_KEYS if touch else DEFAULT_KEYS
    keys = [str(k).strip() for k in keys if str(k).strip()]
    plan = Plan(actions_for(touch, texts, keys), texts, keys, *grid)
    th = Thresholds(confirm=s["confirm"], min_confidence=float(s["min_confidence"]),
                    confirm_p=float(s["confirm_p"]), done_p=float(s["done_p"]), abort_p=float(s["abort_p"]),
                    destructive_words=list(s.get("destructive_words") or DESTRUCTIVE_WORDS))
    settle = parse_duration(s["settle"])
    started, history, rows_out = time.monotonic(), [], []

    def end(state: str, reason: str, **extra) -> dict:
        return {"state": state, "reason": reason, "goal": goal, "dry_run": dry, "steps": len(rows_out),
                "log": rows_out[-KEEP_STEPS:], **extra}

    while True:
        if s.get("halt"):
            return end("stopped", "halted (kill switch)")
        if time.monotonic() - started > max_seconds:
            return end("stopped", "time budget")
        if len(rows_out) >= steps_max:
            return end("finished", "step budget")
        shot = await ctx.call_on(screen_worker, "screen.capture", quality=60)
        raw = base64.b64decode(shot["data"]) if isinstance(shot, dict) and shot.get("data") else b""
        dims = jpeg_size(raw) or size
        if dims is None:
            raise KitError("can't read the screenshot's size; pass screen_size")
        frame, screen = Frame(*dims), size or dims
        obs = await _observe(ctx, screen_worker, raw, goal)
        elements = [e for e in obs["elements"] if isinstance(e, dict)]
        blind = not (obs["text"] or elements)
        state = {"task": "Drive a computer towards a goal, one input action at a time.", "goal": goal,
                 "step": len(rows_out) + 1, "max_steps": steps_max,
                 "screen": {"width": screen[0], "height": screen[1], "grid": f"{grid[0]} rows x {grid[1]} columns"},
                 "observation_source": obs["source"], "screen_text": obs["text"][:OBSERVATION_CHARS],
                 "elements": element_labels(elements, limits["max_options"]), "recent_actions": history[-HISTORY:]}
        res = await asyncio.to_thread(run, ctx, state, build_questions(plan, elements, limits["max_options"]))
        decision = interpret(plan, res["answers"], elements, frame, screen, th, blind)
        row = {"step": len(rows_out) + 1, "observation": obs["source"], "blind": blind, "decision": decision}
        rows_out.append(row)
        if decision["kind"] in ("done", "abort"):
            return end("finished" if decision["kind"] == "done" else "aborted",
                       "goal reached" if decision["kind"] == "done" else "the model asked to stop")
        if decision["kind"] == "wait":
            history.append({"step": row["step"], "action": "wait", "result": "screen not ready"})
            await asyncio.sleep(settle)
            continue
        action = decision["action"]
        try:
            calls = input_calls(action, touch, screen)
        except ValueError as e:
            return end("failed", str(e))
        row["calls"] = [{"cap": c, "args": a} for c, a in calls]
        if dry:
            history.append({"step": row["step"], "action": _brief(action), "result": "dry run: not executed"})
            continue
        if decision["gates"]:
            return end("needs_confirmation", "gated: " + ", ".join(decision["gates"]),
                       pending={"action": action, "calls": row["calls"], "gates": decision["gates"],
                                "confidence": decision["confidence"], "input_worker": input_worker})
        for cap, args in calls:
            try:
                await ctx.call_on(input_worker, cap, **args)
            except KitError as e:
                if "(hold " in e.message:
                    return end("held", e.message, hold=e.message.rpartition("(hold ")[2].rstrip(")"))
                return end("failed", f"{cap}: {e.message}")
        history.append({"step": row["step"], "action": _brief(action), "result": "ok"})
        await asyncio.sleep(settle)


async def _observe(ctx, worker: str, raw: bytes, goal: str) -> dict:
    """What the text-only model reads: the perceiver's text and elements, else
    the device's ui.screen, else nothing (a blind run gates every action)."""
    if ctx.settings.get("perceiver"):
        out = await asyncio.to_thread(_post, ctx, str(ctx.settings["perceiver"]), {
            "image": base64.b64encode(raw).decode("ascii"), "goal": goal})
        if isinstance(out, dict):
            return {"text": str(out.get("text") or ""), "source": "perceiver",
                    "elements": [e for e in out.get("elements") or () if isinstance(e, dict)]}
    try:
        ui = await ctx.call_on(worker, "ui.screen")
    except KitError:
        ui = None
    if isinstance(ui, dict) and ui.get("items"):
        items = [i for i in ui["items"] if isinstance(i, dict) and i.get("text")]
        return {"text": "\n".join(str(i["text"]) for i in items), "source": "ui.screen",
                "elements": [{"label": i["text"], "x": i.get("x", 0), "y": i.get("y", 0)} for i in items]}
    return {"text": "", "elements": [], "source": "none"}
