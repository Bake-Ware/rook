"""``plugin.marketplace.*``: plugin catalogs on worker ``rook`` (preview).

``plugin.marketplace.list`` returns the catalogs listed in the hub setting
``core.plugins.catalogs`` (or the bundled example catalog when none is set),
validated against the format in docs/design/plugin-marketplace.md.
``plugin.marketplace.install`` is a stub: installing from a catalog is not
implemented, and it says so (with the interim path: ``pip install`` a
package that declares a ``rook.plugins`` entry point, then restart).
"""

from __future__ import annotations

from typing import Any

from ...core.plugin import Plugin, capability, place
from ...core.pluginkit import CapError


class Marketplace(Plugin):
    NAMESPACE = "plugin"
    NAME = "marketplace"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### plugin marketplace (preview)\n"
             "`plugin.marketplace.list` on worker `rook` lists catalog entries (name, risk, "
             "caps, package). Installing is not implemented yet: "
             "`plugin.marketplace.install` always refuses. Switch installed plugins with "
             "`worker.plugin.enable/disable` on a worker or `hub.plugin.enable/disable`.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._last_refresh = float("-inf")

    def bind_host(self, node: Any) -> None:
        self._node = node

    #: Seconds between forced catalog refetches (outbound fetches).
    REFRESH_EVERY = 60.0

    def _may_refresh(self, refresh: bool) -> bool:
        """``refresh`` fetches every catalog URL again: honoured only for band
        owners and operator tokens, at most once a minute. Anyone else (this
        cap is read-only) gets the cached copy."""
        if not refresh:
            return False
        from ..authz import require_hub_admin
        if require_hub_admin("plugin.marketplace.list refresh"):
            return False
        import time
        now = time.monotonic()
        if now - self._last_refresh < self.REFRESH_EVERY:
            return False
        self._last_refresh = now
        return True

    def _admin(self):
        if self._node is None:
            raise CapError("the marketplace is not bound to a hub")
        from ..plugin_admin import admin_for
        return admin_for(self._node)

    @capability("marketplace.list", risk="read", limit=50,
                fields=["id", "name", "version", "risk", "namespace", "compatible"], scope="external")
    async def list_entries(self, refresh: bool = False) -> list[dict]:
        """Plugin catalog entries (preview): id, name, version, author, risk,
        caps, placement, package and whether this core can run it.

        Catalog URLs come from the hub setting ``core.plugins.catalogs``; with
        none set, a bundled example catalog (fictional entries) is listed.
        ``refresh`` refetches the catalogs: band owners and operator tokens
        only, at most once a minute; otherwise the (5 min) cached copy."""
        data = await self._admin().catalog(refresh=self._may_refresh(bool(refresh)))
        out = []
        for cat in data["catalogs"]:
            for e in cat["plugins"]:
                out.append({**e, "catalog": cat["name"], "example": cat["example"]})
        return out

    @capability("marketplace.install", risk="admin", tags=("destructive",), scope="external")
    def install(self, id: str) -> dict:
        """Install a catalog plugin: NOT IMPLEMENTED (always refuses).

        Until it is, install a third-party plugin by hand: pip install its
        package (it declares a ``rook.plugins`` entry point) into the hub's or
        a worker's environment and restart that process."""
        from ..plugin_catalog import NOT_IMPLEMENTED
        raise CapError(NOT_IMPLEMENTED)


PLUGIN = Marketplace
