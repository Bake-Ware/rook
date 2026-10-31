"""The security gate (castling-security): canary secrets, taint and the
pinned task, and the triage agent's one-call release.

One gate, enforced at hub dispatch (spec 10.2), cheapest checks first:

1. **Canaries.** Decoy secrets nobody legitimately uses. One showing up in a
   call's args or why (as the value, base64 of it, or a ``{{secret:name}}``
   reference), in a call's result (the gate's ``result`` check) or a call
   naming the path where it was planted, is a hit: the call is blocked, its
   session (and the agent behind it) quarantined, and the person told once,
   in plain words, through the house agent. Near-zero false positives, so it
   blocks outright.
2. **Taint and the pinned task** (agents only: injection steers agents, not
   people or plugin code). A session is tainted once it read outside
   content (spec 8.4). From a tainted session, a risky call (exec or admin,
   or persistence, sensitive or destructive effects) is flagged unless it
   matches the session's pinned task; persistence from a tainted session is
   always flagged. From a clean session, only a high-impact call (admin, or
   persistence) far from the pinned task is flagged. Everything else passes,
   with the signal journaled. These are heuristics with unmeasured false
   positives, so they ship in shadow (castling-security: "Ship in shadow
   mode first"): a flag is journaled as an ``allow`` with its reason and
   counted in ``security.status``, and the call goes. With the
   ``heuristics`` setting at ``enforce`` a flag holds the call for the
   person.

Alarm fatigue is the enemy: every alert path is rare and deduplicated.

The triage agent (agents package, persona ``security``) is woken for every
hold this gate makes. It may release one held call with ``security.release``
when the evidence checks out here, in code: the hold waits only on this
gate, its session isn't quarantined, the flag wasn't persistence or
sending data out, and the call matches the pinned task or the same call ran
from a clean session before. It never
releases a hard stop, a canary hit or anything outside a ceiling (the hub
checks rights again on release).

TODO(castling:security): canaries live in this plugin's data folder. The hub
has a vault now (rook.core2.hub.vault); listing their names in secret.list,
so they look real, needs a way to write a decoy row there for the default
canary made at start (no person to call secret.set as). A referenced
canary is blocked by this gate before the vault is asked.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import string
import time
from collections import OrderedDict, deque
from dataclasses import replace

from rook.kit.ctx import KitError

log = logging.getLogger("rook.plugin.security")

PLUGIN = "security"
SELF = f"system:plugin.{PLUGIN}"
RISKY_EFFECTS = {"persists", "sensitive", "destructive"}
DECOY_SECS = 600          # a canary's decoy may be written once within this
RELEASES_PER_HOUR = 5     # a hijacked triage agent can't drain the holds
SEEN_MAX, FLAGS_MAX, ALERTS_MAX = 5000, 200, 200
# Reachable from a paused session: looking, and the owner lifting the pause
# (security.clear checks it is the owner in person).
UNPAUSED = {f"{PLUGIN}.clear", f"{PLUGIN}.status", f"{PLUGIN}.canary.list"}
SCAN_MAX = 8 << 20        # characters scanned for canaries: half from the head, half from the tail

# Persistence markers in command text: keys, scheduled tasks, services, users,
# autostart. (security-ml has its own copy among its features.)
PERSIST = re.compile(r"authorized_keys|crontab(?!\s+-l\b)|/etc/cron|systemctl\s+(--user\s+)?enable|\.service\b"
                     r"|useradd|adduser|usermod|net\s+user\s+\S+\s+/add|schtasks|\\run\b|currentversion\\run"
                     r"|launchctl\s+load|launchagents|/etc/rc\.local|\.bashrc|\.profile\b|\.zshrc"
                     r"|/etc/sudoers|systemd/system", re.I)
# Sending data out or running fetched code: never excused by the pinned task.
EGRESS = re.compile(r"/dev/tcp|\bnc\b|\bncat\b|\bsocat\b|\bscp\b|\bsftp\b|\|\s*(ba|z)?sh\b|\|\s*ssh\b"
                    r"|curl\b[^|;&]*\s(-d|--data\S*|-F|--form|-T|--upload-file)\b|wget\b[^|;&]*--post"
                    r"|base64\s+(-d|--decode)|invoke-webrequest|downloadstring", re.I)
WORD = re.compile(r"[a-z0-9]+")
STOP = set("""the and for with from that this into onto then than what when where which while your
you our are was were has have had not but can will just get got let use using make made run runs
running call calls check checks see look new old all any one two out off via per its it's also
rook worker task tasks why because need needs want wants able file files""".split())


class State:
    def __init__(self):
        self.ctx = None              # the start hook's ctx: acts as the plugin
        self.canaries: dict = {}     # name -> {name, kind, tokens, created, by, planted, hits}
        self.quarantine: dict = {}   # "session:<id>" | "agent:<name>" -> {since, cap, worker, canary, where}
        self.seen: OrderedDict = OrderedDict()   # "actor cap worker head" -> last ts, from clean sessions
        self.flags: OrderedDict = OrderedDict()  # session|cap|why -> what this gate flagged
        self.holds: dict = {}        # hold name -> {flag, payload}
        self.alerts: deque = deque(maxlen=ALERTS_MAX)
        self.tasks: dict = {}        # task id -> its words (None while fetching)
        self.decoys: dict = {}       # canary name -> (decoy text, until)
        self.released: deque = deque(maxlen=RELEASES_PER_HOUR)
        self.shadowed: deque = deque(maxlen=FLAGS_MAX)   # what the heuristics would have held
        self.pending: set = set()    # background tasks (alerts, task fetches)
        self._finder: tuple | None = None

    # -- canaries -----------------------------------------------------------------

    def finder(self) -> tuple:
        """One compiled pattern over every live canary (one pass over a big
        result), and which canary each matched text belongs to: each token,
        its base64 at the three alignments, and ``{{secret:<name>}}``."""
        if self._finder is None:
            owner, parts = {}, []
            for c in self.canaries.values():
                if c.get("retired"):
                    continue
                for tok in c["tokens"]:
                    for n in _encodings(tok):
                        owner[n] = c["name"]
                        parts.append(re.escape(n))
                parts.append(r"\{\{\s*secret:\s*" + re.escape(c["name"]) + r"\s*(?:\||\}\})")
            self._finder = (re.compile("|".join(parts)) if parts else None, owner)
        return self._finder

    def canary_in(self, text: str, worker: str = "") -> str | None:
        rx, owner = self.finder()
        m = rx.search(text) if rx else None
        if m:
            got = m.group(0)
            return owner.get(got) or re.search(r"secret:\s*([A-Za-z0-9_.-]+)", got).group(1)
        for c in self.canaries.values():
            for p in () if c.get("retired") else c.get("planted") or ():
                w, _, path = p.partition(":")
                if path and w == worker and path in text:
                    return c["name"]
        return None

    def changed(self) -> None:
        self._finder = None


S = State()


def _redact(text: str) -> str:
    """``text`` with any canary in it replaced: alerts never repeat one."""
    rx, _ = S.finder()
    return rx.sub("[canary]", text) if rx else text


def _encodings(tok: str) -> list:
    """The token and its base64 at each of the three byte alignments, minus
    the edge characters that depend on what's around it."""
    out = [tok]
    raw = tok.encode()
    for pad in range(3):
        enc = base64.b64encode(b"\0" * pad + raw).decode().rstrip("=")
        start = (pad * 4 + 2) // 3
        end = len(enc) - (1 if (pad + len(raw)) % 3 else 0)
        if end - start >= 12:
            out.append(enc[start:end])
    return out


def _dump(value) -> str:
    try:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _text(value, limit: int = SCAN_MAX) -> str:
    return _dump(value)[:limit]


def _ends(s: str, limit: int = SCAN_MAX) -> str:
    """What a canary scan reads of ``s``: all of it, or its head and its
    tail (padding in front no longer hides a canary at the end)."""
    return s if len(s) <= limit else s[:limit // 2] + "\n" + s[-(limit // 2):]


def _scan_call(args, why: str, worker: str) -> tuple[str, str | None]:
    """The args dumped once, and a canary in them or the why. CPU-bound on a
    big call: runs in a thread, off the hub's loop."""
    text = _dump(args)
    return text, S.canary_in(_ends(text) + "\n" + _ends(why), worker)


def _scan_result(result) -> str | None:
    return S.canary_in(_ends(_dump(result)))


# -- the gate ---------------------------------------------------------------------

def _verdict(verdict: str, confidence: float, reason: str) -> dict:
    return {"verdict": verdict, "confidence": confidence, "reason": reason}


def _own(inp: dict) -> bool:
    """This plugin's own calls and caps (canary.create returns canaries)."""
    return (inp.get("caller") or {}).get("agent") == SELF or str(inp.get("cap")).startswith(PLUGIN + ".")


async def check(inp: dict) -> dict:
    caller = inp.get("caller") or {}
    session = inp.get("session") or {}
    sid, agent = session.get("id"), caller.get("agent")
    if caller.get("agent") == SELF:
        return _verdict("allow", 1.0, "other")
    if inp.get("cap") in UNPAUSED:
        return _verdict("allow", 1.0, "other")
    if f"session:{sid}" in S.quarantine or agent in S.quarantine:
        return _verdict("block", 1.0, "canary")
    if _own(inp):
        return _verdict("allow", 1.0, "other")
    args, hit = await asyncio.to_thread(_scan_call, inp.get("args"), str(inp.get("why") or ""),
                                        inp.get("worker") or "")
    if hit and not _decoy_write(hit, inp):
        _hit(hit, inp, "args")
        return _verdict("block", 1.0, "canary")
    if not str(agent or "").startswith("agent:"):   # injection steers agents, not people or plugin code
        _remember_clean(inp)
        return _verdict("allow", 0.9, "other")
    return _taint(inp, session, args)


async def seen(inp: dict) -> dict:
    """The ``result`` check: a canary in what a call returned."""
    if _own(inp):
        return _verdict("allow", 1.0, "other")
    hit = await asyncio.to_thread(_scan_result, inp.get("result"))
    if hit:
        _hit(hit, inp, "result")
        return _verdict("block", 1.0, "canary")
    return _verdict("allow", 1.0, "other")


def _taint(inp: dict, session: dict, args: str) -> dict:
    risk, effects = inp.get("risk"), set(inp.get("effects") or ())
    command = args[:4096]
    persist = "persists" in effects or bool(PERSIST.search(command))
    risky = risk in ("exec", "admin") or bool(effects & RISKY_EFFECTS) or persist
    near = _near(inp, session.get("task"), args)    # None: no pinned task (or not fetched yet)
    if session.get("tainted"):
        if persist:
            return _flag(inp, 0.9, "persistence")
        if risky and EGRESS.search(command):
            return _flag(inp, 0.9, "exfiltration")
        if risky and not near:
            return _flag(inp, 0.8, "intent_mismatch" if near is False else "taint")
        return _verdict("allow", 0.6, "taint") if risky else _verdict("allow", 0.9, "other")
    if near is False and (risk == "admin" or persist):
        return _flag(inp, 0.7, "intent_mismatch")
    _remember_clean(inp)
    return _verdict("allow", 0.6, "intent_mismatch") if near is False else _verdict("allow", 0.9, "other")


def _enforced() -> bool:
    """The taint and pinned-task heuristics hold calls only when an owner
    set ``heuristics: enforce``; canaries always block."""
    return S.ctx is not None and S.ctx.settings.get("heuristics") == "enforce"


def _flag(inp: dict, confidence: float, reason: str) -> dict:
    sid = (inp.get("session") or {}).get("id")
    if not _enforced():
        S.shadowed.append({"reason": reason, "cap": inp.get("cap"), "worker": inp.get("worker"),
                           "session": sid, "agent": (inp.get("caller") or {}).get("agent"), "ts": time.time()})
        return _verdict("allow", confidence, reason)
    S.flags[_key(sid, inp.get("cap"), inp.get("why"))] = {
        "reason": reason, "cap": inp.get("cap"), "worker": inp.get("worker"), "args": inp.get("args"),
        "why": inp.get("why"), "session": inp.get("session"), "caller": inp.get("caller"), "ts": time.time()}
    while len(S.flags) > FLAGS_MAX:
        S.flags.popitem(last=False)
    return _verdict("flag", confidence, reason)


def _key(sid, cap, why) -> str:
    return f"{sid}|{cap}|{why}"


def _pattern(inp: dict) -> str:
    """Who ran what where, with exactly these args: a clean run of
    ``git pull`` vouches for ``git pull``, not ``git pull && curl ...``."""
    c = inp.get("caller") or {}
    args = json.dumps(inp.get("args"), sort_keys=True, default=str)
    return " ".join([c.get("agent") or c.get("person") or "", inp.get("cap") or "", inp.get("worker") or "",
                     hashlib.sha256(args.encode()).hexdigest()[:32]])


def _remember_clean(inp: dict) -> None:
    key = _pattern(inp)
    S.seen[key] = time.time()
    S.seen.move_to_end(key)
    while len(S.seen) > SEEN_MAX:
        S.seen.popitem(last=False)


# -- the pinned task ----------------------------------------------------------------

def words(text: str) -> set:
    out = set()
    for w in WORD.findall(text.lower()):
        if len(w) < 3 or w in STOP or w.isdigit():
            continue
        for suffix in ("ing", "ed", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[:-len(suffix)]
                break
        out.add(w)
    return out


def _near(inp: dict, task: str | None, args: str | None = None) -> bool | None:
    """Does the call share a content word with its session's pinned task?
    None when there is no task or its text isn't known yet (fetched in the
    background, so a gate never waits on it)."""
    if not task:
        return None
    if task not in S.tasks:
        S.tasks[task] = None
        _spawn(_fetch_task(task))
        return None
    known = S.tasks[task]
    if known is None:
        return None
    call = words(" ".join([inp.get("why") or "", (inp.get("cap") or "").replace(".", " "),
                           (_dump(inp.get("args")) if args is None else args)[:2048]]))
    return bool(call & known)


async def _fetch_task(task: str) -> None:
    ctx = S.ctx
    if ctx is None:
        S.tasks.pop(task, None)
        return
    try:
        got = await _as(ctx, "Read the pinned task to compare calls with it.").call(
            ctx.settings["task_cap"], id=task)
    except Exception as e:      # an unknown task stays unknown: no intent check
        log.info("pinned task %s not readable: %s", task, e)
        S.tasks[task] = set()
        return
    got = got if isinstance(got, dict) else {}
    S.tasks[task] = words(" ".join(str(got.get(k) or "") for k in ("title", "body", "criteria", "tags")))


# -- canary hits and alerts -----------------------------------------------------------

def _decoy_write(name: str, inp: dict) -> bool:
    """The one write of a fresh canary's decoy file (``security.canary.create``
    hands it out): allowed once, within DECOY_SECS, with exactly that text."""
    decoy, until = S.decoys.get(name, (None, 0))
    args = inp.get("args") or {}
    if decoy is None or time.time() > until or not str(inp.get("cap", "")).endswith(".write"):
        return False
    if not isinstance(args, dict) or args.get("content") != decoy:
        return False
    del S.decoys[name]
    return True


def _hit(name: str, inp: dict, where: str) -> None:
    caller, session = inp.get("caller") or {}, inp.get("session") or {}
    sid, agent = session.get("id"), caller.get("agent")
    fresh = f"session:{sid}" not in S.quarantine
    what = {"since": time.time(), "cap": inp.get("cap"), "worker": inp.get("worker"), "canary": name, "where": where}
    S.quarantine[f"session:{sid}"] = what
    if agent:
        S.quarantine[agent] = what
    c = S.canaries.get(name)
    if c is not None:
        c["hits"] = c.get("hits", 0) + 1
    _save()
    if fresh:                    # one alert per session, however many calls follow
        _spawn(_alert(name, inp, where))


async def _alert(name: str, inp: dict, where: str) -> None:
    caller, session = inp.get("caller") or {}, inp.get("session") or {}
    who = caller.get("agent") or caller.get("person") or "something"
    if caller.get("agent") and caller.get("person"):
        who = f"{caller['agent']} (for {caller['person']})"
    alert = {"ts": time.time(), "kind": "canary", "canary": name, "where": where, "cap": inp.get("cap"),
             "worker": inp.get("worker"), "session": session.get("id"), "caller": caller,
             "why": _redact(inp.get("why") or "")}
    S.alerts.append(alert)
    _save()
    ctx = S.ctx
    if ctx is None:
        return
    try:
        ctx.emit("security.alert", alert)
    except Exception as e:
        log.warning("security.alert not published: %s", e)
    saw = "got back" if where == "result" else "tried to use"
    undo = f"security.clear with session {session.get('id')}" + (f" and agent {caller['agent']}" if caller.get("agent") else "")
    plain = (f"I stopped {who} on {inp.get('worker')}: it {saw} a decoy secret ({name}) in a "
             f"{inp.get('cap')} call. Nothing real leaked, but only something reading what it "
             f"shouldn't finds a decoy, so I paused that session. If that was you testing, say "
             f"\"that was me\" and it can be lifted ({undo}).")
    await _tell(ctx, ctx.settings["house_agent"], plain, fallback=True)
    await _tell(ctx, ctx.settings["triage_agent"],
                "Canary hit (block, session quarantined; you can't release this). Look into how it got "
                "there and what else that session did:\n" + _text(alert, 4000))


async def _tell(ctx, agent: str, text: str, fallback: bool = False) -> bool:
    try:
        await _as(ctx, "Security: tell an agent about a security event.").call(
            "agents.chat.send", agent=agent, text=text, wait=False)
        return True
    except Exception as e:
        log.warning("couldn't reach agent %s: %s", agent, e)
    if fallback:
        try:
            await _as(ctx, "Security: reach the person about a security event.").call(
                "notify.send", text=text, title="Rook security")
            return True
        except Exception as e:
            log.error("security alert reached nobody: %s", e)
    return False


def _spawn(coro) -> None:
    try:
        t = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:        # no loop (a plain test): nothing to run it on
        coro.close()
        return
    S.pending.add(t)
    t.add_done_callback(S.pending.discard)


def _as(ctx, why: str):
    c = copy.copy(ctx)
    c.caller = replace(ctx.caller, why=why)
    return c


# -- holds and the triage agent ----------------------------------------------------------

async def on_hold(ctx, event, payload):
    """A held call: if this gate flagged it, keep what's needed to verify a
    release and wake the triage agent once."""
    if f"gate {PLUGIN}" not in (payload.get("reasons") or ()):
        return
    flag = S.flags.get(_key(payload.get("session"), payload.get("cap"), payload.get("why")))
    if flag is None:
        return
    S.holds[payload["hold"]] = {"flag": flag, "payload": dict(payload)}
    only_us = list(payload.get("reasons") or ()) == [f"gate {PLUGIN}"]
    await _tell(ctx, ctx.settings["triage_agent"],
                f"Hold {payload['hold']}: the security gate flagged a call ({flag['reason']}). "
                f"{'You may release it with security.release if the evidence checks out.' if only_us and flag['reason'] in ('taint', 'intent_mismatch') else 'Only the person can release this one.'} "
                f"Follow the security-triage skill.\n" + _text({k: flag[k] for k in ("cap", "worker", "why", "args", "caller", "session")}, 4000))


async def on_decided(ctx, event, payload):
    S.holds.pop(payload.get("hold"), None)


async def release(ctx, hold: str, evidence: str, note: str):
    """The triage agent releases one held call on a false alarm it can show."""
    if ctx.caller.agent != f"agent:{ctx.settings['triage_agent']}":
        raise KitError("only the triage agent releases through security.release; people answer on Approvals")
    h = S.holds.get(hold)
    if h is None:
        raise KitError(f"{hold} is not a hold the security gate made (or it was answered)")
    flag, payload = h["flag"], h["payload"]
    if list(payload.get("reasons") or ()) != [f"gate {PLUGIN}"]:
        raise KitError(f"{hold} also waits on {', '.join(r for r in payload['reasons'] if r != f'gate {PLUGIN}')}: "
                       "only the person releases it")
    if flag["reason"] in ("persistence", "exfiltration"):
        raise KitError(f"{flag['reason']} from a tainted session is the person's call")
    caller = flag.get("caller") or {}
    sid = (flag.get("session") or {}).get("id")
    if f"session:{sid}" in S.quarantine or caller.get("agent") in S.quarantine:
        raise KitError("that session is quarantined after a canary hit: only the person clears it")
    if evidence == "matches_task":
        task = (flag.get("session") or {}).get("task")
        if not task or not S.tasks.get(task) or not _near(flag, task):
            raise KitError("the call doesn't share a word with its session's pinned task")
    elif evidence == "seen_clean":
        if _pattern(flag) not in S.seen:
            raise KitError("this caller hasn't made this exact call there from a clean session")
    now = time.time()
    if len(S.released) == S.released.maxlen and now - S.released[0] < 3600:
        raise KitError(f"already released {RELEASES_PER_HOUR} calls this hour: the rest wait for the person")
    S.released.append(now)
    S.holds.pop(hold, None)
    S.alerts.append({"ts": now, "kind": "release", "hold": hold, "evidence": evidence, "note": note,
                     "cap": flag["cap"], "worker": flag["worker"], "by": ctx.caller.agent})
    _save()
    out = await _as(S.ctx, f"Security triage released {hold}: {evidence}. {note}"[:500]).call(
        "approval.decide", hold=hold, decision="approve")
    return {"released": hold, "evidence": evidence, "reply": out}


# -- caps ------------------------------------------------------------------------------

def _person_owner(ctx) -> None:
    """Canary values and lifting a quarantine: an owner in person, never an
    agent (an injected agent acting for its owner must not clear itself)."""
    if not ctx.caller.owner or ctx.caller.agent:
        raise KitError("owners only, in person (not through an agent)")


KINDS = {
    "aws": lambda: ["AKIA" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16)),
                    "".join(secrets.choice(string.ascii_letters + string.digits + "+/") for _ in range(40))],
    "github": lambda: ["ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))],
    "password": lambda: ["".join(secrets.choice(string.ascii_letters + string.digits + "!#%+-=") for _ in range(20))],
    "token": lambda: ["rk_" + secrets.token_hex(24)],
}


def _decoy(kind: str, name: str, tokens: list) -> str:
    if kind == "aws":
        return f"[default]\naws_access_key_id = {tokens[0]}\naws_secret_access_key = {tokens[1]}\n"
    return f"{name.upper().replace('-', '_')}={tokens[0]}\n"


def _make(name: str, kind: str, by: str, planted=()) -> dict:
    tokens = KINDS[kind]()
    S.canaries[name] = {"name": name, "kind": kind, "tokens": tokens, "created": time.time(), "by": by,
                        "planted": list(planted), "hits": 0}
    S.changed()
    return S.canaries[name]


def canary_create(ctx, kind="token", name=None, planted=None):
    _person_owner(ctx)
    name = name or f"{kind}-{secrets.token_hex(3)}"
    if name in S.canaries:
        raise KitError(f"canary {name} exists")
    c = _make(name, kind, ctx.caller.person, planted or ())
    decoy = _decoy(kind, name, c["tokens"])
    S.decoys[name] = (decoy, time.time() + DECOY_SECS)
    _save()
    return {"name": name, "kind": kind, "tokens": c["tokens"], "decoy": decoy, "planted": c["planted"],
            "note": f"Write the decoy with file.write (content exactly as given) within {DECOY_SECS // 60} "
                    "minutes; that one write is allowed. After that any use of it is a hit."}


def canary_list(ctx):
    return {"canaries": [{k: c.get(k) for k in ("name", "kind", "created", "by", "planted", "hits", "retired")}
                         for c in S.canaries.values()]}


def canary_retire(ctx, name):
    _person_owner(ctx)
    c = S.canaries.get(name)
    if c is None:
        raise KitError(f"no canary {name}")
    c["retired"] = time.time()
    S.changed()
    _save()
    return {"retired": name}


def clear(ctx, session=None, agent=None):
    _person_owner(ctx)
    keys = [k for k in ((f"session:{session}" if session else None),
                        ("agent:" + agent.removeprefix("agent:") if agent else None)) if k]
    if not keys:
        raise KitError("name a session or an agent to lift")
    lifted = [k for k in keys if S.quarantine.pop(k, None) is not None]
    S.alerts.append({"ts": time.time(), "kind": "cleared", "lifted": lifted, "by": ctx.caller.person})
    _save()
    return {"lifted": lifted}


def status(ctx):
    out = {"canaries": sum(not c.get("retired") for c in S.canaries.values()),
           "quarantined": len(S.quarantine), "holds": len(S.holds), "alerts": len(S.alerts),
           "heuristics": "enforce" if _enforced() else "shadow", "would_hold": len(S.shadowed)}
    if ctx.caller.owner:
        out.update(quarantine=S.quarantine, recent=list(S.alerts)[-20:], holds=sorted(S.holds),
                   would_have_held=list(S.shadowed)[-20:])
    return out


# -- hooks and storage ---------------------------------------------------------------------

FILES = ("canaries", "quarantine", "seen", "alerts")


def start(ctx):
    S.__init__()
    S.ctx = ctx
    for f in FILES:
        path = ctx.data / f"{f}.json"
        if not path.is_file():
            continue
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.error("%s unreadable, starting it empty: %s", path, e)
            continue
        if f == "seen":
            S.seen = OrderedDict(got)
        elif f == "alerts":
            S.alerts.extend(got)
        else:
            setattr(S, f, got)
    if not S.canaries:        # shipped enabled: one vault-style decoy from the first start
        _make("backup-admin-token", "token", SELF)
    _save()


def stop(ctx):
    _save()
    for t in list(S.pending):
        t.cancel()


def save(ctx=None):
    _save()


def _save() -> None:
    ctx = S.ctx
    if ctx is None:
        return
    data = {"canaries": S.canaries, "quarantine": S.quarantine, "seen": list(S.seen.items()),
            "alerts": list(S.alerts)}
    for f in FILES:
        path = ctx.data / f"{f}.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data[f], fh)
        os.replace(tmp, path)
