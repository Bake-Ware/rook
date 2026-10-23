"""Per-run results: a JSON record and a Markdown summary.

A run is one (hardware, model, trust) cell of the matrix. Each scenario
records its steps; each step has a status (pass, fail, skip), a duration,
the human touches a real person would have needed, tokens where the hub
reports them, and notes.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field

PASS, FAIL, SKIP = "pass", "fail", "skip"


@dataclass
class Step:
    scenario: str
    name: str
    status: str = SKIP
    started: float = 0.0
    seconds: float = 0.0
    human_touches: int = 0
    touches: list = field(default_factory=list)   # what a person would have had to do
    tokens: dict = field(default_factory=dict)    # {"input": n, "output": n, ...}
    notes: list = field(default_factory=list)
    error: str = ""


@dataclass
class Run:
    run_id: str
    hardware: str
    model: str
    trust: str
    rook_ref: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    machines: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    first_useful_result_s: float | None = None
    findings: list = field(default_factory=list)  # bugs / friction seen, one line each
    env: dict = field(default_factory=dict)       # what fitted: model, RAM, speeds

    def step(self, scenario: str, name: str) -> "StepCtx":
        s = Step(scenario=scenario, name=name)
        self.steps.append(s)
        return StepCtx(self, s)

    def mark_first_useful(self):
        if self.first_useful_result_s is None:
            self.first_useful_result_s = round(time.time() - self.started, 1)

    # -- summaries ---------------------------------------------------------
    def totals(self) -> dict:
        counted = [s for s in self.steps if s.status != SKIP]
        tokens: dict = {}
        for s in self.steps:
            for k, v in (s.tokens or {}).items():
                if isinstance(v, (int, float)):
                    tokens[k] = tokens.get(k, 0) + v
        return {
            "steps": len(self.steps),
            "passed": sum(s.status == PASS for s in self.steps),
            "failed": sum(s.status == FAIL for s in self.steps),
            "skipped": sum(s.status == SKIP for s in self.steps),
            "success_rate": round(sum(s.status == PASS for s in counted) / len(counted), 3) if counted else None,
            "human_touches": sum(s.human_touches for s in self.steps),
            "tokens": tokens,
            "wall_s": round((self.finished or time.time()) - self.started, 1),
            "first_useful_result_s": self.first_useful_result_s,
        }

    def to_json(self) -> dict:
        d = asdict(self)
        d["totals"] = self.totals()
        return d

    def to_markdown(self) -> str:
        t = self.totals()
        lines = [
            f"# Clean-room run {self.run_id}",
            "",
            f"- Profile: hardware **{self.hardware}**, model **{self.model}**, trust **{self.trust}**",
            f"- Rook: `{self.rook_ref}`",
            f"- Started {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(self.started))}, "
            f"wall time {t['wall_s']:.0f} s",
            f"- Steps: {t['passed']} passed, {t['failed']} failed, {t['skipped']} skipped"
            + (f" (success rate {t['success_rate']:.0%})" if t["success_rate"] is not None else ""),
            f"- Time to first useful result (an agent answer or a PR, from a bare VM): "
            + (f"{t['first_useful_result_s']:.0f} s" if t["first_useful_result_s"] is not None else "never"),
            f"- Human touches a real person would need: {t['human_touches']}",
            f"- Tokens: " + (", ".join(f"{k} {v}" for k, v in t["tokens"].items()) or "not reported"),
            "",
        ]
        if self.machines:
            lines += ["## Machines", ""]
            for m in self.machines:
                lines.append(f"- {m.get('role')}: {m.get('memory_mb')} MB, {m.get('cores')} cores ({m.get('provider')})")
            lines.append("")
        if self.env:
            lines += ["## Environment", ""]
            for k, v in self.env.items():
                lines.append(f"- {k}: {v}")
            lines.append("")
        lines += ["## Steps", "", "| Scenario | Step | Result | Time | Touches | Tokens | Notes |",
                  "|---|---|---|---|---|---|---|"]
        for s in self.steps:
            tok = ", ".join(f"{k} {v}" for k, v in (s.tokens or {}).items()) or ""
            note = "; ".join(s.notes + ([f"ERROR: {s.error}"] if s.error else []) + s.touches)
            note = note.replace("|", "/").replace("\n", " ")
            if len(note) > 400:
                note = note[:397] + "..."
            lines.append(f"| {s.scenario} | {s.name} | {s.status} | {s.seconds:.0f} s | {s.human_touches} | {tok} | {note} |")
        lines.append("")
        if self.findings:
            lines += ["## Findings", ""] + [f"- {f}" for f in self.findings] + [""]
        return "\n".join(lines)

    def write(self, directory: str) -> tuple[str, str]:
        os.makedirs(directory, exist_ok=True)
        self.finished = self.finished or time.time()
        base = os.path.join(directory, self.run_id)
        with open(base + ".json", "w") as f:
            json.dump(self.to_json(), f, indent=2)
        with open(base + ".md", "w") as f:
            f.write(self.to_markdown())
        return base + ".json", base + ".md"


class StepCtx:
    """`with run.step(...) as s:` times a step; an exception marks it failed."""

    def __init__(self, run: Run, step: Step):
        self.run, self.s = run, step

    def __enter__(self):
        self.s.started = time.time()
        self.s.status = PASS
        return self

    def __exit__(self, et, ev, tb):
        self.s.seconds = round(time.time() - self.s.started, 1)
        if et is not None:
            if et is Skip:
                self.s.status = SKIP
                self.s.notes.append(str(ev))
            else:
                self.s.status = FAIL
                self.s.error = f"{et.__name__}: {ev}"[:1500]
        return et is not None and (et is Skip or not issubclass(et, (KeyboardInterrupt, SystemExit)))

    def touch(self, what: str, n: int = 1):
        """A step a real person would have had to do by hand."""
        self.s.human_touches += n
        self.s.touches.append(f"touch: {what}")

    def note(self, text: str):
        self.s.notes.append(text)

    def tokens(self, **kw):
        for k, v in kw.items():
            if isinstance(v, (int, float)):
                self.s.tokens[k] = self.s.tokens.get(k, 0) + v

    def fail(self, why: str):
        raise StepFailed(why)

    def skip(self, why: str):
        raise Skip(why)


class StepFailed(RuntimeError):
    pass


class Skip(Exception):
    pass
