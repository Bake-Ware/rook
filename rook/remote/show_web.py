"""``/show/<id>``: the pages ``show.present`` puts on a screen (rook/hub/plugins/show).

Public on purpose (the dashboard's auth middleware lets ``/show`` through): a
TV's or a phone's browser that opens the link has no Rook sign-in. The id is
the credential (192 random bits, short-lived); the page comes from the MCP
side's ``/show/page-api`` (in memory on ``rook hub serve``, loopback on a
two-process hub) and is served sandboxed, uncached and unindexed, with no
``Referer`` (rook/hub/plugins/show/render.py). An unknown, expired or
malformed id gets the same "gone" page, so the endpoint says nothing about
which ids exist.
"""
from __future__ import annotations

import logging
import os
import re

from aiohttp import web

from . import upstream

log = logging.getLogger("rook.remote.show_web")

UPSTREAM = ("ROOK_SHOW_PAGE_URL", "http://127.0.0.1:8765/show/page-api")
PATH = "/show/{sid}"
_ID = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _respond(html: str, status: int, nonce: str = "") -> web.Response:
    from ..hub.plugins.show import render
    headers = dict(render.HEADERS)
    headers["Content-Security-Policy"] = render.csp(nonce or "none")
    return web.Response(text=html, status=status, content_type="text/html",
                        charset="utf-8", headers=headers)


async def show_page(request: web.Request) -> web.Response:
    from ..hub.plugins.show import render
    sid = request.match_info.get("sid", "")
    if not _ID.match(sid):
        return _respond(render.gone_html(), 404)
    try:
        status, body = await upstream.request_json(
            "GET", os.environ.get(*UPSTREAM), params={"id": sid}, timeout=10)
    except upstream.Unavailable as e:
        log.warning("show page: the MCP side is unavailable (%s)", e)
        return web.Response(text="Rook is busy: try again in a moment.", status=503,
                            headers={"Cache-Control": "no-store", "Retry-After": "5"})
    if status != 200 or not isinstance(body, dict) or not body.get("ok") or not body.get("html"):
        return _respond(render.gone_html(), 404)
    return _respond(str(body["html"]), 200, str(body.get("nonce") or ""))


def install(app: web.Application) -> None:
    app.router.add_get(PATH, show_page)
