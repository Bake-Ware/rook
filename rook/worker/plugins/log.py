"""log.* — read this worker's audit trail back over the band.

Every capability dispatch is recorded locally by :mod:`rook.worker.audit`,
tagged with the caller identity from the request envelope. These caps expose
that trail so an agent can answer "who did what, when" on this machine — the
forensic half of the identity story. Reading is cheap and side-effect-free;
the log itself is written by the worker core, not here.
"""

from __future__ import annotations

from ..plugin import Plugin, capability
from .. import audit


class LogPlugin(Plugin):
    NAMESPACE = "log"

    @capability("audit")
    def _audit(self, limit: int = 50, cap_prefix: str | None = None,
               identity: str | None = None, since: float | None = None) -> dict:
        """Return recent audit entries for this worker (newest last).

        Each entry: ``{ts, identity, cap, ok, args, id?, target?, error?}``.
        Filter with ``cap_prefix`` (e.g. ``"shell."``), exact ``identity``
        (e.g. ``"agent:claude_kaiju"``), and/or ``since`` (unix seconds).
        ``limit`` caps how many are returned. This is the trail behind
        "who renamed this machine and when".
        """
        entries = audit.tail(limit=limit, cap_prefix=cap_prefix,
                             identity=identity, since=since)
        return {"count": len(entries), "entries": entries}

    @capability("tail")
    def _tail(self, limit: int = 20) -> dict:
        """The last ``limit`` audit entries, unfiltered (newest last)."""
        entries = audit.tail(limit=limit)
        return {"count": len(entries), "entries": entries}


PLUGIN = LogPlugin
