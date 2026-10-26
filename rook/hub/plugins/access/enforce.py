"""Hub-side enforcement of user access grants.

Called from Authorizer.check() to evaluate grants for human principals with
stored user access records. Owners and unauthenticated principals fall through
to policy evaluation.
"""
from __future__ import annotations

import fnmatch
import logging
from typing import Optional

from .model import AccessGrantStore

log = logging.getLogger("rook.hub.plugins.access.enforce")


def check_user_grant(principal_id: str, cap: str, target_id: Optional[str],
                     resource_type: str, grant_store: AccessGrantStore) -> Optional[str]:
    """Check if a user principal has a grant for the resource and cap.

    Args:
        principal_id: human:<user_id> format
        cap: capability name (e.g. shell.exec)
        target_id: machine/worker id for targeted caps, or None for broadcast
        resource_type: band|machine|cap|knowledge_area
        grant_store: the AccessGrantStore instance

    Returns:
        None if allowed (grant matches), or error message if denied/no grant.
    """
    # Extract user_id from principal_id (human:<id> format)
    if not principal_id.startswith("human:"):
        return None  # Not a human user, fall through to policy

    user_id = principal_id.split(":", 1)[1]

    # Get active grants for this user
    grants = grant_store.list_grants(user_id, active_only=True)
    if not grants:
        return f"denied: no grants for {user_id}"

    # Check for matching grant by resource type
    matching_grant = None
    for grant in grants:
        # For machine resources, target_id must match
        if resource_type == "machine":
            if grant.resource_type == "machine" and grant.resource_id == target_id:
                matching_grant = grant
                break
            if grant.resource_type == "band" and target_id:
                # Band grant gives access to all machines in band (would need to check band membership)
                # For now, machine-specific grants are required
                pass

        # For cap resources, match the cap glob pattern
        elif resource_type == "cap":
            if grant.resource_type == "cap" and fnmatch.fnmatch(cap, grant.resource_id):
                matching_grant = grant
                break

        # For knowledge_area resources
        elif resource_type == "knowledge_area":
            if grant.resource_type == "knowledge_area" and grant.resource_id == target_id:
                matching_grant = grant
                break

        # For band resources
        elif resource_type == "band":
            if grant.resource_type == "band" and grant.resource_id == target_id:
                matching_grant = grant
                break

    if not matching_grant:
        return f"denied: no grant for {resource_type}:{target_id or cap}"

    # Grant exists; policy evaluation will handle tier ceiling if present
    return None
