"""Where the hub keeps its state, and where a worker keeps its own.

``ROOK_DATA_DIR`` puts every hub file (setup.json, enrollment.db, the MCP
token/journal/chat/vault stores) in one directory, which is what the Docker
image and the quickstart use. Unset, each store keeps its historical default,
so existing deployments are unaffected. More specific variables
(``ROOK_SETUP_PATH``, ``ROOK_MCP_PERSIST``, ``ROOK_CHAT_DB`` …) still win.

The worker's directories (``ROOK_WORKER_HOME``) live in
:mod:`rook.core.paths`, because the worker bundle ships ``rook.core`` but not
this module; they are re-exported here for hub-side code.
"""

from __future__ import annotations

import os

from .core.paths import (  # noqa: F401  (re-exported)
    WORKER_DIRNAME, worker_install_dir, worker_state_dir, worker_state_path,
)


def data_path(name: str, legacy: str) -> str:
    """``$ROOK_DATA_DIR/<name>`` when ROOK_DATA_DIR is set, else ``legacy``."""
    base = os.environ.get("ROOK_DATA_DIR", "").strip()
    if not base:
        return legacy
    return os.path.join(os.path.expanduser(base), name)
