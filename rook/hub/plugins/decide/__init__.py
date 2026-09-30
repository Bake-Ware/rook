"""``decide.*``: one-pass decision models as a hub plugin (worker ``rook``).

A one-pass decision model (a masked-diffusion "diffucision" model, or the
typed decision engine) answers a batch of typed questions about one state in
a single forward pass, fast enough to sit inside a control loop. This plugin
puts one behind the band:

* ``decide.run`` (read): a batch of choice / score / yes-no questions against
  a state, answered in one pass (split into several when the batch exceeds
  the model's limit).
* ``decide.health`` / ``decide.info`` (read): the configured model.
* ``decide.drive`` (exec, physical): drive a screen towards a goal. Each
  frame is captured on a screen worker, turned into questions, answered in
  one pass and executed through ``hid.*`` on an input worker, behind
  confirmation gates, a kill switch and a step/time budget. Dry-run (log the
  intended actions only) is the default.
* ``decide.runs`` (read), ``decide.confirm`` (exec), ``decide.stop`` (write):
  inspect, approve or kill drives.

The model is a connection string (setting ``endpoint``): ``http(s)://`` to
the service, or ``cap://<worker>/<cap>`` to reach it through a band cap. See
docs/design/decide.md.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from ....core.context import current_identity
from ....core.plugin import Plugin, Resource, capability, place, resource, setting
from .client import ADAPTERS, DecisionClient, DecisionError, _aiohttp_request
from .drive import (DESTRUCTIVE_WORDS, DriveConfig, DriveRun, Frame, Thresholds,
                    parse_size)
from .journal import DriveJournal

log = logging.getLogger("rook.hub.plugins.decide")

ACTIVE = ("starting", "running", "awaiting_confirmation")


class Decide(Plugin):
    NAMESPACE = "decide"
    NAME = "decide"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    MIGRATIONS = "migrations"
    SETTINGS = (
        # -- model ---------------------------------------------------------
        resource("endpoint", group="Model", order=1, label="Decision model endpoint",
                 help="http(s)://host:port of the decision service, or cap://<worker>/<cap> "
                      "for a band cap that wraps it (e.g. a worker custom cap "
                      "cmd.decide-run). Blank: decide.* is not configured."),
        setting("adapter", str, "diffucision", choices=ADAPTERS, group="Model", order=2,
                label="API dialect",
                help="diffucision: 64 questions / 26 options, temperature. decision-engine: "
                     "the typed decision engine, 24 questions / 52 options."),
        setting("token", str, secret=True, group="Model", order=3, label="Bearer token",
                help="Sent as Authorization: Bearer to http(s) endpoints that require one."),
        setting("timeout_s", float, 10.0, min=0.5, max=120.0, group="Model", order=4,
                label="Request timeout (s)"),
        setting("temperature", float, None, min=0.05, max=10.0, group="Model", order=5,
                advanced=True, label="Softmax temperature",
                help="Blank: the service's default. Only the diffucision dialect accepts it."),
        # -- drive ---------------------------------------------------------
        setting("dry_run", bool, True, group="Drive", order=1, label="Dry run by default",
                help="Drives only journal the actions they would take unless the caller "
                     "passes dry_run=false."),
        setting("max_steps", int, 20, min=1, max=500, group="Drive", order=2,
                label="Step budget per drive"),
        setting("max_seconds", int, 120, min=5, max=3600, group="Drive", order=3,
                label="Time budget per drive (s)"),
        setting("settle_ms", int, 300, min=0, max=10000, group="Drive", order=4,
                label="Settle time after an action (ms)"),
        setting("screenshot_cap", str, "screenshot.capture_preview", group="Drive", order=5,
                pattern=r"[a-z][\w.-]*", label="Screenshot cap",
                help="Called on the screen worker each frame; must return {data: base64 JPEG}."),
        resource("perceiver", group="Drive", order=6, label="Perception service",
                 help="Optional http(s):// or cap:// service that turns a screenshot into "
                      "{text, elements: [{label, x, y, w, h}]} for the text-only model. "
                      "Blank: Android ui.text where available, else blind."),
        setting("grid", str, "8x8", pattern=r"[1-9]\d?x[1-9]\d?", group="Drive", order=7,
                label="Target grid (rows x columns)",
                help="Used when no perceiver lists elements; each side at most the model's "
                     "option limit."),
        setting("screen_size", str, "", pattern=r"(\d+x\d+)?", group="Drive", order=8,
                advanced=True, label="Screen size override (WxH)",
                help="Input coordinates are scaled to this size when screenshots are "
                     "downscaled. Blank: the screenshot's own size."),
        # -- safety --------------------------------------------------------
        setting("halt", bool, False, group="Safety", order=1, label="Kill switch",
                help="On: every running drive stops before its next input and new drives "
                     "are refused."),
        setting("confirm", str, "gated", choices=("gated", "always"), group="Safety", order=2,
                label="Confirmation policy",
                help="always: every action waits for decide.confirm. gated: destructive-looking, "
                     "model-flagged, low-confidence and blind actions wait."),
        setting("min_confidence", float, 0.9, min=0.0, max=1.0, group="Safety", order=3,
                label="Confidence floor",
                help="Actions whose answers have a top-two margin below this need confirmation. "
                     "Probabilities are uncalibrated, so keep it high."),
        setting("confirm_p", float, 0.2, min=0.0, max=1.0, group="Safety", order=4,
                label="needs_confirmation threshold",
                help="Ask a human when P(needs confirmation) is at least this."),
        setting("done_p", float, 0.5, min=0.0, max=1.0, group="Safety", order=5,
                advanced=True, label="done threshold"),
        setting("abort_p", float, 0.5, min=0.0, max=1.0, group="Safety", order=6,
                advanced=True, label="abort threshold"),
        setting("confirm_timeout_s", int, 300, min=5, max=3600, group="Safety", order=7,
                label="Confirmation timeout (s)", help="Unanswered confirmations stop the drive."),
        setting("destructive_words", list, DESTRUCTIVE_WORDS, group="Safety", order=8,
                advanced=True, label="Destructive words",
                help="An action whose target, text or key contains one of these needs "
                     "confirmation."),
    )
    GUIDANCE = {
        "decide.run": "Probabilities are uncalibrated: treat them as a ranking, not a "
                      "guarantee. Batch every question about one state into one call.",
        "decide.drive": "Starts in dry-run unless dry_run=false. Poll decide.runs(run_id) and "
                        "answer decide.confirm when it waits; decide.stop is the kill switch.",
    }
    SKILL = (
        "### decide\n"
        "One-pass decision model (worker `rook`). `decide.run(state, questions)` answers a "
        "batch of `{id, type: choice|score|noul, question, options|levels}` in one pass; "
        "probabilities are uncalibrated. `decide.drive(goal, screen_worker, input_worker?, "
        "dry_run?, texts?, keys?)` runs screenshot -> decide -> `hid.*` with confirmation "
        "gates and returns a `run_id`; dry-run is the default. Watch it with "
        "`decide.runs(run_id=...)`, approve with `decide.confirm(run_id, approve)`, kill "
        "with `decide.stop()`.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None
        self.journal: DriveJournal | None = None
        self.drives: dict[str, DriveRun] = {}
        self._http = _aiohttp_request     # tests swap in a fake server
        self._roster = None               # tests: callable() -> {wid: {name, caps}}

    def bind_host(self, node) -> None:
        self._node = node

    async def start(self) -> None:
        self._open_journal()

    async def stop(self) -> None:
        for run in list(self.drives.values()):
            run.stop("hub stopping")
        tasks = [r.task for r in self.drives.values() if r.task and not r.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _open_journal(self) -> DriveJournal:
        if self.journal is None:
            conn = sqlite3.connect(self.data_dir / "drive.db", check_same_thread=False)
            self.migrate(conn)
            self.journal = DriveJournal(conn)
            n = self.journal.mark_interrupted()
            if n:
                log.info("decide: %d drive(s) from a previous run marked stopped", n)
        return self.journal

    # -- band access -----------------------------------------------------
    async def _call(self, cap: str, args: dict, worker: str, timeout: float) -> Any:
        res = Resource(f"cap://{worker}/{cap}", "cap", worker, cap,
                       self.__dict__.get("_cap_caller"))
        return await res.call(args, timeout=timeout)

    def _workers(self) -> dict:
        if self._roster is not None:
            return self._roster()
        client = getattr(self._node, "client", None)
        try:
            return dict(client.workers) if client is not None else {}
        except Exception:
            return {}

    def _caps_of(self, worker: str) -> list[str] | None:
        """Caps of a worker by name or id; None when the roster is unknown."""
        roster = self._workers()
        if not roster:
            return None
        if worker in roster:
            return list(roster[worker].get("caps") or [])
        named = [w for w in roster.values() if (w.get("name") or "").lower() == worker.lower()]
        return list(named[0].get("caps") or []) if len(named) == 1 else []

    def _has_cap(self, worker: str, cap: str) -> bool:
        caps = self._caps_of(worker)
        return bool(caps) and cap in caps

    # -- the model -------------------------------------------------------
    def client(self) -> DecisionClient:
        res = self.resource("endpoint")
        if res is None:
            raise DecisionError("decide is not configured: set the decide.endpoint setting "
                                "(http(s)://… or cap://<worker>/<cap>)")
        if res.scheme not in ("http", "https", "cap"):
            raise DecisionError(f"decide.endpoint must be http(s):// or cap://, got {res.scheme}://")
        return DecisionClient(res, adapter=self.settings["adapter"],
                              token=self.settings.get("token"),
                              timeout=float(self.settings["timeout_s"]), http=self._http)

    @capability("run", risk="read")
    async def run(self, state: Any, questions: list, temperature: float | None = None) -> dict:
        """Answer a batch of typed questions about ``state`` in one pass.

        ``questions``: ``[{id, type, question, options|levels}]`` where
        ``type`` is ``choice`` (pick one option), ``score`` (an ordered
        ``levels`` list; returns ``level`` and the expected ``score``) or
        ``noul`` (yes/no; also ``yes-no``; returns ``p`` = P(yes)). ``state``
        is text or JSON. Every answer carries ``probabilities`` and
        ``confidence`` (top-two margin). Probabilities are uncalibrated."""
        if temperature is None:
            temperature = self.settings.get("temperature")
        return await self.client().decide(state, questions, temperature)

    @capability("health", risk="read")
    async def health(self) -> dict:
        """Is the configured decision model reachable and loaded?"""
        try:
            client = self.client()
        except DecisionError as e:
            return {"ok": False, "ready": False, "configured": False, "error": str(e)}
        return {"configured": True, **(await client.health())}

    @capability("info", risk="read")
    async def info(self) -> dict:
        """The configured model: name, adapter, calibration status and limits."""
        return await self.client().info()

    # -- drive -----------------------------------------------------------
    async def _perceive(self, frame: Frame, goal: str) -> dict | None:
        res = self.resource("perceiver")
        if res is None:
            return None
        body = {"image": frame.data_b64, "format": "jpeg", "width": frame.width,
                "height": frame.height, "goal": goal}
        timeout = float(self.settings["timeout_s"])
        if res.scheme == "cap":
            out = await res.call(body, timeout=timeout)
        elif res.scheme in ("http", "https"):
            status, out = await self._http("POST", res.url, body,
                                           {"Content-Type": "application/json"}, timeout)
            if status >= 400:
                raise RuntimeError(f"perceiver returned HTTP {status}")
        else:
            raise RuntimeError(f"perceiver must be http(s):// or cap://, got {res.scheme}://")
        if not isinstance(out, dict) or out.get("ok") is False:
            raise RuntimeError(f"perceiver failed: {out.get('error') if isinstance(out, dict) else out}")
        elements = [e for e in (out.get("elements") or []) if isinstance(e, dict)]
        return {"text": str(out.get("text") or ""), "elements": elements, "source": "perceiver"}

    def _grid(self, max_options: int) -> tuple[int, int]:
        r, _, c = str(self.settings["grid"]).lower().partition("x")
        rows, cols = int(r), int(c)
        if not (2 <= rows <= max_options and 2 <= cols <= max_options):
            raise ValueError(f"decide.grid sides must be 2..{max_options} for this model")
        return rows, cols

    @capability("drive", risk="exec", tags=("physical",))
    async def drive(self, goal: str, screen_worker: str, input_worker: str = "",
                    dry_run: bool | None = None, max_steps: int | None = None,
                    max_seconds: float | None = None, texts: list | None = None,
                    keys: list | None = None, screen_size: str = "",
                    wait: float = 0.0) -> dict:
        """Drive a screen towards ``goal``: screenshot -> one decision pass -> hid input.

        ``screen_worker`` has the screenshot cap; ``input_worker`` (default:
        the same) has ``hid.*``. ``texts``: strings the model may choose to
        type (it cannot write its own). ``keys``: keys it may press (default
        enter/escape/tab/backspace; back/home/recents on Android). Dry-run
        (setting ``dry_run``, default on) journals the intended actions without
        sending input and stops after one frame unless ``max_steps`` is given.
        Returns at once with ``run_id`` (or after ``wait`` seconds / when the
        run ends). Gated actions wait for ``decide.confirm``."""
        if self.settings["halt"]:
            raise PermissionError("decide.halt (kill switch) is on; turn it off to drive")
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("goal is required")
        screen_worker = str(screen_worker or "").strip()
        if not screen_worker:
            raise ValueError("screen_worker is required")
        input_worker = str(input_worker or "").strip() or screen_worker
        client = self.client()
        dry = self.settings["dry_run"] if dry_run is None else bool(dry_run)
        shot_cap = self.settings["screenshot_cap"]
        caps = self._caps_of(screen_worker)
        if caps is not None and shot_cap not in caps:
            raise LookupError(f"worker {screen_worker!r} has no {shot_cap}")
        if not dry:
            icaps = self._caps_of(input_worker)
            if icaps is not None and not any(c.startswith("hid.") for c in icaps):
                raise LookupError(f"worker {input_worker!r} has no hid.* caps")
            busy = [r.id for r in self.drives.values()
                    if r.state in ACTIVE and not r.cfg.dry_run and r.cfg.input_worker == input_worker]
            if busy:
                raise RuntimeError(f"drive {busy[0]} is already sending input to {input_worker!r}")
        steps = int(max_steps) if max_steps is not None else (1 if dry else self.settings["max_steps"])
        if not 1 <= steps <= 500:
            raise ValueError("max_steps must be 1..500")
        seconds = float(max_seconds) if max_seconds is not None else float(self.settings["max_seconds"])
        texts = [str(t) for t in (texts or []) if str(t)]
        keys = None if keys is None else [str(k).strip() for k in keys if str(k).strip()]
        max_opts = client.limits["max_options"]
        th = Thresholds(confirm=self.settings["confirm"],
                        min_confidence=float(self.settings["min_confidence"]),
                        confirm_p=float(self.settings["confirm_p"]),
                        done_p=float(self.settings["done_p"]),
                        abort_p=float(self.settings["abort_p"]),
                        destructive_words=list(self.settings["destructive_words"] or []))
        cfg = DriveConfig(goal=goal[:1000], screen_worker=screen_worker,
                          input_worker=input_worker, dry_run=dry, max_steps=steps,
                          max_seconds=max(1.0, min(seconds, 3600.0)),
                          settle_s=self.settings["settle_ms"] / 1000.0,
                          confirm_timeout_s=float(self.settings["confirm_timeout_s"]),
                          screenshot_cap=shot_cap, grid=self._grid(max_opts),
                          screen_size=parse_size(screen_size or self.settings["screen_size"]),
                          texts=texts[:max_opts], keys=keys, thresholds=th,
                          identity=current_identity() or "")
        temperature = self.settings.get("temperature")

        async def one_pass(state, questions):
            return await client.decide(state, questions, temperature)

        journal = self._open_journal()
        run = DriveRun(cfg, call=self._call, decide=one_pass, max_options=max_opts,
                       perceive=self._perceive, has_cap=self._has_cap, journal=journal,
                       halted=lambda: bool(self.settings["halt"]))
        journal.new_run(run.id, identity=cfg.identity, goal=cfg.goal,
                        screen_worker=screen_worker, input_worker=input_worker, dry_run=dry,
                        config={"max_steps": steps, "max_seconds": cfg.max_seconds,
                                "grid": list(cfg.grid), "texts": len(texts),
                                "keys": keys, "confirm": th.confirm,
                                "min_confidence": th.min_confidence,
                                "adapter": client.adapter})
        self.drives[run.id] = run
        self._prune()
        run.task = asyncio.create_task(run.run(), name=f"decide.drive {run.id}")
        if wait and wait > 0:
            try:
                await asyncio.wait_for(asyncio.shield(run.task), timeout=min(float(wait), 600.0))
            except asyncio.TimeoutError:
                pass
        return run.snapshot()

    def _prune(self, keep: int = 50) -> None:
        done = [rid for rid, r in self.drives.items() if r.state not in ACTIVE]
        for rid in done[:-keep] if len(done) > keep else []:
            self.drives.pop(rid, None)

    @capability("runs", risk="read", limit=20)
    def runs(self, run_id: str = "", steps: int = 5) -> dict:
        """Recent drives, or one drive with its last ``steps`` journaled frames
        (answers, decision, gates, executed calls) and any pending confirmation."""
        journal = self._open_journal()
        if not run_id:
            items = journal.runs(100)  # core trims to the caller's limit (default 20)
            for it in items:
                live = self.drives.get(it["id"])
                if live is not None:
                    it["state"], it["steps"] = live.state, live.step
            return {"items": items}
        row = journal.run(run_id)
        if row is None:
            raise KeyError(f"no drive {run_id!r}")
        live = self.drives.get(run_id)
        if live is not None:
            row.update(live.snapshot())
        row["recent_steps"] = journal.steps(run_id, max(0, min(int(steps), 100)))
        return row

    @capability("confirm", risk="exec", tags=("physical",))
    def confirm(self, run_id: str, approve: bool, note: str = "") -> dict:
        """Approve (``approve=true``) or refuse the action a drive is waiting on.
        Refusing stops the drive."""
        run = self.drives.get(run_id)
        if run is None:
            raise KeyError(f"no live drive {run_id!r}")
        if approve and self.settings["halt"]:
            raise PermissionError("decide.halt (kill switch) is on")
        return run.confirm(bool(approve), by=current_identity() or "", note=str(note or ""))

    @capability("stop", risk="write")
    def stop_drive(self, run_id: str = "") -> dict:
        """Kill switch: stop one drive (``run_id``) or every live drive. No
        further input is sent; a pending confirmation is refused."""
        who = current_identity() or "unknown"
        stopped = []
        for rid, run in list(self.drives.items()):
            if (not run_id or rid == run_id) and run.state in ACTIVE:
                run.stop(f"stopped by {who}")
                stopped.append(rid)
        if run_id and not stopped and run_id not in self.drives:
            raise KeyError(f"no live drive {run_id!r}")
        return {"stopped": stopped}

    def heartbeat(self) -> dict | None:
        live = sum(1 for r in self.drives.values() if r.state in ACTIVE)
        return {"drives": live} if live else None


PLUGIN = Decide
