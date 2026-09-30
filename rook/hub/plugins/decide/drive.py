"""``decide.drive``: a screenshot -> one decision pass -> input loop.

Each frame:

1. **Capture** a screenshot on the screen worker (``screenshot.capture_preview``
   by default) and read its size from the JPEG header.
2. **Perceive**: turn the frame into text the (text-only) decision model can
   read. In order: the configured ``perceiver`` resource (an OCR / UI-parsing
   service returning ``{text, elements}``), else Android ``ui.text`` when the
   screen worker has it, else nothing (the run is *blind*: every action then
   needs confirmation).
3. **Ask** one batch of typed questions in one pass: the action type, the
   target (an element when the perceiver listed some, else a grid row and
   column), which prepared text to type or key to press, and yes/no
   questions ``done``, ``ready``, ``needs_confirmation`` and ``abort``.
4. **Gate**: stop on ``done`` / ``abort``; wait when not ``ready``; ask a
   human (``decide.confirm``) when the policy says so, the action looks
   destructive, the model says it needs confirmation, the answers are below
   the confidence floor, or the run is blind. The model's probabilities are
   uncalibrated, so the defaults lean towards asking.
5. **Act** through ``hid.*`` on the input worker (desktop backends or the
   Android accessibility backend), or, in dry-run mode, only record what it
   would have done.

Every step is journaled (:mod:`.journal`). The loop stops at the step or time
budget, on the kill switch (setting ``halt`` or ``decide.stop``), when a
confirmation is refused or times out, or when an action fails.

The loop only sees the band through injected callables, so tests drive it with
fake screenshot/hid caps and a fake decision server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("rook.hub.plugins.decide.drive")

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

OBSERVATION_CHARS = 4000     # keeps the prompt well inside the model's token budget
HISTORY = 5                  # previous actions shown to the model


# -- frames -------------------------------------------------------------------

def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker, stdlib only."""
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


def parse_size(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    w, _, h = str(s).lower().partition("x")
    if w.strip().isdigit() and h.strip().isdigit() and int(w) > 0 and int(h) > 0:
        return int(w), int(h)
    raise ValueError(f"screen size must look like 1920x1080, got {s!r}")


@dataclass
class Frame:
    width: int
    height: int
    bytes: int
    sha: str
    data_b64: str = field(repr=False, default="")

    def summary(self) -> dict:
        return {"width": self.width, "height": self.height, "bytes": self.bytes,
                "sha256": self.sha[:16]}


# -- questions ----------------------------------------------------------------

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


# -- interpretation ------------------------------------------------------------

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


# -- input backends --------------------------------------------------------------

#: Key names per hid backend. xdotool/wtype/ydotool take X keysyms; the Windows
#: backend takes its own lower-case names.
_KEYSYM = {"enter": "Return", "return": "Return", "escape": "Escape", "esc": "Escape",
           "tab": "Tab", "backspace": "BackSpace", "delete": "Delete", "space": "space",
           "pagedown": "Next", "pageup": "Prior", "home": "Home", "end": "End",
           "up": "Up", "down": "Down", "left": "Left", "right": "Right"}


def split_combo(combo: str) -> tuple[str, list[str]]:
    parts = [p.strip() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key")
    return parts[-1], parts[:-1]


def key_for_backend(key: str, backend: str) -> str:
    if backend in ("xdotool", "wtype", "ydotool"):
        return _KEYSYM.get(key.lower(), key)
    return key.lower()


def is_android(backend: str) -> bool:
    return backend.startswith("android")


def actions_for(backend: str, texts: list[str], keys: list[str]) -> list[str]:
    acts = ["click"]
    if not is_android(backend):
        acts += ["double_click", "right_click"]
    if texts:
        acts.append("type")
    if keys:
        acts.append("key")
    return acts + ["scroll_down", "scroll_up", "wait"]


def input_calls(action: dict, backend: str, screen: tuple[int, int]) -> list[tuple[str, dict]]:
    """The ``hid.*`` calls that perform ``action`` on ``backend``."""
    kind = action["type"]
    android = is_android(backend)
    if kind in ("click", "double_click", "right_click"):
        x, y = int(action["x"]), int(action["y"])
        if android:
            if kind != "click":
                raise ValueError(f"{kind} is not supported by the Android backend")
            return [("hid.mouse.click", {"x": x, "y": y})]
        button = 3 if kind == "right_click" else 1
        calls = [("hid.mouse.click", {"button": button, "x": x, "y": y})]
        if kind == "double_click":
            calls.append(("hid.mouse.click", {"button": 1}))
        return calls
    if kind == "type":
        return [("hid.type", {"text": str(action["text"])})]
    if kind == "key":
        if android:
            k = str(action["key"]).lower()
            return [("hid.key_combo", {"keys": {"escape": "back", "esc": "back"}.get(k, k)})]
        key, mods = split_combo(action["key"])
        return [("hid.key_combo", {"key": key_for_backend(key, backend),
                                   "modifiers": [m.lower() for m in mods]})]
    if kind in ("scroll_down", "scroll_up"):
        if android:
            x = screen[0] // 2
            lo, hi = int(screen[1] * 0.7), int(screen[1] * 0.3)
            y1, y2 = (lo, hi) if kind == "scroll_down" else (hi, lo)
            return [("hid.mouse.drag", {"x1": x, "y1": y1, "x2": x, "y2": y2, "duration": 0.3})]
        key = "pagedown" if kind == "scroll_down" else "pageup"
        return [("hid.key_combo", {"key": key_for_backend(key, backend), "modifiers": []})]
    if kind == "wait":
        return []
    raise ValueError(f"unknown action {kind!r}")


# -- the run ----------------------------------------------------------------------

CallFn = Callable[[str, dict, str, float], Awaitable[Any]]


@dataclass
class DriveConfig:
    goal: str
    screen_worker: str
    input_worker: str
    dry_run: bool = True
    max_steps: int = 20
    max_seconds: float = 120.0
    settle_s: float = 0.3
    confirm_timeout_s: float = 300.0
    screenshot_cap: str = "screenshot.capture_preview"
    grid: tuple[int, int] = (8, 8)
    screen_size: tuple[int, int] | None = None
    texts: list = field(default_factory=list)
    keys: list | None = None
    thresholds: Thresholds = field(default_factory=Thresholds)
    identity: str = ""


class DriveRun:
    """One drive. ``call(cap, args, worker, timeout)`` reaches the band;
    ``decide(state, questions)`` runs one decision pass (the plugin's client);
    ``perceive(frame, goal)`` returns ``{text, elements, source}`` or None;
    ``has_cap(worker, cap)`` checks the roster; ``journal`` records steps;
    ``halted()`` reads the kill-switch setting."""

    def __init__(self, cfg: DriveConfig, *, call: CallFn,
                 decide: Callable[[Any, list], Awaitable[dict]],
                 max_options: int,
                 perceive: Callable[[Frame, str], Awaitable[dict | None]] | None = None,
                 has_cap: Callable[[str, str], bool] = lambda w, c: False,
                 journal: Any = None,
                 halted: Callable[[], bool] = lambda: False,
                 run_id: str | None = None) -> None:
        self.id = run_id or uuid.uuid4().hex[:12]
        self.cfg = cfg
        self._call = call
        self._decide = decide
        self._perceive = perceive
        self._has_cap = has_cap
        self._journal = journal
        self._halted = halted
        self.max_options = max_options
        self.state = "starting"
        self.reason = ""
        self.step = 0
        self.started = time.time()
        self.finished: float | None = None
        self.pending: dict | None = None
        self.history: list[dict] = []
        self.backend = "unknown"
        self._stop = False
        self._decision: asyncio.Future | None = None
        self.task: asyncio.Task | None = None

    # -- control ------------------------------------------------------------
    def stop(self, reason: str = "stopped") -> None:
        """Kill switch for this run: no further input is sent."""
        self._stop = True
        if self.reason == "":
            self.reason = reason
        if self._decision is not None and not self._decision.done():
            self._decision.set_result(False)

    def confirm(self, approve: bool, by: str = "", note: str = "") -> dict:
        if self.pending is None or self._decision is None or self._decision.done():
            raise ValueError(f"run {self.id} is not waiting for a confirmation")
        self.pending["answer"] = {"approve": bool(approve), "by": by, "note": note[:200]}
        self._decision.set_result(bool(approve))
        return {"run_id": self.id, "approved": bool(approve), "step": self.pending["step"]}

    def snapshot(self) -> dict:
        out = {"run_id": self.id, "state": self.state, "reason": self.reason,
               "goal": self.cfg.goal, "dry_run": self.cfg.dry_run, "step": self.step,
               "max_steps": self.cfg.max_steps, "screen_worker": self.cfg.screen_worker,
               "input_worker": self.cfg.input_worker, "backend": self.backend,
               "started": self.started, "finished": self.finished}
        if self.pending and self.state == "awaiting_confirmation":
            out["pending"] = {k: v for k, v in self.pending.items() if k != "answer"}
        return out

    # -- helpers ------------------------------------------------------------
    def _should_stop(self) -> str:
        if self._stop:
            return self.reason or "stopped"
        if self._halted():
            return "halted (kill switch)"
        if time.time() - self.started > self.cfg.max_seconds:
            return "time budget"
        return ""

    def _record(self, row: dict) -> None:
        if self._journal is not None:
            try:
                self._journal.step(self.id, row)
            except Exception:
                log.exception("drive %s: journaling step failed", self.id)

    def _set(self, state: str, reason: str = "") -> None:
        self.state = state
        if reason:
            self.reason = reason
        if self._journal is not None:
            try:
                self._journal.update_run(self.id, state=state, reason=self.reason,
                                         steps=self.step, backend=self.backend,
                                         finished=self.finished)
            except Exception:
                log.exception("drive %s: journaling state failed", self.id)

    async def _capture(self) -> Frame:
        shot = await self._call(self.cfg.screenshot_cap, {}, self.cfg.screen_worker, 20.0)
        if not isinstance(shot, dict) or shot.get("ok") is False or not shot.get("data"):
            err = shot.get("error") if isinstance(shot, dict) else shot
            raise RuntimeError(f"screenshot failed: {err}")
        raw = base64.b64decode(shot["data"])
        size = jpeg_size(raw) or self.cfg.screen_size
        if size is None:
            raise RuntimeError("cannot read the screenshot size; set screen_size")
        return Frame(size[0], size[1], len(raw), hashlib.sha256(raw).hexdigest(), shot["data"])

    async def _observe(self, frame: Frame) -> dict:
        if self._perceive is not None:
            obs = await self._perceive(frame, self.cfg.goal)
            if obs:
                return obs
        if self._has_cap(self.cfg.screen_worker, "ui.text"):
            res = await self._call("ui.text", {}, self.cfg.screen_worker, 10.0)
            if isinstance(res, dict) and res.get("ok") is not False and res.get("text"):
                return {"text": str(res["text"]), "elements": [], "source": "ui.text"}
        return {"text": "", "elements": [], "source": "none"}

    def _state(self, frame: Frame, screen: tuple[int, int], obs: dict) -> dict:
        return {
            "task": "Drive a computer towards a goal, one input action at a time.",
            "goal": self.cfg.goal,
            "step": self.step, "max_steps": self.cfg.max_steps,
            "screen": {"width": screen[0], "height": screen[1],
                       "grid": f"{self.cfg.grid[0]} rows x {self.cfg.grid[1]} columns"},
            "observation_source": obs.get("source", "none"),
            "screen_text": str(obs.get("text") or "")[:OBSERVATION_CHARS],
            "elements": [str(e.get("label") or e.get("text") or "")[:80]
                         for e in (obs.get("elements") or [])[:self.max_options]],
            "recent_actions": self.history[-HISTORY:],
        }

    async def _await_confirmation(self, step_row: dict) -> bool:
        loop = asyncio.get_running_loop()
        self._decision = loop.create_future()
        self.pending = {"step": self.step, "action": step_row["decision"].get("action"),
                        "gates": step_row["decision"].get("gates"),
                        "confidence": step_row["decision"].get("confidence"),
                        "expires": time.time() + self.cfg.confirm_timeout_s}
        self._set("awaiting_confirmation")
        try:
            while not self._decision.done():
                if self._should_stop():
                    self._decision.set_result(False)
                    break
                if time.time() >= self.pending["expires"]:
                    self.reason = "confirmation timed out"
                    self._decision.set_result(False)
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(self._decision), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            return bool(self._decision.result())
        finally:
            if self.state == "awaiting_confirmation":
                self._set("running")

    async def _execute(self, action: dict, screen: tuple[int, int]) -> list[dict]:
        results = []
        for cap, args in input_calls(action, self.backend, screen):
            res = await self._call(cap, args, self.cfg.input_worker, 15.0)
            ok = not (isinstance(res, dict) and res.get("ok") is False)
            results.append({"cap": cap, "args": args, "ok": ok,
                            **({} if ok else {"error": str(res.get("error"))[:300]})})
            if not ok:
                break
        return results

    # -- the loop -----------------------------------------------------------
    async def run(self) -> dict:
        try:
            await self._run()
        except asyncio.CancelledError:
            self.reason = self.reason or "cancelled"
            self.finished = time.time()
            self._set("stopped")
            raise
        except Exception as e:
            log.exception("drive %s failed", self.id)
            self.finished = time.time()
            self._set("failed", f"{type(e).__name__}: {e}")
        return self.snapshot()

    async def _run(self) -> None:
        self._set("running")
        if self.cfg.dry_run and not self._has_cap(self.cfg.input_worker, "hid.backend"):
            self.backend = "dry-run"
        else:
            info = await self._call("hid.backend", {}, self.cfg.input_worker, 10.0)
            self.backend = str((info or {}).get("backend") or "unknown")
        keys = self.cfg.keys if self.cfg.keys is not None else (
            ANDROID_KEYS if is_android(self.backend) else DEFAULT_KEYS)
        plan = Plan(actions_for(self.backend, self.cfg.texts, keys), list(self.cfg.texts),
                    list(keys), *self.cfg.grid)
        while True:
            why = self._should_stop()
            if why:
                return self._finish("stopped", why)
            if self.step >= self.cfg.max_steps:
                return self._finish("finished", "step budget")
            self.step += 1
            t0 = time.perf_counter()
            frame = await self._capture()
            screen = self.cfg.screen_size or (frame.width, frame.height)
            obs = await self._observe(frame)
            elements = [e for e in (obs.get("elements") or []) if isinstance(e, dict)]
            blind = not (obs.get("text") or elements)
            qs = build_questions(plan, elements, self.max_options)
            res = await self._decide(self._state(frame, screen, obs), qs)
            decision = interpret(plan, res["answers"], elements, frame, screen,
                                 self.cfg.thresholds, blind)
            row: dict[str, Any] = {
                "frame": frame.summary(), "observation": obs.get("source"),
                "blind": blind, "answers": res["answers"], "decision": decision,
                "model_ms": res.get("latency_ms"), "dry_run": self.cfg.dry_run}
            kind = decision["kind"]
            if kind in ("done", "abort"):
                row["outcome"] = kind
                row["step_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                self._record(row)
                return self._finish("finished" if kind == "done" else "aborted",
                                    "goal reached" if kind == "done" else "model asked to abort")
            if kind == "wait":
                row["outcome"] = "waited"
                self._record(row)
                self.history.append({"step": self.step, "action": "wait", "result": "screen not ready"})
                await asyncio.sleep(self.cfg.settle_s)
                continue
            action = decision["action"]
            if self.cfg.dry_run:
                row["outcome"] = "dry_run"
                row["would_confirm"] = bool(decision["gates"])
                row["intended_calls"] = [{"cap": c, "args": a} for c, a in
                                         _safe_calls(action, self.backend, screen)]
                row["step_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                self._record(row)
                self.history.append({"step": self.step, "action": _brief(action),
                                     "result": "dry run: not executed"})
                continue
            if decision["gates"]:
                approved = await self._await_confirmation(row)
                row["confirmation"] = (self.pending or {}).get("answer") or {
                    "approve": False, "reason": self.reason or "no answer"}
                if not approved:
                    row["outcome"] = "refused"
                    self._record(row)
                    why = self._should_stop() or self.reason or "confirmation refused"
                    return self._finish("stopped", why)
            if self._should_stop():
                row["outcome"] = "stopped"
                self._record(row)
                return self._finish("stopped", self._should_stop())
            results = await self._execute(action, screen)
            row["executed"] = results
            ok = all(r["ok"] for r in results)
            row["outcome"] = "acted" if ok else "action_failed"
            row["step_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self._record(row)
            self.history.append({"step": self.step, "action": _brief(action),
                                 "result": "ok" if ok else "failed"})
            if not ok:
                return self._finish("failed", f"{results[-1]['cap']}: {results[-1].get('error')}")
            await asyncio.sleep(self.cfg.settle_s)

    def _finish(self, state: str, reason: str) -> None:
        self.finished = time.time()
        self.pending = None
        self.reason = reason
        self._set(state, reason)


def _safe_calls(action: dict, backend: str, screen: tuple[int, int]) -> list:
    try:
        return input_calls(action, backend if backend != "dry-run" else "xdotool", screen)
    except ValueError as e:
        return [("unsupported", {"error": str(e)})]


def _brief(action: dict) -> str:
    kind = action["type"]
    if kind in ("click", "double_click", "right_click"):
        return f"{kind} {action.get('target')} at ({action.get('x')},{action.get('y')})"
    if kind == "type":
        return f"type {str(action.get('text'))[:40]!r}"
    if kind == "key":
        return f"key {action.get('key')}"
    return kind
