"""The migration's way into the 0.5 hub's vault (:mod:`rook.core2.hub.vault`):
``<data>/vault.db``, the format the hub reads. Values go only into it; a
name already there keeps its value."""
from __future__ import annotations

from pathlib import Path

from rook.core2.hub.vault import Vault


def put(data: Path, secrets: list[dict]) -> list[str]:
    """Store ``[{name, value, description, created, set_by}]``. Returns the names written."""
    return Vault(data).unlock().put(secrets)


def names(data: Path) -> list[str]:
    return Vault(data).names()


def get(data: Path, name: str) -> str:
    return Vault(data).unlock().get(name)
