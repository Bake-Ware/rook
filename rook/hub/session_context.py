"""Hub-side session context and brief receipt (design item 12).

Agents receive a session brief on first contact or when context changes (compaction,
user/band changes). The brief includes a receipt (hash) so the agent can echo it back;
the hub omits the brief on subsequent calls when the receipt matches, avoiding token
duplication.

The brief is a notice that travels with rook_call replies, following the _task/_unread_chat
pattern via the Notices.fresh mechanism. Non-echoing clients (phone, browser) are tracked
per MCP session: sent once and marked silent, not spammed on every call.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("rook.hub.session_context")


@dataclass(frozen=True)
class SessionBrief:
    """The session context brief sent to an agent or MCP client."""

    session_id: str
    user: str
    timestamp: float
    band_id: str | None = None
    hub_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict."""
        return {
            "session_id": self.session_id,
            "user": self.user,
            "timestamp": self.timestamp,
            **({"band_id": self.band_id} if self.band_id else {}),
            **({"hub_version": self.hub_version} if self.hub_version else {}),
        }

    def to_json(self) -> str:
        """Serialize to JSON for hashing."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    def receipt(self) -> str:
        """Compute a deterministic receipt (hash) of this brief.

        Same brief content = same receipt, so changes invalidate it.
        """
        return hashlib.sha256(self.to_json().encode()).hexdigest()


class EchoState:
    """Track whether a client echoed back a receipt in this MCP session.

    States:
    - never_echoed: first call(s) send the brief, wait for echo
    - silent: client never echoed; stop sending brief to avoid spam
    - echoed: client echoed at least once; require matching receipt to skip resend
    """

    def __init__(self) -> None:
        self.state = "never_echoed"
        self.last_receipt: str | None = None

    def mark_echoed(self, receipt: str) -> None:
        """Record that the client echoed this receipt."""
        if self.state == "never_echoed":
            self.state = "echoed"
        self.last_receipt = receipt

    def mark_silent(self) -> None:
        """Record that a call arrived without an echo, after one should have."""
        if self.state == "never_echoed":
            self.state = "silent"

    def should_send_brief(self, brief: SessionBrief) -> bool:
        """Decide whether to send the brief in the next reply.

        Returns True if:
        - state is never_echoed (first call, always send to try for an echo)
        - state is echoed but receipt is missing or mismatched
        - brief changed (receipt changed)

        Returns False if:
        - state is silent (client never echoed, stop sending)
        - state is echoed and receipt matches
        """
        if self.state == "silent":
            return False
        if self.state == "echoed" and self.last_receipt:
            return brief.receipt() != self.last_receipt
        return True


def make_notice(brief: SessionBrief) -> dict[str, Any]:
    """Build the _session_context notice for a reply.

    Returns a dict with the brief and receipt, compact.
    """
    return {
        "brief": brief.to_dict(),
        "receipt": brief.receipt(),
    }
