"""Where the hub keeps its state.

``ROOK_DATA_DIR`` puts every hub file (setup.json, enrollment.db, the MCP
token/journal/chat/vault stores) in one directory, which is what the Docker
image and the quickstart use. Unset, each store keeps its historical default,
so existing deployments are unaffected. More specific variables
(``ROOK_SETUP_PATH``, ``ROOK_MCP_PERSIST``, ``ROOK_CHAT_DB`` …) still win.
"""

from __future__ import annotations

import os


def data_path(name: str, legacy: str) -> str:
    """``$ROOK_DATA_DIR/<name>`` when ROOK_DATA_DIR is set, else ``legacy``."""
    base = os.environ.get("ROOK_DATA_DIR", "").strip()
    if not base:
        return legacy
    return os.path.join(os.path.expanduser(base), name)
