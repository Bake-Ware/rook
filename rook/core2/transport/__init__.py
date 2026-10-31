"""Core v2 transport: hub and worker messages over Telesthete v0.3.0
(spec 2.1). :class:`Transport` (conn.py) carries the messages; a link
(links.py) carries one band's bytes, over a real relay
(:class:`TelestheteLink`) or in memory (:class:`LoopbackRelay`).

    t = from_config({"node_id": wid, "bands": [{"psk_env": "ROOK_PSK", "relay": "relay:7474"},
                                                {"psk": psk2, "ws_url": "wss://hub/band"}]},
                    hello=announce, beat=heartbeat, on_lost=lambda band: gate.tick())
    await t.start(on_message)          # on_message(band, msg)
"""
from __future__ import annotations

import os
from typing import Any

from .conn import (BEAT_SECS, DROP_THRESHOLD, FEATURES, LOST_SECS, Peer, Transport,
                   TransportError)
from .links import LoopbackLink, LoopbackRelay, TelestheteLink, band_of

__all__ = ["BEAT_SECS", "DROP_THRESHOLD", "FEATURES", "LOST_SECS", "LoopbackLink",
           "LoopbackRelay", "Peer", "TelestheteLink", "Transport", "TransportError",
           "band_of", "from_config", "hub_transport", "link_from", "worker_transport"]


def link_from(spec: dict) -> TelestheteLink:
    """A band link from ``{psk | psk_env, relay | ws_url, keepalive?, pace?}``. The PSK is the
    resolved value (or an environment variable naming it), never a vault
    reference: whoever reads the config resolves those first."""
    psk = spec.get("psk") or (os.environ.get(spec["psk_env"]) if spec.get("psk_env") else None)
    if not psk:
        raise ValueError("a band needs psk or a set psk_env")
    return TelestheteLink(psk, relay=spec.get("relay"), ws_url=spec.get("ws_url"),
                          keepalive=spec.get("keepalive"), pace=spec.get("pace"))


def from_config(cfg: dict, **kw: Any) -> Transport:
    """``cfg``: ``{"node_id": <32 hex>, "bands": [<link spec>, ...]}``;
    keyword arguments go to :class:`Transport`."""
    return Transport(cfg["node_id"], [link_from(b) for b in cfg.get("bands") or ()], **kw)


hub_transport = worker_transport = from_config     # the same transport on both sides
