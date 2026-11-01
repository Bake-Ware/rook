"""The 0.5 names the move writes and checks, read from the plugins'
manifests in this checkout, never spelled here: a plugin's namespace and
record names may change (castling-caps rule 5, "Singular namespace = plugin
name"; drift #33), and the move must follow whatever the hub loads.

    record("jobs")            -> "job.job" (the plugin was "jobs" before drift #33)
    record("agents", "memory") -> "agent.memory" / "agent.memory"
    cap("jobs", "list")       -> the record's list cap: "job.list" / "job.list"

Folders are the plugin folders under ``rook/plugins`` (those don't change).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from rook.kit.manifest import load

PLUGINS = Path(__file__).resolve().parents[3] / "rook" / "plugins"


@lru_cache(maxsize=None)
def manifest(folder: str):
    return load(PLUGINS / folder)


def plugin(folder: str) -> str:
    return manifest(folder).data["plugin"]


def _key(folder: str, key: str | None) -> str:
    recs = list((manifest(folder).data.get("records") or {}))
    if key is None:                    # the plugin's main record: named like the plugin, else its first
        name = plugin(folder)
        return name if name in recs else recs[0]
    if key not in recs:
        raise KeyError(f"plugin folder {folder} declares no record {key}")
    return key


def record(folder: str, key: str | None = None) -> str:
    """A record type's full name (``<plugin>.<record>``)."""
    return f"{plugin(folder)}.{_key(folder, key)}"


def cap(folder: str, verb: str, key: str | None = None) -> str:
    """A record cap's name: ``<plugin>.<verb>`` for the record named like
    the plugin, else ``<plugin>.<record>.<verb>`` (the kit's rule)."""
    name, rec = plugin(folder), _key(folder, key)
    return f"{name}.{verb}" if rec == name else f"{name}.{rec}.{verb}"
