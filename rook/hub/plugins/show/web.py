"""The MCP side's page API for show pages: ``/show/page-api?id=<id>``.

The dashboard serves ``/show/<id>`` to any browser (rook/remote/bootstrap.py:
a TV or a phone's browser has no Rook sign-in) and asks this endpoint for the
page, in memory on a single-process hub and over loopback on a two-process
one, the same way it proxies the other ``/<x>/account-api`` endpoints. The
id is the credential (192 random bits); an unknown or expired one gets
``{"ok": false}`` with 404 and nothing else.
"""
from __future__ import annotations

import secrets
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PATH = "/show/page-api"


def routes(get_plugin: Callable[[], Any]):
    """``get_plugin()`` returns the loaded ``show`` hub plugin or None."""

    async def page(request: Request):
        plugin = get_plugin()
        if plugin is None:
            return JSONResponse({"ok": False, "error": "show is off on this hub"}, status_code=503)
        sid = str(request.query_params.get("id") or "")
        nonce = secrets.token_urlsafe(12)
        html = plugin.page_html(sid, nonce)
        if html is None:
            return JSONResponse({"ok": False}, status_code=404,
                                headers={"Cache-Control": "no-store"})
        return JSONResponse({"ok": True, "html": html, "nonce": nonce},
                            headers={"Cache-Control": "no-store"})

    return [Route(PATH, page, methods=["GET"])]
