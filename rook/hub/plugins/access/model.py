"""User access grants data model and store operations."""
from __future__ import annotations

import dataclasses
import json
import logging
import time
import uuid
from typing import Optional

log = logging.getLogger("rook.hub.plugins.access.model")


@dataclasses.dataclass(frozen=True)
class AccessGrant:
    """A user's permission for a resource, up to an optional tier ceiling."""
    id: str
    user_id: str
    resource_type: str  # band | machine | cap | knowledge_area
    resource_id: str    # band_id, machine id, cap glob, or knowledge area slug
    tier_ceiling: Optional[str] = None  # read|write|exec|admin, or None for full access
    created: float = 0.0
    created_by: str = ""
    quoted_words: str = ""
    revoked: Optional[float] = None
    revoked_by: Optional[str] = None

    def is_active(self) -> bool:
        return self.revoked is None

    def to_dict(self) -> dict:
        """Serializable dict for history table."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "tier_ceiling": self.tier_ceiling,
            "created": self.created,
            "created_by": self.created_by,
            "quoted_words": self.quoted_words,
            "revoked": self.revoked,
            "revoked_by": self.revoked_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AccessGrant:
        """Load from history dict."""
        return cls(**data)

    @classmethod
    def from_row(cls, row) -> AccessGrant:
        """Load from database row."""
        return cls(
            id=row['id'],
            user_id=row['user_id'],
            resource_type=row['resource_type'],
            resource_id=row['resource_id'],
            tier_ceiling=row['tier_ceiling'],
            created=row['created'],
            created_by=row['created_by'],
            quoted_words=row['quoted_words'],
            revoked=row['revoked'],
            revoked_by=row['revoked_by'],
        )


class AccessGrantStore:
    """Operations on user access grants in enrollment.db."""

    def __init__(self, db_fn):
        """db_fn: callable that returns a transaction context (like AccountStore.db)."""
        self.db_fn = db_fn

    def create_grant(self, user_id: str, resource_type: str, resource_id: str,
                     tier_ceiling: Optional[str] = None, created_by: str = "",
                     quoted_words: str = "") -> AccessGrant:
        """Create a new grant. Returns the grant."""
        grant_id = str(uuid.uuid4())
        now = time.time()

        with self.db_fn() as db:
            db.execute(
                """INSERT INTO user_access_grants
                   (id, user_id, resource_type, resource_id, tier_ceiling, created, created_by, quoted_words)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (grant_id, user_id, resource_type, resource_id, tier_ceiling, now, created_by, quoted_words)
            )
            self._record_grant_change(db, grant_id, "created")

        grant = AccessGrant(
            id=grant_id, user_id=user_id, resource_type=resource_type,
            resource_id=resource_id, tier_ceiling=tier_ceiling,
            created=now, created_by=created_by, quoted_words=quoted_words
        )
        return grant

    def list_grants(self, user_id: str = None, active_only: bool = True) -> list[AccessGrant]:
        """List grants. If user_id is None, list all grants."""
        with self.db_fn() as db:
            query = "SELECT * FROM user_access_grants"
            params = []
            if user_id:
                query += " WHERE user_id = ?"
                params.append(user_id)
            if active_only:
                if user_id:
                    query += " AND revoked IS NULL"
                else:
                    query += " WHERE revoked IS NULL"
            rows = db.execute(query, params).fetchall()
            return [AccessGrant.from_row(row) for row in rows]

    def get_grant(self, grant_id: str) -> Optional[AccessGrant]:
        """Get a single grant by ID."""
        with self.db_fn() as db:
            row = db.execute("SELECT * FROM user_access_grants WHERE id = ?", (grant_id,)).fetchone()
            return AccessGrant.from_row(row) if row else None

    def revoke_grant(self, grant_id: str, revoked_by: str = "") -> Optional[AccessGrant]:
        """Revoke a grant. Returns the revoked grant or None if not found."""
        now = time.time()
        with self.db_fn() as db:
            row = db.execute("SELECT * FROM user_access_grants WHERE id = ?", (grant_id,)).fetchone()
            if not row:
                return None

            db.execute(
                "UPDATE user_access_grants SET revoked = ?, revoked_by = ? WHERE id = ?",
                (now, revoked_by, grant_id)
            )
            self._record_grant_change(db, grant_id, "revoked")

        grant = AccessGrant.from_row(row)
        return grant._replace(revoked=now, revoked_by=revoked_by)

    def _record_grant_change(self, db, grant_id: str, action: str):
        """Record a change in the history table."""
        row = db.execute("SELECT * FROM user_access_grants WHERE id = ?", (grant_id,)).fetchone()
        if row:
            doc = json.dumps(AccessGrant.from_row(row).to_dict())
        else:
            doc = None

        db.execute(
            """INSERT INTO user_access_grants_history (ref, doc, ts, actor, note)
               VALUES (?, ?, ?, ?, ?)""",
            (grant_id, doc, time.time(), "", action)
        )
