"""Claude Code rules: the rule format, its checks, the store and per-worker configs.

A rule is "when <trigger> happens <where>, do <actions>" (docs/design/ccmod.md):

* **trigger**: a Claude Code hook event (``PreToolUse`` with a tool matcher,
  ``UserPromptSubmit``, ``Stop``, ...), ``Idle`` (a session waiting on the
  person for N minutes, noticed by the worker) or a Rook event
  (``rook.worker_offline`` / ``rook.worker_online``, noticed by the hub);
* **scope**: the whole band set (``all``), one band, one worker, a project
  (a folder name or path) or an agent identity (``ROOK_AGENT``);
* **actions**: *remote* ones the hub runs (``post`` to a chat room or a
  huddle, ``notify`` the person, run an allow-listed ``cap``) and *local*
  ones the hook applies inside Claude Code (``inject`` context, ``block`` a
  tool call or prompt, set the ``statusline``).

Every action has a risk tier. Posting, notifying and the local actions are
``write``. A ``cap`` action has its capability's own tier (floored at
``write``); it must be on the operator's allow-list (``ccmod.cap_allowlist``)
and an ``exec``/``admin`` tier capability also needs ``ccmod.allow_exec``.
Event text never reaches a capability's arguments: they are fixed when the
rule is saved.

The store is one JSON file in the plugin's data folder: the rules (each with
its own ``rev``), the band-wide ``rev``, rooms made for rules, the last push
to each worker, an attributed history and a short activity log.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ....core import authz as core_authz
from ....core.db import atomic_write_text
from ....worker import ccmod_hook as hook

ROOK_EVENTS = ("rook.worker_offline", "rook.worker_online")
EVENTS = hook.HOOK_EVENTS + (hook.IDLE,) + ROOK_EVENTS
REMOTE = ("post", "notify", "cap")
LOCAL = ("inject", "block", "statusline")
ACTIONS = REMOTE + LOCAL
SCOPES = ("all", "band", "worker", "project", "agent")
TEXT_EVENTS = hook.TOOL_EVENTS + ("UserPromptSubmit", "Notification")
MAX_RULES = 200
MAX_ACTIONS = 8
HISTORY = 200
ACTIVITY = 300
DEFAULT_POST = "{worker}: Claude Code {event} {tool} in {project}"

#: Plain-language labels, shared with the dashboard (the page reads them from the API).
EVENT_LABELS = {
    "SessionStart": "A session starts",
    "UserPromptSubmit": "A prompt is sent",
    "PreToolUse": "Claude is about to use a tool",
    "PostToolUse": "Claude has used a tool",
    "Notification": "Claude Code shows a notification",
    "Stop": "Claude finishes its turn",
    "SubagentStop": "A subagent finishes",
    "PreCompact": "The conversation is about to be compacted",
    "SessionEnd": "A session ends",
    hook.IDLE: "A session has been waiting on you",
    "rook.worker_offline": "A worker goes offline",
    "rook.worker_online": "A worker comes back online",
}
ACTION_LABELS = {
    "post": "Post in a chat room or huddle",
    "notify": "Notify me (Telegram, Discord)",
    "cap": "Run a capability",
    "inject": "Give Claude extra instructions",
    "block": "Block it, with a message",
    "statusline": "Set the status line",
}

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CAP = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+$")


class RuleError(ValueError):
    """A rule that cannot be saved; the message says what to fix, in plain words."""


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:48]
    return s or "rule"


def _str(v: Any, room: int, what: str, required: bool = False) -> str:
    if v is None:
        v = ""
    if not isinstance(v, (str, int, float)):
        raise RuleError(f"{what} must be text")
    s = str(v).strip()
    if required and not s:
        raise RuleError(f"{what} is required")
    if len(s) > room:
        raise RuleError(f"{what} is longer than {room} characters")
    return s


def allowed(cap: str, allowlist) -> bool:
    """Whether ``cap`` is on the allow-list (exact names, or ``prefix.*``)."""
    for pat in allowlist or ():
        pat = str(pat).strip()
        if not pat:
            continue
        if pat == cap or (pat.endswith(".*") and cap.startswith(pat[:-1])):
            return True
    return False


class Policy:
    """What a rule may do: the allow-list, the exec switch and capability tiers."""

    def __init__(self, allowlist=(), allow_exec: bool = False,
                 tier_of: Callable[[str], str] | None = None) -> None:
        self.allowlist = [str(x).strip() for x in (allowlist or ()) if str(x).strip()]
        self.allow_exec = bool(allow_exec)
        self._tier_of = tier_of

    def tier(self, cap: str) -> str:
        if self._tier_of is not None:
            return self._tier_of(cap)
        return core_authz.effective_tier(cap)

    def check_cap(self, cap: str) -> str:
        """The cap's tier, or RuleError when policy refuses it."""
        if not allowed(cap, self.allowlist):
            raise RuleError(f"{cap} is not on the capability allow-list. An administrator adds it "
                            "under Settings (Claude Code: capabilities rules may run).")
        tier = self.tier(cap)
        if core_authz.tier_rank(tier) >= core_authz.tier_rank("exec") and not self.allow_exec:
            raise RuleError(f"{cap} can run code or control the machine ({tier}); an administrator "
                            "must first allow rules to run such capabilities (Settings, Claude Code).")
        return tier


def _max_tier(*tiers: str) -> str:
    return max(tiers, key=lambda t: core_authz.tier_rank(t)) if tiers else "write"


def validate(rule: Any, policy: Policy) -> dict:
    """A checked, normalised copy of ``rule`` (with its computed ``risk``)."""
    if not isinstance(rule, dict):
        raise RuleError("a rule must be an object")
    name = _str(rule.get("name"), 80, "The name", required=True)
    rid = _str(rule.get("id"), 64, "The id") or slug(name)
    if not _ID.match(rid):
        raise RuleError("The id may hold lowercase letters, digits, - and _ only")
    trig = rule.get("trigger") if isinstance(rule.get("trigger"), dict) else {}
    event = trig.get("event")
    if event not in EVENTS:
        raise RuleError("Pick what should trigger the rule")
    t: dict = {"event": event}
    if event in hook.TOOL_EVENTS:
        m = _str(trig.get("matcher"), 200, "The tool name")
        if m and m != "*":
            try:
                re.compile(m)
            except re.error:
                raise RuleError("The tool name is not a valid pattern (try Bash, or Edit|Write)") from None
            t["matcher"] = m
    if event in TEXT_EVENTS:
        c = _str(trig.get("contains"), 200, "The text to look for")
        if c:
            t["contains"] = c
    if event == hook.IDLE:
        try:
            minutes = int(trig.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if not 1 <= minutes <= 1440:
            raise RuleError("Idle needs a number of minutes between 1 and 1440")
        t["minutes"] = minutes
    sc = rule.get("scope") if isinstance(rule.get("scope"), dict) else {}
    kind = sc.get("kind") or "all"
    if kind not in SCOPES:
        raise RuleError("The scope must be everywhere, a band, a worker, a project or an agent")
    target = _str(sc.get("target"), 300, "The scope's target", required=kind != "all")
    if event in ROOK_EVENTS and kind in ("project", "agent"):
        raise RuleError("Worker online/offline rules apply to a band or a worker, not a project or agent")
    actions = rule.get("actions")
    if not isinstance(actions, list) or not actions:
        raise RuleError("Add at least one action")
    if len(actions) > MAX_ACTIONS:
        raise RuleError(f"A rule has at most {MAX_ACTIONS} actions")
    out_actions, tiers = [], []
    for i, a in enumerate(actions, 1):
        if not isinstance(a, dict) or a.get("type") not in ACTIONS:
            raise RuleError(f"Action {i}: pick what to do")
        kind_a = a["type"]
        if kind_a in LOCAL and event not in hook.HOOK_EVENTS:
            raise RuleError(f"Action {i}: {_lower(ACTION_LABELS[kind_a])} only works on a Claude Code "
                            "event (there is no running hook to answer for this trigger)")
        if kind_a == "block" and event not in hook.BLOCKABLE:
            raise RuleError(f"Action {i}: only a tool call (before it runs) or a prompt can be blocked")
        if kind_a == "inject" and event not in hook.INJECTABLE:
            raise RuleError(f"Action {i}: instructions can be added on session start, a prompt, "
                            "after a tool, or when Claude stops")
        if kind_a == "post":
            room = _str(a.get("room"), 64, f"Action {i}: the room")
            title = _str(a.get("room_title"), 120, f"Action {i}: the new room's name")
            huddle = _str(a.get("huddle"), 120, f"Action {i}: the huddle's project")
            if sum(bool(x) for x in (room, title, huddle)) != 1:
                raise RuleError(f"Action {i}: pick one room, a new room's name, or a huddle")
            act = {"type": "post", "text": _str(a.get("text"), 2000, f"Action {i}: the message")
                   or DEFAULT_POST}
            act.update({k: v for k, v in (("room", room), ("room_title", title),
                                          ("huddle", huddle)) if v})
            tiers.append("write")
        elif kind_a == "notify":
            act = {"type": "notify", "text": _str(a.get("text"), 1000, f"Action {i}: the message")
                   or DEFAULT_POST, "channel": _str(a.get("channel"), 40, "channel") or "all"}
            tiers.append("write")
        elif kind_a == "cap":
            cap = _str(a.get("cap"), 100, f"Action {i}: the capability", required=True)
            if not _CAP.match(cap):
                raise RuleError(f"Action {i}: {cap!r} is not a capability name (like notify.send)")
            tier = policy.check_cap(cap)
            args = a.get("args") or {}
            if not isinstance(args, dict):
                raise RuleError(f"Action {i}: the arguments must be a JSON object")
            if len(json.dumps(args)) > 4000:
                raise RuleError(f"Action {i}: the arguments are too long")
            act = {"type": "cap", "cap": cap, "worker": _str(a.get("worker"), 100, "worker") or "same",
                   "args": copy.deepcopy(args), "tier": tier}
            tiers.append(_max_tier("write", tier))
        elif kind_a == "inject":
            act = {"type": "inject", "text": _str(a.get("text"), 4000, f"Action {i}: the instructions",
                                                  required=True)}
            tiers.append("write")
        elif kind_a == "block":
            act = {"type": "block", "message": _str(a.get("message"), 1000, f"Action {i}: the message")
                   or "Blocked by a Rook rule."}
            tiers.append("write")
        else:
            act = {"type": "statusline", "text": _str(a.get("text"), 200, f"Action {i}: the status line",
                                                      required=True)}
            tiers.append("write")
        out_actions.append(act)
    return {"id": rid, "name": name, "enabled": rule.get("enabled") is not False,
            "note": _str(rule.get("note"), 300, "The note"),
            "trigger": t, "scope": {"kind": kind, "target": target},
            "actions": out_actions, "risk": _max_tier(*tiers)}


def needs_admin(rule: dict) -> bool:
    """Rules that run capabilities, or anything above ``write``, are for
    hub administrators only."""
    return any(a["type"] == "cap" for a in rule.get("actions") or ()) or \
        core_authz.tier_rank(rule.get("risk") or "write") >= core_authz.tier_rank("exec")


def describe(rule: dict) -> str:
    """One plain sentence: "When Claude is about to use Bash on worker x: block it"."""
    t = rule.get("trigger") or {}
    ev = EVENT_LABELS.get(t.get("event"), t.get("event") or "?")
    if t.get("event") in hook.TOOL_EVENTS and t.get("matcher"):
        ev = ev.replace("a tool", t["matcher"])
    if t.get("minutes"):
        ev += f" for {t['minutes']} min"
    if t.get("contains"):
        ev += f' (mentioning "{t["contains"]}")'
    sc = rule.get("scope") or {}
    where = {"all": "anywhere", "band": "on band {}", "worker": "on {}",
             "project": "in project {}", "agent": "for agent {}"}.get(sc.get("kind"), "anywhere")
    where = where.format(sc.get("target") or "")
    acts = ", ".join(_lower(ACTION_LABELS.get(a.get("type"), "?")) for a in rule.get("actions") or ())
    return f"When {_lower(ev)} {where}: {acts}."


def _lower(text: str) -> str:
    """Sentence case inside a sentence: "A session starts" -> "a session
    starts", but "Claude" stays a name."""
    return text if text.startswith("Claude") else text[:1].lower() + text[1:]


def applies_to(rule: dict, worker: dict) -> bool:
    """Band and worker scopes, decided here; project/agent go to every worker."""
    sc = rule.get("scope") or {}
    kind, target = sc.get("kind") or "all", str(sc.get("target") or "")
    if kind == "band":
        return target in (str(worker.get("band") or ""), str(worker.get("band_id") or ""))
    if kind == "worker":
        return target in (str(worker.get("worker_id") or ""), str(worker.get("name") or ""))
    return True


def fingerprint(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()[:12]


def worker_config(rules: list[dict], worker: dict, rev: int) -> dict:
    """The rules one worker needs, as ``ccmod.apply`` takes them. Remote
    actions are not sent (only that the rule forwards): the hub runs them."""
    out = []
    for r in rules:
        if not r.get("enabled") or r["trigger"]["event"] in ROOK_EVENTS or not applies_to(r, worker):
            continue
        local = [{"type": a["type"], "text": a.get("text", ""), "message": a.get("message", "")}
                 for a in r["actions"] if a["type"] in LOCAL]
        sc = r["scope"] if r["scope"]["kind"] in ("project", "agent") else {"kind": "all", "target": ""}
        out.append({"id": r["id"], "name": r["name"], "trigger": dict(r["trigger"]), "scope": dict(sc),
                    "local": local, "forward": any(a["type"] in REMOTE for a in r["actions"])})
    return {"v": 1, "rev": rev, "sha": fingerprint(out), "worker": str(worker.get("name") or ""),
            "band": str(worker.get("band") or ""), "rules": out}


class Store:
    """The plugin's JSON state file, written atomically on every change."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data = data if isinstance(data, dict) else {}
            for key, default in (("rev", 0), ("rules", []), ("rooms", {}), ("pushes", {}),
                                 ("history", []), ("activity", [])):
                if not isinstance(data.get(key), type(default)):
                    data[key] = copy.deepcopy(default)
            self._data = data
        return self._data

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(self._data, indent=1, ensure_ascii=False) + "\n",
                          0o600, fsync=False)

    # -- rules ---------------------------------------------------------------------------
    @property
    def rev(self) -> int:
        with self._lock:
            return int(self._load()["rev"])

    def rules(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._load()["rules"])

    def get(self, rid: str) -> dict | None:
        with self._lock:
            for r in self._load()["rules"]:
                if r["id"] == rid:
                    return copy.deepcopy(r)
        return None

    def _note(self, data: dict, action: str, rid: str, actor: str, note: str = "",
              doc: dict | None = None) -> None:
        entry = {"ts": self.clock(), "actor": actor, "action": action, "rule": rid, "note": note}
        if doc is not None:
            entry["doc"] = doc
        data["history"].insert(0, entry)
        del data["history"][HISTORY:]

    def save(self, rule: dict, actor: str, note: str = "", expect_rev: int | None = None,
             new: bool = False) -> dict:
        """Create or replace a checked rule; ``new`` refuses an id in use."""
        with self._lock:
            data = self._load()
            rules = data["rules"]
            idx = next((i for i, r in enumerate(rules) if r["id"] == rule["id"]), None)
            if idx is None and len(rules) >= MAX_RULES:
                raise RuleError(f"There are already {MAX_RULES} rules")
            if idx is not None and new:
                raise RuleError(f"A rule with the id {rule['id']!r} already exists; pick another name")
            cur = rules[idx] if idx is not None else None
            if expect_rev is not None and int(expect_rev) != int((cur or {}).get("rev") or 0):
                raise RuleError("Someone changed this rule since you opened it; reload and try again")
            body = {k: v for k, v in rule.items() if k not in ("rev", "updated", "actor")}
            if cur is not None and {k: v for k, v in cur.items()
                                    if k not in ("rev", "updated", "actor")} == body:
                return copy.deepcopy(cur)
            doc = {**body, "rev": int((cur or {}).get("rev") or 0) + 1, "updated": self.clock(),
                   "actor": actor}
            if idx is None:
                rules.append(doc)
            else:
                rules[idx] = doc
            data["rev"] += 1
            self._note(data, "save", rule["id"], actor, note, doc)
            self._save()
            return copy.deepcopy(doc)

    def delete(self, rid: str, actor: str, note: str = "") -> dict:
        with self._lock:
            data = self._load()
            before = len(data["rules"])
            data["rules"] = [r for r in data["rules"] if r["id"] != rid]
            if len(data["rules"]) == before:
                raise KeyError(f"no rule {rid!r}")
            data["rev"] += 1
            self._note(data, "delete", rid, actor, note)
            self._save()
            return {"ok": True, "deleted": rid, "rev": data["rev"]}

    def history(self, rid: str = "", limit: int = 50) -> list[dict]:
        with self._lock:
            rows = [h for h in self._load()["history"] if not rid or h["rule"] == rid]
            return copy.deepcopy(rows[:limit])

    # -- rooms made for rules -----------------------------------------------------------
    def room_for(self, title: str) -> str | None:
        with self._lock:
            return self._load()["rooms"].get(title)

    def set_room(self, title: str, room: str) -> None:
        with self._lock:
            self._load()["rooms"][title] = room
            self._save()

    # -- pushes ---------------------------------------------------------------------------
    def pushes(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._load()["pushes"])

    def record_push(self, worker_id: str, row: dict) -> None:
        with self._lock:
            self._load()["pushes"][worker_id] = {**row, "at": self.clock()}
            self._save()

    # -- activity -------------------------------------------------------------------------
    def log(self, row: dict) -> None:
        with self._lock:
            data = self._load()
            data["activity"].insert(0, {"ts": self.clock(), **row})
            del data["activity"][ACTIVITY:]
            self._save()

    def activity(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._load()["activity"][:limit])


def tier_from_roster(roster: dict, cap: str) -> str:
    """A capability's tier: the highest any live worker declares for it,
    floored by the built-in table (unknown everywhere: ``exec``)."""
    declared = []
    for e in (roster or {}).values():
        t = ((e or {}).get("tiers") or {}).get(cap)
        if t:
            declared.append(t)
    tiers = [core_authz.effective_tier(cap, d) for d in declared] or [core_authz.effective_tier(cap)]
    return _max_tier(*tiers)


def env_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,]+", value)
    return [str(v).strip() for v in (value or []) if str(v).strip()]


__all__ = ["ACTIONS", "EVENTS", "Policy", "RuleError", "Store", "validate", "worker_config",
           "describe", "needs_admin", "applies_to", "tier_from_roster"]
