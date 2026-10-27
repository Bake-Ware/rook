"""``trust.*``: the trust map, read by agents and proposed to (worker
``rook``; :mod:`rook.hub.trust`, docs/design/trust.md).

* ``trust.get`` (read): the summary agents get in context, and the map
  itself (entries, exceptions, the borrowed-state ledger, pending proposals).
* ``trust.check`` (read): the ruling for an action (a cap on a worker, or a
  repository and branch) with its citation, as the hub would rule it.
* ``trust.history`` (read): who changed what, when, in whose words.
* ``trust.propose`` (write): any agent asks the person for a change; it waits
  on the dashboard's Trust page. Nothing changes until the person says so. A
  change that only makes the map stricter is refused (no ratchet: only the
  person adds guardrails); ``batch`` carries several changes as one.
* ``trust.tighten`` (write): a person makes the map stricter through a cap
  (raise a level, add a guarded or sacred entry, make unlisted stricter, end
  an exception early). Agents, roles and the house agent are refused.

The ``rook_trust`` MCP tool routes to all five.
"""
from __future__ import annotations

import json

from ....core.plugin import Plugin, capability, place
from ...trust import Actor, TrustError, store_for

ACTIONS = ("summary", "get", "check", "history", "propose", "tighten")
ERRORS = (ValueError, KeyError, TypeError, PermissionError, LookupError)
TIGHTEN_OPS = ("set_entry", "set_unlisted", "end_exception")
#: No ratchet (change-management-design-20261025): agents and huddle roles
#: never add or tighten guardrails; only the person does.
NO_RATCHET = ("only the person adds or tightens guardrails (Trust > Trust map, or by voice); "
              "agents and roles never do. If you think one is needed, say so in your report")


def _caller():
    from ...authz import current_principal
    return current_principal.get()


def node_store(node):
    """The trust map this hub process enforces (the authorizer's), else the
    plugin's own view of the same file."""
    authz = getattr(getattr(node, "client", None), "authz", None)
    st = getattr(getattr(authz, "approvals", None), "trust", None)
    if st is not None:
        return st
    plugin = node.plugin("trust") if node is not None and hasattr(node, "plugin") else None
    return getattr(plugin, "store", None)


def _advanced(node) -> bool:
    from ..security import for_node
    return for_node(node)


def summary_text(node) -> str:
    """The trust summary for an agent's context, or "" without a map."""
    try:
        st = node_store(node)
        return st.summary(advanced=_advanced(node)) if st is not None else ""
    except Exception:  # noqa: BLE001 - context is best effort
        return ""


class Trust(Plugin):
    NAMESPACE = "trust"
    NAME = "trust"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    GUIDANCE = {
        "tool:rook_trust": "",
        "cap:trust.": "trust.get, trust.check {cap, worker} | {repo, branch}, trust.history, "
                      "trust.propose {change {op, data}, why}, trust.tighten: see rook_trust.",
    }
    SKILL = ("### trust map\n"
             "The hub enforces a trust map: a `sacred` host, project, repo or resource changes "
             "only after the person says so (you may read, propose and prepare); `guarded` and "
             "`ship-it` entries just go unless the advanced security plugin is on. A held call "
             "cites its entry. `rook_trust(action=\"summary\")` before acting on something you "
             "are unsure of; `action=\"check\", data={\"cap\":…, \"worker\":…}` gives the ruling. "
             "Never try to get around a trust hold. To change the map, "
             "`action=\"propose\", data={\"change\": {\"op\": \"set_entry\", \"data\": {...}}, "
             "\"why\": …}`: the person decides on the dashboard. Only the person changes it; a "
             "proposal that only makes it stricter is refused.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None
        self._store = None

    def bind_host(self, node) -> None:
        self._node = node

    @property
    def store(self):
        if self._store is None:
            authz = getattr(getattr(self._node, "client", None), "authz", None)
            self._store = getattr(getattr(authz, "approvals", None), "trust", None)
            if self._store is None:
                from ...trust import default_path
                self._store = store_for(default_path())
        return self._store

    # -- reads -----------------------------------------------------------------
    @capability("get", risk="read", scope="multi-user")
    def get(self, section: str = "") -> dict:
        """The trust map: summary (what agents get in context) and the map; section: entries|exceptions|baseline|ledger|proposals|notices for just that part."""
        doc = self.store.public()
        if section:
            if section not in ("entries", "exceptions", "baseline", "ledger", "proposals",
                               "notices"):
                raise ValueError("section: entries, exceptions, baseline, ledger, proposals or "
                                 "notices")
            return {"version": doc["version"], section: doc.get(section) or []}
        return {"summary": self.store.summary(advanced=_advanced(self._node)), "map": doc}

    @capability("check", risk="read", scope="multi-user")
    def check(self, cap: str = "", worker: str = "", repo: str = "", branch: str = "",
              project: str = "", tier: str = "") -> dict:
        """The ruling for an action, with its citation: a cap on a worker (its tier is the band's), or repo + branch (a push or merge: tier admin) or project."""
        from ....core.authz import effective_tier, norm_tier
        entry = self._entry(worker) if worker else None
        on_hub = bool(worker) and worker.strip().lower() == "rook"
        t = norm_tier(tier) or (effective_tier(cap, ((entry or {}).get("tiers") or {}).get(cap))
                                if cap else "admin")
        r = self.store.rule(cap, t, (), worker=("rook" if on_hub else str((entry or {}).get(
            "name") or worker)), on_hub=on_hub, repo=repo, branch=branch, project=project)
        out = {**r.public(), "cap_tier": t}
        if out["tier"] == "ask" and not _advanced(self._node):
            out.update(tier="go", note="guarded and unlisted ask only with advanced security on")
        return out

    def _entry(self, worker: str) -> dict | None:
        roster = dict(getattr(getattr(self._node, "client", None), "workers", {}) or {})
        w = worker.strip().lower()
        for wid, e in roster.items():
            if wid.lower() == w or str(e.get("name") or "").lower() == w:
                return e
        return None

    @capability("history", risk="read", scope="multi-user")
    def history(self, limit: int = 30, entry: str = "") -> dict:
        """Changes to the trust map, newest first: who, when, the person's words, before and after (entry: one entry's)."""
        return {"history": self.store.history(max(1, min(int(limit or 30), 200)), entry)}

    # -- writes ----------------------------------------------------------------
    @capability("propose", risk="write", scope="multi-user")
    def propose(self, change: dict | None = None, why: str = "") -> dict:
        """Ask the person to change the trust map: change {op, data} (set_entry, delete_entry, set_unlisted, add_exception, end_exception, add_lease, close_lease, or batch {changes: [...]}) and why. It waits on the dashboard's Trust page. A change that only makes the map stricter is refused: only the person adds guardrails."""
        p = _caller()
        who = (getattr(p, "label", "") or getattr(p, "id", "")) if p is not None else "unknown"
        if getattr(p, "kind", "") != "human":
            from ...trust import describe_change
            if not describe_change(self.store.current(), change or {}).get("loosens", True):
                raise PermissionError(NO_RATCHET)
        try:
            prop = self.store.propose(change or {}, by=who, principal=getattr(p, "id", "") or "",
                                      why=why)
        except TrustError as e:
            raise ValueError(str(e)) from None
        return {"proposal": prop["id"], "state": prop["state"],
                "note": "waiting for the person on the dashboard's Trust page"}

    @capability("tighten", risk="write", scope="multi-user")
    def tighten(self, op: str, data: dict | None = None, words: str = "",
                session: str = "") -> dict:
        """Make the trust map stricter (a person only; agents, roles and the house agent are refused): op set_entry (new guarded/sacred entry, or a higher level), set_unlisted (stricter), end_exception; words: the person's own words that asked for it. Logged and shown to the person."""
        p = _caller()
        if op not in TIGHTEN_OPS:
            raise ValueError("tighten: op one of " + ", ".join(TIGHTEN_OPS))
        if p is not None and getattr(p, "kind", "") == "human" and getattr(p, "verified", False):
            kind = "house"   # a person through a cap: tighten-only too; loosening is the dashboard's
        else:
            raise PermissionError(NO_RATCHET)
        who = getattr(p, "label", "") or getattr(p, "id", "")
        try:
            return self.store.change(op, data or {}, Actor(kind, who,
                                                           principal=getattr(p, "id", "") or "",
                                                           words=words, session=session))
        except TrustError as e:
            raise ValueError(str(e)) from None

    # -- MCP -------------------------------------------------------------------
    def mcp_tools(self, invoke):
        async def rook_trust(action: str = "summary", data: dict | None = None) -> str:
            """Hub-enforced trust map: sacred asks the person first; the rest go.
            summary|get|history|check data {cap, worker}|{repo, branch}|propose data
            {change {op, data}, why} (never stricter)."""
            d = data if isinstance(data, dict) else {}
            try:
                if action == "summary":
                    result = (await invoke("trust.get", {}))["summary"]
                elif action == "get":
                    result = await invoke("trust.get", {k: d[k] for k in ("section",) if k in d})
                elif action == "check":
                    result = await invoke("trust.check", {
                        k: d[k] for k in ("cap", "worker", "repo", "branch", "project", "tier")
                        if k in d})
                elif action == "history":
                    result = await invoke("trust.history", {k: d[k] for k in ("limit", "entry")
                                                            if k in d})
                elif action == "propose":
                    result = await invoke("trust.propose", {"change": d.get("change"),
                                                            "why": d.get("why") or ""})
                elif action == "tighten":
                    result = await invoke("trust.tighten", {
                        "op": d.get("op") or "", "data": d.get("data") or {},
                        "words": d.get("words") or "", "session": d.get("session") or ""})
                else:
                    raise ValueError("action must be one of " + ", ".join(ACTIONS))
                return json.dumps({"ok": True, "result": result}, separators=(",", ":"),
                                  ensure_ascii=False, default=str)
            except ERRORS as error:
                return json.dumps({"ok": False, "error": str(error),
                                   "code": type(error).__name__}, separators=(",", ":"))
        return [rook_trust]


PLUGIN = Trust
