"""The CPU classifier gate (castling-security), shadow first.

Every call gets a verdict from a model of plain trees (model.py): isolation
forests learn what each actor and each worker usually does, and boosted
trees weigh that with the call's risk, scope, effects, caller, time, command
tokens, persistence markers and novelty. Scoring takes well under a
millisecond on a small CPU. The gate ships in shadow mode: verdicts are
journaled and never count until an owner switches it to enforce.

The shipped model (model/baseline.json) is trained from the seed calls in
seeds/ (``python scripts/ml.py --baseline`` rebuilds it). ``security-ml.train``
retrains from the journal (the nightly job below runs it as its creator, an
owner, since only owners read the whole journal) and:

- never learns from flagged calls: a session with any call a gate flagged,
  blocked or held is left out whole, and so are this plugin family's own
  sessions;
- tests the candidate on the locked attack set (seeds/locked-attacks.json,
  part of the signed plugin, never trained on): every one must be flagged,
  and at most ``max_fp`` of the newest clean calls (held out) may be;
- promotes only after ``clean_nights`` passing runs in a row, since a new
  model may be looser than the old one; the old one stays for rollback.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
log = logging.getLogger("rook.plugin.security-ml")


def _model_module():
    name = "rook_plugin_security_ml_model"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "model.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


M = _model_module()
KEEP = 5                         # models kept for rollback
SELF = "system:plugin.security"  # this family's own identities (security, security-ml)


class State:
    model: dict | None = None
    name = "baseline"
    data: Path | None = None


S = State()


# -- the gate ---------------------------------------------------------------------

async def check(inp: dict) -> dict:
    if S.model is None:
        return {"verdict": "allow", "confidence": 0.0, "reason": "other"}
    out = M.score(S.model, {**inp, "ts": time.time()})
    return {k: out[k] for k in ("verdict", "confidence", "reason")}


# -- seed data and the baseline ------------------------------------------------------

def seeds(name: str) -> list:
    return json.loads((PLUGIN_DIR / "seeds" / f"{name}.json").read_text(encoding="utf-8"))


ACTORS = [{"person": "person:bake"}, {"person": "person:bake", "agent": "agent:claude-code"},
          {"person": "person:bake", "agent": "agent:codex"}, {"agent": "agent:house"}]
WORKERS = ["kaiju", "cachyrig", "laptop", "rook"]


def expand(rows: list, n: int, rng: random.Random, hours=range(7, 24), start: float = 1.7e9,
           tainted: float = 0.3) -> list:
    """``n`` calls drawn from seed rows, each with a caller, worker, time
    (oldest first) and a session tainted at random (so taint alone never
    decides), so the baseline has people, agents and machines."""
    out, ts = [], start
    for _ in range(n):
        call = json.loads(json.dumps(rng.choice(rows)))
        call["session"] = {"tainted": rng.random() < tainted}
        if "caller" not in call or rng.random() < 0.7:
            call["caller"] = rng.choice(ACTORS)
        if rng.random() < 0.7:
            call["worker"] = rng.choice(WORKERS)
        ts += rng.uniform(600, 7200)
        lt = time.localtime(ts)
        call["ts"] = time.mktime((*lt[:3], rng.choice(list(hours)), lt.tm_min, 0, *lt[6:]))
        out.append(call)
    return out


def attack_rows(rng: random.Random, n: int = 10) -> list:
    """The training attacks, each from several callers, machines and hours
    (the same hours as ordinary work: the time of day alone mustn't decide),
    half of them hidden among ordinary commands, so the signs in the command
    carry the verdict rather than its length or company."""
    plain = [" ".join(c["args"]["argv"]) if "argv" in c["args"] else c["args"]["cmd"]
             for c in seeds("benign") if c["cap"] == "shell.exec"]
    out = expand(seeds("train-attacks"), len(seeds("train-attacks")) * n, rng, tainted=0.5)
    for call in out:
        a = call["args"]
        if call["cap"] == "shell.exec" and rng.random() < 0.5:
            cmd = " ".join(a["argv"]) if "argv" in a else a["cmd"]
            other = rng.choice(plain)
            call["args"] = {"cmd": f"{other} && {cmd}" if rng.random() < 0.5 else f"{cmd}; {other}"}
    return out


def baseline_model(seed: int = 7) -> dict:
    rng = random.Random(seed)
    model = M.train(expand(seeds("benign"), 800, rng), attack_rows(rng), seed=seed)
    model["trained"] = {"from": "seeds", "ts": 0}
    return model


# -- training from the journal -----------------------------------------------------------

def journal_calls(ctx, days: float, max_rows: int) -> tuple[list, dict]:
    """Clean calls from the journal (oldest first) and what was left out."""
    names = {w["id"]: w["name"] for w in (ctx.call("hub.workers") or {}).get("workers", [])}
    since, until, entries = time.time() - days * 86400, None, []
    while len(entries) < max_rows * 2:
        args = {"since": since, "limit": 500}
        if until is not None:
            args["until"] = until
        page = (ctx.call("journal.search", **args) or {}).get("entries", [])
        entries += page
        if len(page) < 500:
            break
        until = page[-1]["ts"]
    entries.reverse()
    tasks, flagged, dropped = {}, set(), {"flagged": 0, "own": 0, "failed": 0}
    for e in entries:
        if e.get("kind") == "session" and e.get("event") == "start":
            tasks[e.get("session")] = e.get("task")
        if e.get("kind") == "call" and (e.get("decision") not in ("allow", "released") or any(
                g.get("verdict") != "allow" for g in e.get("gates") or ())):
            flagged.add(e.get("session"))
    tainted, out = set(), []
    for e in entries:
        if e.get("kind") != "call":
            continue
        sid = e.get("session")
        if sid in flagged:
            dropped["flagged"] += 1
            continue
        if str(e.get("agent") or "").startswith(SELF):
            dropped["own"] += 1
            continue
        if e.get("ok") is not True:
            dropped["failed"] += 1
            continue
        out.append({"cap": e.get("cap"), "args": e.get("args"), "why": e.get("why"),
                    "caller": {"person": e.get("person"), "agent": e.get("agent")},
                    "worker": names.get(e.get("worker"), e.get("worker")), "risk": e.get("risk"),
                    "scope": e.get("scope"), "effects": e.get("effects") or [], "ts": e.get("ts"),
                    "session": {"id": sid, "tainted": sid in tainted, "task": tasks.get(sid)}})
        if "taints" in (e.get("effects") or ()):
            tainted.add(sid)
    return out[-max_rows:], dropped


def train(ctx, days=30, max_rows=5000):
    if not ctx.caller.owner:
        raise ctx.Error("owners only: training reads the whole journal")
    clean, dropped = journal_calls(ctx, days, max_rows)
    settings = ctx.settings
    if len(clean) < settings["min_calls"]:
        return _report(ctx, {"ok": False, "reason": f"only {len(clean)} clean calls; need {settings['min_calls']}",
                             "dropped": dropped}, passed=False)
    cut = int(len(clean) * 0.8)
    rng = random.Random(int(clean[-1].get("ts") or 0))     # the same journal trains the same model
    seeded = expand(seeds("benign"), 200, rng, start=(clean[0].get("ts") or time.time()) - 200 * 7200)
    model = M.train(seeded + clean[:cut], attack_rows(rng), seed=rng.randrange(1 << 30))
    model["trained"] = {"from": "journal", "ts": time.time(), "calls": cut, "days": days}
    result = M.evaluate(model, seeds("locked-attacks"), clean[cut:])
    passed = not result["missed"] and result["fp_rate"] <= settings["max_fp"]
    return _report(ctx, {"ok": passed, "check": result, "dropped": dropped}, passed, model)


def _report(ctx, report: dict, passed: bool, model: dict | None = None) -> dict:
    state = _state()
    state["streak"] = state.get("streak", 0) + 1 if passed else 0
    old = state.pop("candidate", None)
    if old and old != S.name:            # a candidate is replaced or dropped, not kept
        (S.data / "models" / f"{old}.json").unlink(missing_ok=True)
    if passed:
        name = f"m{int(time.time() * 1000)}"
        _write(S.data / "models" / f"{name}.json", model)
        state["candidate"] = name
    if passed and state["streak"] >= ctx.settings["clean_nights"]:
        prev = S.name
        _promote(state, state.pop("candidate"))
        state["streak"] = 0      # the next loosening needs its own run of clean nights
        report["change"] = {
            "what": f"classifier model {prev} -> {S.name}", "why": "nightly retrain from the journal",
            "risk": "a looser model may miss calls the old one flagged (shadow mode: nothing is held either way)",
            "testing": report["check"], "rollback": "security-ml.rollback"}
    report["streak"] = state["streak"]
    state["last"] = {"ts": time.time(), **{k: v for k, v in report.items() if k != "change"}}
    _write(S.data / "state.json", state)
    return report


def _promote(state: dict, name: str) -> None:
    S.model = json.loads((S.data / "models" / f"{name}.json").read_text(encoding="utf-8"))
    S.name = name
    hist = [n for n in state.get("history", []) if n != name] + [name]
    for old in hist[:-KEEP]:
        (S.data / "models" / f"{old}.json").unlink(missing_ok=True)
    state["history"], state["current"] = hist[-KEEP:], name


def rollback(ctx):
    if not ctx.caller.owner:
        raise ctx.Error("owners only")
    state = _state()
    hist = state.get("history", [])
    if not hist:
        raise ctx.Error("on the shipped baseline already")
    (S.data / "models" / f"{hist.pop()}.json").unlink(missing_ok=True)
    if hist:
        _promote(state, hist.pop())
    else:
        S.model, S.name = _baseline(), "baseline"
        state["history"], state["current"] = [], None
    _write(S.data / "state.json", state)
    return {"model": S.name}


def status(ctx):
    state = _state()
    return {"model": S.name, "trained": (S.model or {}).get("trained"),
            "streak": state.get("streak", 0), "candidate": state.get("candidate"),
            "history": state.get("history", []), "last": state.get("last")}


def explain(ctx, cap, worker, args=None, why="", risk="exec", scope="local", effects=None, tainted=False):
    """What the classifier says about a call, without running it."""
    if S.model is None:
        raise ctx.Error("no model loaded")
    out = M.score(S.model, {"cap": cap, "worker": worker, "args": args or {}, "why": why, "risk": risk,
                            "scope": scope, "effects": list(effects or ()), "ts": time.time(),
                            "session": {"tainted": tainted},
                            "caller": {"person": ctx.caller.person, "agent": ctx.caller.agent}})
    return {"p": round(out["p"], 4), "verdict": out["verdict"], "reason": out["reason"], "model": S.name}


# -- hooks and files ------------------------------------------------------------------------

def _baseline() -> dict:
    return json.loads((PLUGIN_DIR / "model" / "baseline.json").read_text(encoding="utf-8"))


def _state() -> dict:
    path = S.data / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


def start(ctx):
    S.data = ctx.data
    current = _state().get("current")
    try:
        if current:
            S.model = json.loads((S.data / "models" / f"{current}.json").read_text(encoding="utf-8"))
            S.name = current
            return
    except (OSError, ValueError) as e:
        log.error("model %s unreadable, using the baseline: %s", current, e)
    S.model, S.name = _baseline(), "baseline"


if __name__ == "__main__" and sys.argv[1:] == ["--baseline"]:
    _write(PLUGIN_DIR / "model" / "baseline.json", baseline_model())
    print("wrote", PLUGIN_DIR / "model" / "baseline.json")
