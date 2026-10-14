"""``ui.*``: the dashboard's look and start page, as a hub plugin.

* Themes (themes.py, rook/web/themes/*.json): each account picks one; the
  choice is the user-scope setting ``ui.theme``.
* The start page (layout.py): each account's widget layout, the user-scope
  setting ``ui.startpage``. Feeds on it are fetched by the hub (feeds.py)
  under an address guard and size and time limits the operator sets here.
* The knowledge half of the Overview page (overview.py).

The dashboard reaches all of it through ``/ui/account-api`` (web.py). Other
hub plugins can add start page widgets by declaring ``WIDGETS`` (see
:meth:`DashboardUI.widgets` and docs/web/start-page.md).
"""

from __future__ import annotations

import logging

from ....core.plugin import Plugin, place, setting
from . import themes as T
from .feeds import FeedCache

log = logging.getLogger("rook.hub.plugins.ui")

try:
    _THEMES = T.load()
except T.ThemeError:
    log.exception("bundled dashboard themes are invalid; only the default is offered")
    _THEMES = {}
THEME_IDS = tuple(sorted(_THEMES, key=lambda t: (t != T.DEFAULT, t))) or (T.DEFAULT,)


class DashboardUI(Plugin):
    NAMESPACE = "ui"
    NAME = "dashboard-ui"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("theme", str, default=T.DEFAULT, scope="user", choices=THEME_IDS,
                group="Look", order=1, label="Theme",
                help="Colours of the web dashboard for this account. Pick one with a preview "
                     "on Settings > My preferences."),
        setting("startpage", dict, default={}, scope="user", group="Start page", order=2,
                advanced=True, label="Start page layout",
                help="Widgets and columns of this account's start page. Edit them on the "
                     "Start page itself (Edit page); empty means the default page."),
        setting("feeds_allow_private", bool, default=False, group="Feeds", order=3,
                label="Let feeds reach private addresses",
                help="Off: the hub fetches start page feeds only from public internet "
                     "addresses. On: also from your own network (192.168.x.x, 10.x.x.x and "
                     "the like) and this machine, for a feed served in your homelab. Anyone "
                     "who can sign in could then make the hub fetch those addresses."),
        setting("feed_max_kb", int, default=512, min=16, max=4096, group="Feeds", order=4,
                label="Largest feed (KB)", help="A feed bigger than this is refused."),
        setting("feed_timeout", float, default=8.0, min=1.0, max=30.0, group="Feeds", order=5,
                label="Feed time limit (seconds)",
                help="The whole fetch, redirects included, must finish within this."),
        setting("feed_cache_minutes", int, default=15, min=1, max=1440, group="Feeds", order=6,
                label="Keep a fetched feed for (minutes)",
                help="Every start page showing a feed shares one fetch per period."),
    )

    def __init__(self) -> None:
        super().__init__()
        self._node = None
        self.feeds = FeedCache()

    def bind_host(self, node) -> None:
        self._node = node

    # -- themes --------------------------------------------------------------
    @staticmethod
    def themes() -> dict[str, dict]:
        return _THEMES

    def theme_css(self, theme_id: str) -> dict:
        theme = _THEMES.get(theme_id) or _THEMES.get(T.DEFAULT)
        if theme is None:
            return {"id": T.DEFAULT, "css": ""}
        return {"id": theme["id"], "scheme": theme["scheme"], "css": T.css(theme, _THEMES)}

    def theme_list(self) -> list[dict]:
        return [T.summary(_THEMES[t], _THEMES) for t in THEME_IDS if t in _THEMES]

    # -- widgets other plugins add ---------------------------------------------
    def widgets(self) -> list[dict]:
        """Start page widget types declared by loaded hub plugins:
        ``WIDGETS = ({"type": "<ns>.<name>", "title", "description",
        "module": "/same-origin/path.js"},)``. The page imports ``module``,
        which registers a renderer for ``type``."""
        out = []
        host = getattr(self._node, "host", None)
        for p in getattr(host, "plugins", ()) or ():
            for w in getattr(p, "WIDGETS", ()) or ():
                try:
                    wtype, module = str(w["type"]), str(w["module"])
                except (KeyError, TypeError):
                    continue
                if "." not in wtype or not module.startswith("/") or module.startswith("//"):
                    log.warning("plugin %s: ignoring start page widget %r", p.NAMESPACE, w)
                    continue
                out.append({"type": wtype, "module": module, "plugin": p.NAMESPACE,
                            "title": str(w.get("title") or wtype)[:60],
                            "description": str(w.get("description") or "")[:200]})
        return out

    # -- feeds ---------------------------------------------------------------
    async def feed(self, url: str, limit: int = 8) -> dict:
        s = self.settings
        return await self.feeds.get(
            url.strip(), ttl=60.0 * s.get("feed_cache_minutes", 15), limit=max(1, min(int(limit), 30)),
            allow_private=bool(s.get("feeds_allow_private", False)),
            max_bytes=1024 * int(s.get("feed_max_kb", 512)), timeout=float(s.get("feed_timeout", 8.0)))


PLUGIN = DashboardUI
