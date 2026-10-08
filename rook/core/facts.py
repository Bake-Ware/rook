"""Node facts and placement.

Every node (the hub and each worker) describes itself with two kinds of fact:

* **Roles** (``is_hub``, later others) come from a *grant signed by the root
  key* (``docs/design/permissions.md`` section 4). A node cannot make itself the hub by saying so. Grants ride in
  the announce as ``grants``; :func:`roles_from_announce` checks them
  (``rook-grant-v1`` signature by a trusted root, window, band scope, and a
  signed announce proving the node holds the grant key; see
  :mod:`rook.core.authz`). The hub's own node holds ``is_hub`` as local
  authority (it is the key holder).
* **Hardware/platform facts** (os, arch, cpus, memory, pty, display, camera,
  gpu, embedded) are self-reported. They only gate *placement* (where a plugin
  runs); they grant nothing. Operators can add or correct them with the
  ``ROOK_NODE_FACTS`` env var (a JSON object merged over the detected facts).

Facts ride in the announce under ``facts`` (a small flat dict). Build-167
workers do not send it; receivers treat a missing ``facts`` as ``{}``.

Placement expressions are tiny Python-syntax predicates evaluated safely over
the facts (no ``eval``)::

    is_hub
    not is_hub
    has('camera')
    has('gpu', vram_gb >= 8)
    is_embedded or os == 'android'
    has('pty') and arch in ('x86_64', 'aarch64')

This module is stdlib-only: it ships inside the worker bundle.
"""

from __future__ import annotations

import ast
import glob
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("rook.core.facts")

# -- role grants -------------------------------------------------------------

RoleVerifier = Callable[[dict, str], "str | None"]


def _verify_signed(grant: dict, node_id: str) -> str | None:
    """Default verifier: a ``rook-grant-v1`` signature by a trusted root
    (:func:`rook.core.authz.default_anchors`), validity window, known role and
    ``sub.worker_id`` binding. Band scope and key possession need the whole
    announce; :func:`roles_from_announce` checks those."""
    from . import authz
    ok, _ = authz.verify_grant(grant, authz.default_anchors(), worker_id=node_id)
    return grant.get("role") if ok else None


_role_verifier: RoleVerifier = _verify_signed
_custom_verifier = False


def set_role_verifier(fn: RoleVerifier | None) -> None:
    """Install a grant verifier: ``fn(grant, node_id) -> role | None``.

    The default checks a ``rook-grant-v1`` ed25519 signature by a trusted
    root, expiry and role (``docs/design/permissions.md`` section 4.4);
    :func:`roles_from_announce` additionally requires band scope and that
    the announcer holds ``sub.key``. ``None`` restores the default.
    """
    global _role_verifier, _custom_verifier
    _role_verifier = fn or _verify_signed
    _custom_verifier = fn is not None


def verify_role_grant(grant: Any, node_id: str) -> str | None:
    """The role a grant confers on ``node_id``, or ``None`` if it does not
    verify. Never raises."""
    if not isinstance(grant, dict) or not node_id:
        return None
    try:
        role = _role_verifier(grant, node_id)
    except Exception:
        log.warning("role grant verifier raised; grant rejected", exc_info=True)
        return None
    return role if isinstance(role, str) and role else None


def roles_from_grants(grants: Any, node_id: str) -> frozenset[str]:
    if not isinstance(grants, list):
        return frozenset()
    return frozenset(r for r in (verify_role_grant(g, node_id) for g in grants) if r)


def roles_from_announce(msg: dict, band: str | None = None,
                        anchors: "list[str] | None" = None) -> frozenset[str]:
    """Roles an announce *proves*: each grant must verify (root signature,
    window, band scope when ``band`` is given, revocation, bindings) and the
    announce must be signed by the grant's key (``asig``), so a grant copied
    into another node's announce confers nothing. A custom verifier installed
    with :func:`set_role_verifier` replaces the signature check. Never raises."""
    if not isinstance(msg, dict):
        return frozenset()
    wid = str(msg.get("worker_id") or "")
    if _custom_verifier:
        return roles_from_grants(msg.get("grants"), wid)
    try:
        from . import authz
        held = authz.held_roles(msg, anchors if anchors is not None else authz.default_anchors(),
                                band=band)
        return frozenset(held)
    except Exception:
        log.warning("role grant check failed; no roles granted", exc_info=True)
        return frozenset()


# -- hardware / platform facts ---------------------------------------------

_BOOL_FACTS = ("pty", "display", "camera", "embedded")
_ROLE_KEYS = ("role", "roles", "grants")


def clean_facts(raw: Any) -> dict:
    """Facts as received: a dict, with any key that could pass for a role
    (``is_*``, ``role``, ``roles``, ``grants``) dropped. Roles come from
    signed grants only; a self-reported ``is_hub`` fact must never count."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items()
            if isinstance(k, str) and not k.startswith("is_") and k not in _ROLE_KEYS}
_MAX_FACTS_BYTES = 1024  # facts ride every announce; keep them tiny


def _mem_gb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1048576, 1)
    except Exception:
        pass
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except Exception:
        return None


def _gpus() -> list[dict]:
    """NVIDIA GPUs via nvidia-smi (one short, bounded call at start-up).
    Other vendors are left to ``ROOK_NODE_FACTS`` for now."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    gpus = []
    for line in (out.stdout or "").splitlines():
        name, _, mem = line.rpartition(",")
        try:
            gpus.append({"vendor": "nvidia", "name": name.strip()[:40],
                         "vram_gb": round(float(mem) / 1024, 1)})
        except ValueError:
            continue
    return gpus[:8]


def _has_conpty() -> bool:
    """Windows 10 1809+ pseudoconsoles (what ``work.stream`` needs there)."""
    try:
        import ctypes
        return hasattr(ctypes.WinDLL("kernel32"), "CreatePseudoConsole")
    except (ImportError, OSError, AttributeError):
        return False


def detect_facts() -> dict:
    """Detect this machine's platform/hardware facts. Cheap and bounded; call
    once at start-up. Every probe is best-effort: a failure leaves the fact out
    (unknown), it never raises."""
    facts: dict[str, Any] = {}
    try:
        system = platform.system().lower()
        android = "ANDROID_ROOT" in os.environ or getattr(sys, "platform", "") == "android"
        facts["os"] = "android" if android else system
        facts["arch"] = platform.machine().lower()
        facts["py"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        facts["cpus"] = os.cpu_count()
        mem = _mem_gb()
        if mem is not None:
            facts["mem_gb"] = mem
        facts["pty"] = os.name == "posix" or (system == "windows" and _has_conpty())
        if system == "linux" and not android:
            facts["display"] = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            facts["camera"] = bool(glob.glob("/dev/video*"))
        elif system in ("windows", "darwin"):
            facts["display"] = True
        facts["embedded"] = bool(android or os.path.exists("/proc/device-tree/model")
                                 or (mem is not None and mem < 2))
        gpus = _gpus()
        if gpus:
            facts["gpu"] = gpus
    except Exception:
        log.debug("fact detection failed", exc_info=True)
    raw = os.environ.get("ROOK_NODE_FACTS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, dict):
                facts.update(clean_facts(extra))
        except ValueError:
            log.warning("ROOK_NODE_FACTS is not a JSON object; ignored")
    return {k: v for k, v in facts.items() if v is not None}


_local_facts: dict | None = None


def local_facts() -> dict:
    """:func:`detect_facts` for this process, detected once and cached."""
    global _local_facts
    if _local_facts is None:
        _local_facts = detect_facts()
    return dict(_local_facts)


def wire_facts(facts: dict) -> dict:
    """The announce-safe form: bounded size, JSON-serialisable, falsy booleans
    dropped (absent == false)."""
    out = {k: v for k, v in facts.items()
           if not (k in _BOOL_FACTS and v is False)}
    try:
        if len(json.dumps(out, separators=(",", ":"))) > _MAX_FACTS_BYTES:
            out.pop("gpu", None)
            out = {k: v for k, v in out.items()
                   if isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 40)}
    except (TypeError, ValueError):
        out = {}
    return out


@dataclass(frozen=True)
class NodeFacts:
    """What placement is evaluated against for one node."""

    node_id: str = ""
    name: str = ""
    roles: frozenset = field(default_factory=frozenset)
    hw: dict = field(default_factory=dict)

    @property
    def is_hub(self) -> bool:
        return "is_hub" in self.roles

    @property
    def is_embedded(self) -> bool:
        return bool(self.hw.get("embedded"))

    def has(self, name: str, *conds: Callable[[dict], bool], **eq: Any) -> bool:
        """Whether the node has hardware ``name``. A fact that is a list (e.g.
        ``gpu``) matches when any item satisfies every condition; a dict
        matches on its own keys; a scalar is truthy. ``conds`` are predicates
        over the item dict, ``eq`` exact-match keys."""
        v = self.hw.get(name)
        items = v if isinstance(v, list) else [v]
        for item in items:
            if not item:
                continue
            if not conds and not eq:
                return True
            d = item if isinstance(item, dict) else {}
            if all(d.get(k) == want for k, want in eq.items()) and all(c(d) for c in conds):
                return True
        return False

    @classmethod
    def from_announce(cls, msg: dict, band: str | None = None) -> "NodeFacts":
        """Facts for a remote node from its announce. Missing/garbled fields
        (build-167 workers) yield empty facts and no roles. Roles need a
        verified grant plus proof of key possession (:func:`roles_from_announce`)."""
        wid = str(msg.get("worker_id") or "")
        return cls(node_id=wid, name=str(msg.get("name") or ""),
                   roles=roles_from_announce(msg, band),
                   hw=clean_facts(msg.get("facts")))


# -- placement expressions ---------------------------------------------------

class PlacementError(ValueError):
    pass


_CMP = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a is not None and b is not None and a < b,
    ast.LtE: lambda a, b: a is not None and b is not None and a <= b,
    ast.Gt: lambda a, b: a is not None and b is not None and a > b,
    ast.GtE: lambda a, b: a is not None and b is not None and a >= b,
    ast.In: lambda a, b: b is not None and a in b,
    ast.NotIn: lambda a, b: b is not None and a not in b,
}


def compile_placement(expr: str) -> ast.Expression:
    """Parse and validate a placement expression; raises PlacementError."""
    if not isinstance(expr, str) or not expr.strip():
        raise PlacementError("placement expression must be a non-empty string")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise PlacementError(f"bad placement expression {expr!r}: {e.msg}") from None
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
               ast.Compare, ast.Name, ast.Load, ast.Constant, ast.Call, ast.Tuple,
               ast.List, ast.keyword, *_CMP.keys())
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise PlacementError(f"{type(node).__name__} not allowed in placement {expr!r}")
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id == "has"):
                raise PlacementError(f"only has(...) calls are allowed in placement {expr!r}")
            if not node.args or not isinstance(node.args[0], ast.Constant) \
                    or not isinstance(node.args[0].value, str):
                raise PlacementError(f"has() needs a fact name first in {expr!r}")
    return tree


def _eval(node: ast.AST, facts: NodeFacts, scope: dict | None) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, facts, scope)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_eval(e, facts, scope) for e in node.elts)
    if isinstance(node, ast.Name):
        n = node.id
        if scope is not None:           # inside has('x', ...): item fields
            return scope.get(n)
        if n in ("any", "anywhere"):
            return True
        if n == "is_embedded":
            return facts.is_embedded
        if n.startswith("is_"):
            return n in facts.roles
        return facts.hw.get(n)
    if isinstance(node, ast.BoolOp):
        vals = (_eval(v, facts, scope) for v in node.values)
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp):
        return not _eval(node.operand, facts, scope)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, facts, scope)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, facts, scope)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        name = node.args[0].value
        conds = [(lambda item, c=c: bool(_eval(c, facts, item))) for c in node.args[1:]]
        eq = {kw.arg: _eval(kw.value, facts, None) for kw in node.keywords if kw.arg}
        return facts.has(name, *conds, **eq)
    raise PlacementError(f"unsupported node {type(node).__name__}")


def evaluate_placement(expr: "str | Callable[[NodeFacts], bool] | None",
                       facts: NodeFacts) -> bool:
    """Evaluate a placement predicate (expression string or callable) over a
    node's facts. ``None`` = anywhere. Errors count as "does not match"."""
    if expr is None:
        return True
    if callable(expr):
        try:
            return bool(expr(facts))
        except Exception:
            log.warning("placement callable raised; treated as no match", exc_info=True)
            return False
    try:
        return bool(_eval(compile_placement(expr), facts, None))
    except PlacementError:
        log.warning("invalid placement %r; treated as no match", expr)
        return False
    except Exception:
        log.warning("placement %r failed to evaluate; treated as no match", expr, exc_info=True)
        return False
