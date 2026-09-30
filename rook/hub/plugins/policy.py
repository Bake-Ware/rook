"""``policy.*`` and ``grants.*``: the permission policy as hub caps.

Reach them with ``rook_call(worker="rook", cap="policy.explain", ...)``; they
declare no dedicated MCP tool, so ``tools/list`` stays within budget. The
dashboard's ``/api/policy`` endpoints use the same store.

* ``policy.explain`` (read): the decision for a principal, cap and worker,
  with the winning rule and the runner-up.
* ``policy.get`` (read, sensitive): the current document, its revision,
  source, mode and lint.
* ``policy.set`` (admin): replace the document. Band owners and operator
  tokens only, whatever the policy mode; refuses a document that leaves no
  principal with admin on ``rook``; journaled as ``audit.policy``.
* ``grants.revocations`` (read): the hub's op-key id and grant status.
"""

from __future__ import annotations

from ...core.plugin import Plugin, capability, place


def _principal(spec: str, role: str | None):
    from ..policy import Principal
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("principal= is required (e.g. token:<agent_id>, human:owner, role:agent)")
    if spec.startswith("role:"):
        r = spec[5:]
        return Principal(f"token:explain-{r}", "token", r)
    if spec in ("human:owner", "human:member"):
        r = spec.split(":", 1)[1]
        return Principal(f"human:explain-{r}", "human", r, (spec,))
    if spec == "unverified":
        return Principal("unverified", "unverified", verified=False)
    kind = spec.split(":", 1)[0]
    default_role = {"token": "agent", "system": "system", "integration": "integration"}.get(kind, "")
    groups = (f"human:{role}",) if kind == "human" and role else ()
    return Principal(spec, kind, role or default_role, groups)


class PolicyPlugin(Plugin):
    NAMESPACE = "policy"
    NAME = "hub-policy"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### policy\n"
             "`rook_call(cap=\"policy.explain\", worker=\"rook\", args={\"principal\": "
             "\"role:agent\", \"cap\": \"shell.exec\", \"worker\": \"<name>\"})` shows whether "
             "a call would be allowed and which rule decides it. `policy.get` returns the "
             "document; changing it (`policy.set`) is for band owners and operator tokens.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None

    def bind_host(self, node) -> None:
        self._node = node

    def _authz(self):
        client = getattr(self._node, "client", None)
        authz = getattr(client, "authz", None)
        if authz is None:
            raise RuntimeError("permissions are not configured on this hub")
        return authz

    def _target(self, worker: str | None):
        from ..authz import target_from_entry
        node = self._node
        if not worker:
            return target_from_entry(None, None)
        if worker.lower() == "rook" and node is not None:
            return target_from_entry(node.worker_id, node.entry(), local=True)
        roster = getattr(node.client, "workers", {}) if node is not None else {}
        if worker in roster:
            return target_from_entry(worker, roster[worker])
        named = [wid for wid, w in roster.items() if (w.get("name") or "").lower() == worker.lower()]
        if len(named) == 1:
            return target_from_entry(named[0], roster[named[0]])
        # Not live: evaluate against the name alone.
        return target_from_entry(worker, {"name": worker})

    @capability("explain", risk="read")
    def explain(self, principal: str, cap: str, worker: str | None = None,
                role: str | None = None) -> dict:
        """Would this principal be allowed to call ``cap`` on ``worker``?

        ``principal`` is ``token:<agent_id>``, ``human:<user_id>``,
        ``integration:<name>``, ``unverified``, or a probe such as
        ``role:agent`` / ``human:owner``. Returns the decision, tier, winning
        rule, runner-up and policy revision."""
        policy = self._authz().store.current()
        d = policy.evaluate([_principal(principal, role)], cap, self._target(worker))
        return d.explain()

    @capability("get", risk="read", tags=("sensitive",))
    def get(self) -> dict:
        """The current policy document with its revision, source, mode, lint
        and last load error (if the file on disk is invalid)."""
        store = self._authz().store
        policy = store.current()
        return {"rev": policy.rev, "mode": policy.mode, "source": store.source,
                "error": store.error, "lint": policy.lint(), "policy": policy.doc}

    @capability("set", risk="admin")
    def set(self, policy: dict, note: str = "") -> dict:
        """Replace the policy document (band owners and operator tokens only).

        The new revision is the old one + 1. Refused if it fails validation or
        would leave no principal holding admin on ``rook``. Journaled as
        ``audit.policy`` with the old/new revision and a diff summary."""
        from ..authz import current_principal, require_hub_admin
        from ..policy import summarize_diff
        refused = require_hub_admin("policy.set")
        if refused:
            return {"ok": False, "error": refused}
        authz = self._authz()
        old = authz.store.current()
        new = authz.store.save(policy)
        p = current_principal.get()
        authz.record_event("audit.policy", "rook", {
            "actor": p.id if p else None, "old_rev": old.rev, "new_rev": new.rev,
            "diff": summarize_diff(old.doc, new.doc), "note": note[:200]})
        return {"ok": True, "rev": new.rev, "mode": new.mode, "lint": new.lint()}

    @capability("status", risk="read")
    def status(self) -> dict:
        """Hub permission status: policy mode/revision, whether calls carry
        tickets (root key present), the op-key id and the trust anchor id."""
        from ...core.authz import key_id
        authz = self._authz()
        signer = authz.signer
        enabled = bool(getattr(signer, "enabled", False))
        policy = authz.store.current()
        return {"mode": policy.mode, "rev": policy.rev, "source": authz.store.source,
                "tickets": enabled, "op_kid": getattr(signer, "kid", None) if enabled else None,
                "anchor": key_id(signer.anchor) if enabled else None}


PLUGIN = PolicyPlugin
