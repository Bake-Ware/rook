"""Permission policy: document, compiler and evaluator (permissions.md 3).

One policy document per hub. It lives in ``policy.json`` in the hub data
directory (``$ROOK_DATA_DIR`` or next to the MCP stores); ``policy.yaml`` is
read instead when present and PyYAML is installed. Until the settings
framework owns it, edit the file (or use ``policy.set`` / the dashboard's
``/api/policy``); it is reloaded on change. A document that fails to parse
or validate is ignored and the last good one stays in force.

With no file the built-in *compatibility policy* applies: ``mode: audit``,
every existing principal keeps what it can do today, and only principal
kinds that do not exist yet (integrations, plugins) get restrictive
defaults. Nothing is denied until the operator sets ``mode: enforce``.

Evaluation (3.3): hard invariants, then for each principal in the
on-behalf-of chain the most specific matching rule by
``(target, cap, principal)`` specificity (file order breaks exact ties
only), else the principal's tier default. The call is allowed only if every
principal in the chain is.
"""

from __future__ import annotations

import ast
import copy
import fnmatch
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..core import authz
from ..core.facts import NodeFacts, compile_placement, evaluate_placement

log = logging.getLogger("rook.hub.policy")

MODES = ("off", "audit", "enforce")
CELLS = ("allow", "deny", "audit")
_ALL = {"read": "allow", "write": "allow", "exec": "allow", "admin": "allow"}

#: The compatibility policy (permissions 5.2): behaviour matches today's until
#: the operator turns enforcement on. ``unverified`` keeps full access here;
#: denying it exec/admin (the spec's recommendation, 3.8) is one edit:
#: ``"unverified": {"exec": "deny", "admin": "deny"}``.
DEFAULT_POLICY: dict = {
    "version": 1,
    "rev": 0,
    "mode": "audit",
    "defaults": {"read": "allow", "write": "allow", "exec": "deny", "admin": "deny"},
    "principals": {
        "role:operator": dict(_ALL),
        "role:system": dict(_ALL),
        "role:agent": {"read": "allow", "write": "allow", "exec": "allow", "admin": "audit"},
        "role:readonly": {"read": "allow", "write": "deny", "exec": "deny", "admin": "deny"},
        "role:integration": {"read": "allow", "write": "allow", "exec": "deny", "admin": "deny"},
        "human:owner": dict(_ALL),
        "human:member": {"read": "allow", "write": "allow", "exec": "allow", "admin": "audit"},
        "human:dashboard": dict(_ALL),
        "token:static": dict(_ALL),
        "integration:*": {"read": "allow", "write": "allow", "exec": "deny", "admin": "deny"},
        "plugin:*": {"read": "deny", "write": "deny", "exec": "deny", "admin": "deny"},
        "band:unauthenticated": {"read": "allow", "write": "deny", "exec": "deny", "admin": "deny"},
        "unverified": dict(_ALL),
    },
    "groups": {},
    "principal_groups": {},
    "tiers": {},
    # Hub administration that did not exist before permissions: band settings
    # and policy changes are for band owners and operator-role tokens only
    # (maintainer decision). These caps also hard-gate on the principal in
    # their handlers, whatever the mode.
    "rules": [
        {"id": "hub-admin-owners-operators-agents", "who": "role:agent",
         "deny": ["policy.set", "settings.set", "settings.reset"], "on": "rook"},
        {"id": "hub-admin-owners-operators-members", "who": "human:member",
         "deny": ["policy.set", "settings.set", "settings.reset"], "on": "rook"},
    ],
}

# Principals whose evaluation errors fail open (3.8): the owner is never
# locked out by a bug.
_FAIL_OPEN_ROLES = frozenset({"operator", "owner", "system"})
_FAIL_OPEN_IDS = frozenset({"token:static", "human:dashboard"})


class PolicyError(ValueError):
    pass


# -- evaluation inputs -------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    """An authenticated principal (1.2). ``id`` is ``<kind>:<id>`` (or the
    bare ``unverified``); ``groups`` are built-in groups such as
    ``human:owner``; ``via`` is the on-behalf-of chain, outermost first."""

    id: str
    kind: str
    role: str = ""
    groups: tuple = ()
    via: tuple = ()
    verified: bool = True
    label: str = ""

    def key(self) -> tuple:
        return (self.id, self.role, self.groups, self.verified)

    def fail_open(self) -> bool:
        return (self.role in _FAIL_OPEN_ROLES or self.id in _FAIL_OPEN_IDS
                or any(g == "human:owner" for g in self.groups))


@dataclass(frozen=True)
class Target:
    """The worker a call is aimed at (``None`` id = broadcast)."""

    id: str | None = None
    name: str = ""
    device_id: str = ""
    facts: dict = field(default_factory=dict)
    roles: frozenset = frozenset()
    is_rook: bool = False          # the verified hub node (valid is_hub grant, or local)
    claims_rook: bool = False      # announced the reserved name
    banned: bool = False
    tiers: dict = field(default_factory=dict)  # declared {cap: r|w|x|a}

    def key(self) -> tuple:
        return (self.id, self.name.lower(), self.device_id, self.is_rook,
                self.claims_rook, self.banned)


@dataclass
class Decision:
    decision: str                   # allow | deny | would_deny | error_allow | error_deny | off
    cap: str
    tier: str
    principal: str
    via: tuple = ()
    rule: str | None = None
    rev: int = 0
    target: str | None = None
    target_name: str = ""
    reason: str = ""
    runner_up: str | None = None
    tags: tuple = ()

    @property
    def denied(self) -> bool:
        return self.decision in ("deny", "error_deny")

    def journal(self) -> dict:
        return {"principal": self.principal, "decision": self.decision, "rule": self.rule,
                "policy_rev": self.rev, "tier": self.tier}

    def explain(self) -> dict:
        out = {"decision": self.decision, "cap": self.cap, "tier": self.tier,
               "principal": self.principal, "via": list(self.via), "rule": self.rule,
               "runner_up": self.runner_up, "rev": self.rev,
               "target": self.target_name or self.target, "reason": self.reason}
        if self.tags:
            out["tags"] = list(self.tags)
        return out

    def denial(self) -> dict:
        where = self.target_name or self.target or "the band"
        return {"ok": False,
                "error": (f"denied: {self.principal} may not call {self.cap} "
                          f"({self.tier}) on {where}"),
                "denied": {"tier": self.tier, "rule": self.rule, "rev": self.rev,
                           "principal": self.principal, "via": list(self.via)}}


# -- selectors ---------------------------------------------------------------

_FACT_HINT = re.compile(r"[()&|]|^is_|^not |\b(and|or)\b")


def _fact_source(expr: str) -> str:
    s = expr[5:] if expr.startswith("fact:") else expr
    s = s.replace("&&", " and ").replace("||", " or ")
    s = re.sub(r"!(?!=)", " not ", s)
    s = re.sub(r"\bhas\(\s*([A-Za-z_]\w*)", r"has('\1'", s)   # spec form: has(gpu, vram_gb>=8)
    s = re.sub(r"\brole\(\s*['\"]?(\w+)['\"]?\s*\)",
               lambda m: m.group(1) if m.group(1).startswith("is_") else "is_" + m.group(1), s)
    return s.strip()


def _signed_only(src: str) -> bool:
    """True if a fact expression names signed roles only (is_* but not is_embedded)."""
    try:
        tree = compile_placement(src)
    except Exception:
        return False
    names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    return bool(names) and not calls and all(
        n.startswith("is_") and n != "is_embedded" for n in names)


class _TargetSel:
    def __init__(self, spec: Any, groups: dict) -> None:
        if isinstance(spec, list):
            self.items = [_TargetSel(s, groups) for s in spec]
            self.kind = "list"
            if not self.items:
                raise PolicyError("empty target list")
            return
        if not isinstance(spec, str) or not spec.strip():
            raise PolicyError(f"bad target selector {spec!r}")
        spec = spec.strip()
        self.neg = spec.startswith("!")
        body = spec[1:].strip() if self.neg else spec
        self.value = body
        self.self_reported = False
        if body == "*":
            self.kind, self.score = "any", 0
        elif body.lower() == authz.RESERVED_HUB_NAME:
            self.kind, self.score = "rook", 4
        elif body.startswith("id:"):
            self.kind, self.score, self.value = "id", 4, body[3:]
        elif body.startswith("device:"):
            self.kind, self.score, self.value = "device", 4, body[7:]
        elif body.startswith("group:"):
            self.kind, self.score, self.value = "group", 3, body[6:]
            if self.value not in groups:
                raise PolicyError(f"unknown target group {self.value!r}")
            g = groups[self.value]
            if isinstance(g, dict):
                self.group_expr = _fact_source(str(g.get("match", "")))
                try:
                    compile_placement(self.group_expr)
                except Exception as e:
                    raise PolicyError(f"group {self.value!r}: {e}") from None
                self.self_reported = not _signed_only(self.group_expr)
                self.group_names = None
            else:
                self.group_expr = None
                self.group_names = {str(x).lower() for x in (g or [])}
        elif body.startswith("fact:") or _FACT_HINT.search(body):
            src = _fact_source(body)
            try:
                compile_placement(src)
            except Exception as e:
                raise PolicyError(f"bad fact expression {body!r}: {e}") from None
            self.kind, self.value = "fact", src
            signed = _signed_only(src)
            self.score = 2 if signed else 1
            self.self_reported = not signed
        else:
            self.kind, self.score = "name", 4
        if self.neg:
            self.score = max(0, self.score - 1)

    def _match_one(self, t: Target) -> bool:
        k = self.kind
        if k == "any":
            hit = True
        elif k == "rook":
            hit = t.is_rook
        elif k == "id":
            hit = t.id == self.value
        elif k == "device":
            hit = bool(t.device_id) and t.device_id == self.value
        elif k == "name":
            hit = bool(t.name) and t.name.lower() == self.value.lower()
        elif k == "group":
            if self.group_names is not None:
                hit = (t.name.lower() in self.group_names
                       or (t.id or "").lower() in self.group_names
                       or f"id:{t.id}".lower() in self.group_names)
            else:
                hit = evaluate_placement(self.group_expr, _node(t))
        else:  # fact
            hit = evaluate_placement(self.value, _node(t))
        return (not hit) if self.neg else hit

    def match(self, t: Target) -> int | None:
        """Specificity of the (best) matching element, or None."""
        if self.kind == "list":
            scores = [s for s in (i.match(t) for i in self.items) if s is not None]
            return max(scores) if scores else None
        return self.score if self._match_one(t) else None

    def fact_only(self) -> bool:
        if self.kind == "list":
            return all(i.fact_only() for i in self.items)
        return self.self_reported


def _node(t: Target) -> NodeFacts:
    return NodeFacts(node_id=t.id or "", name=t.name, roles=frozenset(t.roles), hw=dict(t.facts))


class _CapSel:
    def __init__(self, spec: Any) -> None:
        if isinstance(spec, list):
            if not spec:
                raise PolicyError("empty cap list")
            self.items = [_CapSel(s) for s in spec]
            self.kind = "list"
            return
        if not isinstance(spec, str) or not spec.strip():
            raise PolicyError(f"bad cap selector {spec!r}")
        s = spec.strip()
        self.value = s
        if s == "*":
            self.kind, self.score = "any", 0
        elif s.startswith("tier:"):
            t = authz.norm_tier(s[5:])
            if t is None:
                raise PolicyError(f"unknown tier in {s!r}")
            self.kind, self.score, self.value = "tier", 1, t
        elif s.startswith("tag:"):
            self.kind, self.score, self.value = "tag", 2, s[4:]
        elif "*" in s or "?" in s:
            self.kind, self.score = "glob", 3
        else:
            self.kind, self.score = "exact", 4

    def match(self, cap: str, tier: str, tags: tuple) -> int | None:
        if self.kind == "list":
            scores = [s for s in (i.match(cap, tier, tags) for i in self.items) if s is not None]
            return max(scores) if scores else None
        k = self.kind
        hit = (k == "any" or (k == "tier" and tier == self.value)
               or (k == "tag" and self.value in tags)
               or (k == "glob" and fnmatch.fnmatchcase(cap, self.value))
               or (k == "exact" and cap == self.value))
        return self.score if hit else None


class _WhoSel:
    def __init__(self, spec: Any, principal_groups: dict) -> None:
        if not isinstance(spec, str) or not spec.strip():
            raise PolicyError(f"bad principal selector {spec!r}")
        s = spec.strip()
        self.value = s
        if s == "*":
            self.kind, self.score = "any", 0
        elif s.startswith("role:"):
            self.kind, self.score, self.value = "role", 2, s[5:]
        elif s.startswith("group:"):
            self.kind, self.score, self.value = "group", 3, s[6:]
            if self.value not in principal_groups:
                raise PolicyError(f"unknown principal group {self.value!r}")
            self.members = set(principal_groups[self.value] or [])
        elif s in ("human:owner", "human:member"):
            self.kind, self.score = "builtin", 3
        elif s.endswith(":*"):
            self.kind, self.score, self.value = "kind", 1, s[:-2]
        else:
            self.kind, self.score = "exact", 4

    def match(self, p: Principal) -> int | None:
        k = self.kind
        hit = (k == "any" or (k == "role" and p.role == self.value)
               or (k == "group" and p.id in self.members)
               or (k == "builtin" and self.value in p.groups)
               or (k == "kind" and p.kind == self.value)
               or (k == "exact" and p.id == self.value))
        return self.score if hit else None


@dataclass
class _Rule:
    id: str
    index: int
    action: str
    who: _WhoSel
    caps: _CapSel
    on: _TargetSel
    note: str = ""


# -- compiled policy ---------------------------------------------------------

class Policy:
    """A validated, compiled policy document."""

    def __init__(self, doc: dict) -> None:
        if not isinstance(doc, dict):
            raise PolicyError("policy must be an object")
        doc = copy.deepcopy(doc)
        if doc.get("version", 1) != 1:
            raise PolicyError("unsupported policy version")
        self.mode = doc.get("mode", "audit")
        if self.mode not in MODES:
            raise PolicyError(f"mode must be one of {MODES}")
        self.rev = int(doc.get("rev", 0) or 0)
        self.defaults = self._table(doc.get("defaults") or {}, "defaults")
        self.principals: dict[str, dict] = {}
        self.principal_modes: dict[str, str] = {}
        for pid, table in (doc.get("principals") or {}).items():
            if not isinstance(table, dict):
                raise PolicyError(f"principals.{pid} must be an object")
            m = table.get("mode")
            if m is not None:
                if m not in MODES:
                    raise PolicyError(f"principals.{pid}.mode must be one of {MODES}")
                self.principal_modes[pid] = m
            self.principals[pid] = self._table({k: v for k, v in table.items() if k != "mode"},
                                               f"principals.{pid}")
        groups = doc.get("groups") or {}
        pgroups = doc.get("principal_groups") or {}
        if not isinstance(groups, dict) or not isinstance(pgroups, dict):
            raise PolicyError("groups and principal_groups must be objects")
        self.tiers: dict[str, tuple[str, bool, tuple]] = {}
        for cap, spec in (doc.get("tiers") or {}).items():
            spec = spec if isinstance(spec, dict) else {"tier": spec}
            t = authz.norm_tier(spec.get("tier"))
            if t is None:
                raise PolicyError(f"tiers.{cap}: unknown tier")
            self.tiers[cap] = (t, bool(spec.get("lower")), tuple(spec.get("tags") or ()))
        self.rules: list[_Rule] = []
        seen = set()
        for i, r in enumerate(doc.get("rules") or []):
            if not isinstance(r, dict):
                raise PolicyError(f"rule #{i} must be an object")
            rid = str(r.get("id") or f"rule-{i + 1}")
            if rid in seen:
                raise PolicyError(f"duplicate rule id {rid!r}")
            seen.add(rid)
            if ("allow" in r) == ("deny" in r):
                raise PolicyError(f"rule {rid!r} needs exactly one of allow / deny")
            action = "allow" if "allow" in r else "deny"
            try:
                self.rules.append(_Rule(rid, i, action, _WhoSel(r.get("who"), pgroups),
                                        _CapSel(r[action]), _TargetSel(r.get("on", "*"), groups),
                                        str(r.get("note") or "")))
            except PolicyError as e:
                raise PolicyError(f"rule {rid!r}: {e}") from None
        self.doc = doc
        self._memo: dict[tuple, tuple] = {}
        self._memo_lock = threading.Lock()

    @staticmethod
    def _table(table: dict, where: str) -> dict:
        out = {}
        for tier, cell in table.items():
            t = authz.norm_tier(tier)
            if t is None:
                raise PolicyError(f"{where}: unknown tier {tier!r}")
            if cell not in CELLS:
                raise PolicyError(f"{where}.{tier}: must be one of {CELLS}")
            out[t] = cell
        return out

    # -- tiers -----------------------------------------------------------------
    def tier_of(self, cap: str, declared: Any = None) -> tuple[str, tuple]:
        ov = self.tiers.get(cap)
        tier = authz.effective_tier(cap, declared, ov[0] if ov else None, ov[1] if ov else False)
        tags = tuple(dict.fromkeys(authz.builtin_tags(cap) + (ov[2] if ov else ())))
        return tier, tags

    # -- defaults ----------------------------------------------------------------
    def _default_chain(self, p: Principal) -> list[tuple[str, dict]]:
        keys = [p.id, *p.groups]
        if p.role:
            keys.append(f"role:{p.role}")
        keys.append(f"{p.kind}:*")
        out = [(k, self.principals[k]) for k in dict.fromkeys(keys) if k in self.principals]
        out.append(("defaults", self.defaults))
        return out

    def default_cell(self, p: Principal, tier: str) -> tuple[str, str]:
        for name, table in self._default_chain(p):
            if tier in table:
                return table[tier], f"default:{name}"
        return "deny", "default:none"

    def mode_for(self, p: Principal) -> str:
        if self.mode == "off":
            return "off"
        for k in (p.id, *p.groups, f"role:{p.role}" if p.role else None, f"{p.kind}:*"):
            if k and k in self.principal_modes:
                return self.principal_modes[k]
        return self.mode

    # -- evaluation ----------------------------------------------------------------
    def _one(self, p: Principal, cap: str, tier: str, tags: tuple,
             t: Target) -> tuple[str, str, str | None]:
        """(cell, rule_id, runner_up) for one principal."""
        matches = []
        for r in self.rules:
            ps = r.who.match(p)
            if ps is None:
                continue
            cs = r.caps.match(cap, tier, tags)
            if cs is None:
                continue
            ts = r.on.match(t)
            if ts is None:
                continue
            matches.append(((ts, cs, ps, -r.index), r))
        if not matches:
            cell, name = self.default_cell(p, tier)
            return cell, name, None
        matches.sort(key=lambda m: m[0], reverse=True)
        win = matches[0][1]
        runner = matches[1][1].id if len(matches) > 1 else self.default_cell(p, tier)[1]
        return win.action, win.id, runner

    def evaluate(self, chain: list[Principal], cap: str, target: Target,
                 declared_tier: Any = None) -> Decision:
        tier, tags = self.tier_of(cap, declared_tier if declared_tier is not None
                                  else target.tiers.get(cap))
        head = chain[0] if chain else Principal("unverified", "unverified", verified=False)
        via = tuple(p.id for p in chain[1:]) or head.via
        d = Decision(decision="allow", cap=cap, tier=tier, principal=head.id, via=via,
                     rev=self.rev, target=target.id, target_name=target.name, tags=tags)
        mode = self.mode_for(head)
        if mode == "off":
            d.decision, d.reason = "off", "policy mode off"
            return d
        key = (tuple(p.key() for p in chain), cap, tier, target.key())
        with self._memo_lock:
            hit = self._memo.get(key)
        if hit is None:
            hit = self._evaluate(chain or [head], cap, tier, tags, target)
            with self._memo_lock:
                if len(self._memo) > 8192:
                    self._memo.clear()
                self._memo[key] = hit
        cell, rule, runner, reason = hit
        d.rule, d.runner_up, d.reason = rule, runner, reason
        if cell == "allow":
            d.decision = "allow"
        elif cell == "audit":
            d.decision = "would_deny"
        else:
            d.decision = "deny" if mode == "enforce" else "would_deny"
        return d

    def _evaluate(self, chain, cap, tier, tags, t: Target) -> tuple:
        # 1. Hard invariants (policy cannot override them).
        if t.banned:
            return "deny", "invariant:banned", None, "target is deauthed (banned)"
        if t.claims_rook and not t.is_rook:
            return "deny", "invariant:rook-impostor", None, "target claims 'rook' without a valid is_hub grant"
        if t.id is None and authz.tier_rank(tier) > authz.tier_rank("read"):
            return "deny", "invariant:broadcast", None, "untargeted call above read"
        audit_hit = None
        for p in chain:
            cell, rule, runner = self._one(p, cap, tier, tags, t)
            if cell == "deny":
                return "deny", rule, runner, f"{p.id}: {rule}"
            if cell == "audit" and audit_hit is None:
                audit_hit = ("audit", rule, runner, f"{p.id}: {rule} (audit)")
        if audit_hit:
            return audit_hit
        cell, rule, runner = self._one(chain[0], cap, tier, tags, t)
        return "allow", rule, runner, ""

    # -- lint ----------------------------------------------------------------------
    def lint(self) -> list[str]:
        out = []
        for i, a in enumerate(self.rules):
            if a.action == "allow" and a.on.fact_only():
                tier_sel = a.caps
                risky = tier_sel.kind != "tier" or authz.tier_rank(tier_sel.value) > authz.tier_rank("write")
                if risky:
                    out.append(f"rule {a.id!r}: allow on a self-reported fact target; a "
                               f"worker can claim this fact to receive these calls")
            for b in self.rules[i + 1:]:
                if a.action != b.action and a.who.value == b.who.value \
                        and a.who.score == b.who.score and _score(a.caps) == _score(b.caps) \
                        and _score(a.on) == _score(b.on):
                    out.append(f"rules {a.id!r} and {b.id!r} tie on specificity; the earlier "
                               f"one ({a.id!r}) wins where both match")
        return out


def _score(sel) -> int:
    if getattr(sel, "kind", "") == "list":
        return max(_score(i) for i in sel.items)
    return sel.score


# -- store -------------------------------------------------------------------

def _load_doc(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        import yaml  # optional; only when an operator chose YAML
        return yaml.safe_load(text) or {}
    return json.loads(text)


class PolicyStore:
    """Loads the policy file, keeps the last good compiled policy, reloads on
    change (checked at most every ``check_every`` seconds, no I/O otherwise)."""

    def __init__(self, path: str | None, check_every: float = 2.0) -> None:
        self.path = path
        self._check_every = check_every
        self._checked = 0.0
        self._mtime: float | None = None
        self.error: str | None = None
        self.policy = Policy(DEFAULT_POLICY)
        self.source = "built-in compatibility policy"
        self._lock = threading.Lock()
        self._maybe_reload(force=True)

    @staticmethod
    def default_path(data_dir: str | None) -> str | None:
        if not data_dir:
            return None
        yaml_path = os.path.join(data_dir, "policy.yaml")
        if os.path.exists(yaml_path):
            return yaml_path
        return os.path.join(data_dir, "policy.json")

    def _maybe_reload(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._checked < self._check_every:
            return
        self._checked = now
        if not self.path:
            return
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            if self._mtime is not None:  # file removed: back to the compatibility policy
                self._mtime = None
                self.policy, self.source, self.error = Policy(DEFAULT_POLICY), "built-in compatibility policy", None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self.policy = Policy(_load_doc(self.path))
            self.source, self.error = self.path, None
            log.info("policy rev %s loaded from %s (mode %s)", self.policy.rev, self.path,
                     self.policy.mode)
        except Exception as e:  # keep the last good policy (3.8)
            self.error = f"{type(e).__name__}: {e}"
            log.error("ROOK AUTHZ ALERT: policy %s invalid (%s); keeping rev %s",
                      self.path, self.error, self.policy.rev)

    def current(self) -> Policy:
        self._maybe_reload()
        return self.policy

    def save(self, doc: dict) -> Policy:
        """Validate and persist a new document with ``rev`` = old rev + 1.
        Refuses a document that leaves no principal with admin on ``rook``."""
        if not self.path:
            raise PolicyError("this hub has no policy file location (no data dir)")
        with self._lock:
            old = self.current()
            doc = dict(doc)
            doc["rev"] = old.rev + 1
            new = Policy(doc)
            if not has_admin_holder(new):
                raise PolicyError("refused: no principal would hold admin on rook")
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(new.doc, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            self.policy, self.source, self.error = new, self.path, None
            try:
                self._mtime = os.stat(self.path).st_mtime
            except OSError:
                pass
            return new


def has_admin_holder(policy: Policy) -> bool:
    rook = Target(id="rook", name="rook", is_rook=True)
    probes = [Principal("human:dashboard", "human", "owner", ("human:owner",)),
              Principal("token:static", "token", "operator"),
              Principal("human:x", "human", "owner", ("human:owner",)),
              Principal("token:x", "token", "operator")]
    for p in probes:
        cell, _, _ = policy._one(p, "policy.set", "admin", (), rook)
        if cell != "deny":
            return True
    return False


def summarize_diff(old: dict, new: dict) -> dict:
    keys = sorted(set(old) | set(new))
    changed = [k for k in keys if old.get(k) != new.get(k) and k != "rev"]
    old_rules = {r.get("id") for r in old.get("rules") or [] if isinstance(r, dict)}
    new_rules = {r.get("id") for r in new.get("rules") or [] if isinstance(r, dict)}
    return {"changed": changed, "rules_added": sorted(x for x in new_rules - old_rules if x),
            "rules_removed": sorted(x for x in old_rules - new_rules if x),
            "mode": [old.get("mode"), new.get("mode")]}
