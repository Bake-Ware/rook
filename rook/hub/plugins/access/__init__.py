"""Access control: users, owners, fine-grained grants per user.

Design: [[users-access-trust-map-and-agent-sessions-bake-s-design-decisions-2026-10-26-voi]]

This plugin provides:
- User management (owners and members with grants)
- Fine-grained access grants (band, machine, cap, knowledge_area)
- Hub-enforced access control
"""
from __future__ import annotations

import logging
from typing import Any

from rook.core.plugin import Plugin, capability, place
from rook.hub.authz import require_hub_admin, current_principal
from rook.remote.accounts import AccountStore
from .model import AccessGrantStore

log = logging.getLogger("rook.hub.plugins.access")


class Access(Plugin):
    """Access control plugin: manages user grants and enforces hub-side access."""

    NAMESPACE = "users"
    NAME = "access"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    MIGRATIONS = "migrations"
    SETTINGS = {}

    def __init__(self, ctx):
        self.ctx = ctx
        self.account_store = AccountStore(ctx.enrollment)
        self.grant_store = AccessGrantStore(self.account_store.db)

    @capability("list", risk="read", tool=True, fields="*")
    def list_users(self, **_) -> dict[str, Any]:
        """List all users with their access grants.

        Returns: {users: [{id, name, email, is_owner, grants: [...]}, ...]}
        """
        refusal = require_hub_admin("users.list")
        if refusal:
            return {"error": refusal}

        with self.account_store.db() as db:
            users_rows = db.execute("SELECT id, name, email, admin FROM users").fetchall()
            users = []
            for user_row in users_rows:
                user_id = user_row['id']
                grants = self.grant_store.list_grants(user_id, active_only=True)
                users.append({
                    "id": user_id,
                    "name": user_row['name'],
                    "email": user_row['email'],
                    "is_owner": bool(user_row['admin']),
                    "grants": [g.to_dict() for g in grants],
                })
            return {"users": users}

    @capability("grant.add", risk="write", tool=True, fields="*")
    def add_grant(self, user_id: str, resource_type: str, resource_id: str,
                 tier_ceiling: str = None, why: str = "", **_) -> dict[str, Any]:
        """Add a grant to a user.

        Args:
            user_id: the user's ID
            resource_type: band|machine|cap|knowledge_area
            resource_id: the resource identifier (band_id, machine id, cap glob, or slug)
            tier_ceiling: read|write|exec|admin, or None for full access
            why: quoted words explaining why this grant was given

        Returns: {grant: {...}} or {error: ...}
        """
        refusal = require_hub_admin("users.grant.add")
        if refusal:
            return {"error": refusal}

        principal = current_principal.get()
        actor = principal.id if principal else "system:hub"

        try:
            grant = self.grant_store.create_grant(
                user_id=user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                tier_ceiling=tier_ceiling,
                created_by=actor,
                quoted_words=why,
            )
            return {"grant": grant.to_dict()}
        except Exception as e:
            log.exception("Failed to add grant")
            return {"error": str(e)}

    @capability("grant.revoke", risk="write", tool=True, fields="*")
    def revoke_grant(self, grant_id: str, **_) -> dict[str, Any]:
        """Revoke a grant.

        Args:
            grant_id: the grant's ID

        Returns: {grant: {...}} or {error: ...}
        """
        refusal = require_hub_admin("users.grant.revoke")
        if refusal:
            return {"error": refusal}

        principal = current_principal.get()
        actor = principal.id if principal else "system:hub"

        try:
            grant = self.grant_store.revoke_grant(grant_id, revoked_by=actor)
            if not grant:
                return {"error": "Grant not found"}
            return {"grant": grant.to_dict()}
        except Exception as e:
            log.exception("Failed to revoke grant")
            return {"error": str(e)}

    @capability("explain", risk="read", tool=True, fields="*")
    def explain_access(self, user_id: str = None, principal_id: str = None, **_) -> dict[str, Any]:
        """Explain what a user or principal can access.

        Args:
            user_id: the user's ID (for humans)
            principal_id: the principal ID to explain (human:*, token:*, etc.)

        Returns: {access: {bands: [...], machines: [...], caps: [...], knowledge_areas: [...]}}
        """
        if not user_id and not principal_id:
            principal = current_principal.get()
            if principal and principal.kind == "human":
                user_id = principal.id.split(":", 1)[-1] if ":" in principal.id else principal.id
            else:
                return {"error": "No user context"}

        try:
            grants = self.grant_store.list_grants(user_id, active_only=True)

            access = {
                "bands": [],
                "machines": [],
                "caps": [],
                "knowledge_areas": [],
            }

            for grant in grants:
                entry = {
                    "id": grant.resource_id,
                    "tier": grant.tier_ceiling or "all",
                }
                if grant.resource_type == "band":
                    access["bands"].append(entry)
                elif grant.resource_type == "machine":
                    access["machines"].append(entry)
                elif grant.resource_type == "cap":
                    access["caps"].append(entry)
                elif grant.resource_type == "knowledge_area":
                    access["knowledge_areas"].append(entry)

            return {"access": access}
        except Exception as e:
            log.exception("Failed to explain access")
            return {"error": str(e)}


PLUGIN = Access
