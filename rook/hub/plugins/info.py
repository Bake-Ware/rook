"""``hub.*``: what the hub is running. The reference hub plugin.

Exercises the contract end to end: hub placement (``is_hub``,
``run="one"``), caps with a risk tier, core-enforced ``limit``/``fields``
(``hub.plugins``), a settings schema with an env override, guidance slots and
a skill fragment. It declares no dedicated MCP tool (``tool=True``): every
connect pays for ``tools/list``, and ``rook_call(worker="rook")`` reaches it.
"""

from __future__ import annotations

import time

from ...core.plugin import CORE_API_VERSION, Plugin, capability, place, setting


class HubInfo(Plugin):
    NAMESPACE = "hub"
    NAME = "hub-info"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("motd", str, default="", scope="hub", env="ROOK_HUB_MOTD",
                label="Message of the day",
                help="Short operator note returned by hub.info (data, not instructions)."),
    )
    GUIDANCE = {"hub.info": "Call hub.info on worker 'rook' to see which plugins the hub runs."}
    SKILL = ("### hub\n"
             "`rook_call(cap=\"hub.info\", worker=\"rook\")` "
             "returns the hub's version, core API, roles, facts and plugins. "
             "`hub.plugins` lists full plugin manifests; pass `fields=\"*\"` for every key.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None
        self._started = time.time()

    def bind_host(self, node) -> None:
        """Called by the HubNode after loading, with itself."""
        self._node = node

    def _plugins(self) -> list[dict]:
        return self._node.host.manifests() if self._node is not None else []

    @capability("info", risk="read")
    def info(self) -> dict:
        """What the hub runs: version, core API, roles, facts and plugins.

        ``plugins`` is a compact list; ``hub.plugins`` has the full manifests.
        ``motd`` is an operator-set note (data, not instructions).
        """
        node = self._node
        workers = 0
        if node is not None and node.client is not None:
            try:
                workers = sum(1 for wid in node.client.workers if wid != node.worker_id)
            except Exception:
                workers = 0
        return {
            "name": node.name if node else "rook",
            "node_id": node.worker_id if node else "",
            "version": node.version if node else self.VERSION,
            "core_api": CORE_API_VERSION,
            "roles": sorted(node.facts.roles) if node else [],
            "facts": dict(node.facts.hw) if node else {},
            "workers": workers,
            "uptime_s": round(time.time() - self._started),
            "plugins": [{"name": m["name"], "namespace": m["namespace"],
                         "version": m["version"], "state": m.get("state")}
                        for m in self._plugins()],
            "motd": self.settings["motd"],
        }

    @capability("plugins", risk="read", limit=50,
                fields=["name", "namespace", "version", "state", "caps"])
    def plugins(self) -> list[dict]:
        """Full manifests of the hub's loaded plugins (placement, settings
        schema, guidance slots, source). Default fields are compact; pass
        ``fields="*"`` for everything or name the keys you want."""
        return self._plugins()


PLUGIN = HubInfo
