"""The gate hook at hub dispatch (spec 10.2 of docs/spec/core-v2.md).

A gate is a plugin's ``gate:`` section::

    gate:
      run: scripts/gate.py:check    # input dict -> {verdict, confidence, reason}
      match: { risk: [write, exec, admin], effects: [...], caps: [...] }
      budget: 20ms
      mode: shadow                  # shadow | enforce; ships shadow first
      result: scripts/gate.py:seen  # optional: input + "result" -> the same verdict

Gates run in parallel for the calls their ``match`` selects. They can only
restrict: the hub keeps ``verdict``, ``confidence`` and ``reason`` and
ignores everything else. A gate that errors, overruns its budget or returns
something malformed counts as ``flag`` with reason ``gate_error``. Shadow
gates are journaled and never count (rights.gate_decision).

A gate's optional ``result`` check sees a call's result after it ran (the
input plus ``result``). It can only withhold the result: a ``block`` turns
the reply into a refusal. A flag there can't hold a call that already ran, so
it is journaled like a shadow verdict.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from rook.kit._types import parse_duration

from .. import rights

DEFAULT_BUDGET = 0.020
MAX_WAIT = 0.200          # the hub never waits longer than this for any gate
MODES = ("shadow", "enforce")

Check = Callable[[dict], "dict | Awaitable[dict]"]


@dataclass
class Gate:
    name: str
    check: Check
    match: dict = field(default_factory=dict)
    budget: float = DEFAULT_BUDGET
    mode: str = "shadow"
    result: Check | None = None

    @classmethod
    def from_manifest(cls, plugin: str, section: dict, check: Check,
                      result: Check | None = None) -> "Gate":
        """``section`` is the manifest's ``gate:``; ``check`` is its resolved
        ``run`` target. TODO(castling:wiring): resolve ``run`` with
        rook.kit.ctx.load_run and decide whether gates get a ctx."""
        mode = section.get("mode", "shadow")
        if mode not in MODES:
            raise ValueError(f"gate {plugin}: unknown mode {mode!r}")
        budget = parse_duration(section["budget"]) if "budget" in section else DEFAULT_BUDGET
        return cls(plugin, check, dict(section.get("match") or {}), budget, mode, result)

    def selects(self, call: dict) -> bool:
        """Every key given in ``match`` must match: ``risk`` lists the call's
        risk, ``effects`` shares one with the call, a ``caps`` glob matches."""
        m = self.match
        if "risk" in m and call["risk"] not in m["risk"]:
            return False
        if "effects" in m and not set(m["effects"]) & set(call["effects"]):
            return False
        return "caps" not in m or any(rights.matches(g, call["cap"]) for g in m["caps"])


def clean(result: Any) -> dict | None:
    """The restriction in a gate's result, or None when it is malformed."""
    if not isinstance(result, dict):
        return None
    verdict, conf, reason = result.get("verdict"), result.get("confidence"), result.get("reason")
    if verdict not in rights.VERDICTS or reason not in rights.GATE_REASONS:
        return None
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        return None
    return {"verdict": verdict, "confidence": conf, "reason": reason}


def _in_thread(check: Check, inp: dict) -> asyncio.Future:
    """Run a sync gate on a daemon thread: one that hangs is abandoned
    without holding an executor slot or the event loop's shutdown."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def settle(ok: bool, value: Any) -> None:
        if not fut.done():
            fut.set_result(value) if ok else fut.set_exception(value)

    def work() -> None:
        try:
            res = (True, check(inp))
        except Exception as e:      # reported as gate_error
            res = (False, e)
        try:
            loop.call_soon_threadsafe(settle, *res)
        except RuntimeError:        # the loop is gone; nobody is waiting
            pass

    threading.Thread(target=work, name="rook-gate", daemon=True).start()
    return fut


async def _one(gate: Gate, inp: dict, check: Check | None = None) -> dict:
    out = {"gate": gate.name, "mode": gate.mode}
    check = check or gate.check
    try:
        if inspect.iscoroutinefunction(check):
            pending = check(inp)
        else:
            pending = _in_thread(check, inp)
        res = clean(await asyncio.wait_for(pending, min(gate.budget, MAX_WAIT)))
    except Exception:  # an error or a timeout: the gate failed, the call is flagged
        res = None
    return {**out, **(res or {"verdict": "flag", "confidence": 1.0, "reason": "gate_error"})}


async def run(gates: list[Gate], inp: dict) -> list[dict]:
    """Run the gates that select this call, in parallel; one entry per gate,
    in gate order: {gate, mode, verdict, confidence, reason}. Each gate gets
    its own copy of the input, so none can change the call."""
    call = {"cap": inp["cap"], "risk": inp["risk"], "effects": inp["effects"]}
    chosen = [g for g in gates if g.selects(call)]
    return list(await asyncio.gather(*(_one(g, copy.deepcopy(inp)) for g in chosen)))


async def run_result(gates: list[Gate], inp: dict, result: Any) -> list[dict]:
    """Run the ``result`` checks of the gates that select this call on what
    it returned; entries as :func:`run`, with ``stage: result``."""
    call = {"cap": inp["cap"], "risk": inp["risk"], "effects": inp["effects"]}
    chosen = [g for g in gates if g.result is not None and g.selects(call)]
    if not chosen:
        return []
    out = await asyncio.gather(*(_one(g, copy.deepcopy({**inp, "result": result}), g.result) for g in chosen))
    return [{**r, "stage": "result"} for r in out]
