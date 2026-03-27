"""Rook network configuration.

Determines whether this machine runs as the hub (local mode) or
connects to a remote hub (client mode).

Config file: ~/.rook/net.json
{
    "mode": "hub" | "client",
    "hub_url": "ws://rook.bake.systems:7006/band",  // for client mode
    "psk": "rook-hub-2026",
    "udp_port": 9999,
    "ws_port": 7006
}

If no config exists, defaults to local mode (graph on this machine).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("rook.net")

CONFIG_PATH = Path.home() / ".rook" / "net.json"

DEFAULT_CONFIG = {
    "mode": "local",  # local = graph on this machine, hub = run the hub, client = connect to hub
    "hub_url": "ws://localhost:7006/band",
    "psk": "rook-hub-2026",
    "udp_port": 9999,
    "ws_port": 7006,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            return {**DEFAULT_CONFIG, **cfg}
        except Exception as e:
            log.warning("Failed to load net config: %s", e)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def is_hub() -> bool:
    return load_config()["mode"] == "hub"


def is_client() -> bool:
    return load_config()["mode"] == "client"


def is_local() -> bool:
    return load_config()["mode"] == "local"
