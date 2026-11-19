"""The hub's one HTTP server: the knock endpoint, the MCP door, webhooks and the site.

aiohttp, because the hub is asyncio already: a handler awaits a call on a
worker directly, and the same server can later serve the site and live
streams (WebSocket) from the same address.

Every request takes its turn before its handler runs (`Turns`), so that a burst of requests is
taken one at a time and the workers' links are read between any two (docs/stress.md, fault 1).
"""

import asyncio

from aiohttp import web

from rook_protocol import ProtocolError, decode, encode

from . import door, events, link, site
from .errors import error


class Turns:
    """Requests wait, in the order they came, for the hub's loop to take them one at a time.

    The hub runs on one asyncio loop. In each of its turns the loop runs every task that is ready,
    then reads its sockets. Requests that arrive together are ready together: 10,000 calls at once
    ran back to back in one turn of about 5 s, during which the hub read nothing from the workers'
    links. A worker's Telesthete Channel that hears no acknowledgement for ten retransmits (about
    5.5 s) gives up on the link, and every call in flight on it was answered worker_disconnected
    although no worker had it (docs/stress.md, fault 1; brief 49).

    `await take()` returns once every request that took a turn before this one has had its own,
    and never in the same turn of the loop as the one before it: between any two, the loop reads its
    sockets. A request with none before it goes at once. Nothing is refused, dropped or cut short,
    and there is no maximum: a burst waits as long as the work ahead of it takes."""

    def __init__(self):
        self._last = None       # done one turn of the loop after the latest request's turn came

    async def take(self):
        loop = asyncio.get_running_loop()
        before, after = self._last, loop.create_future()
        self._last = after
        try:
            if before is not None and not before.done():
                await asyncio.shield(before)      # a request that stops waiting leaves before untouched
        finally:
            if before is None or before.done():
                loop.call_soon(_done, after)
            else:   # it stopped waiting (cancelled): the next one's turn still comes after the one before
                before.add_done_callback(lambda _: loop.call_soon(_done, after))


# Paths whose answers are data, not the site's files: never kept by a browser or a proxy (brief 66).
# /band is the workers' carrier (a WebSocket), not a file of the site either.
API_PATHS = ("/api/", "/mcp", "/knock", "/link", "/webhook", "/band")


def _is_api(path):
    return any(path == p or path.startswith(p if p.endswith("/") else p + "/") for p in API_PATHS)


async def cache_header(request, response):
    """Every answer says how it may be cached (brief 66). The site's files (the index at `/` and at each
    page path, `/site/*`, a plugin's pages and files) are `no-cache`: a browser or proxy such as
    Cloudflare may keep a copy but asks the hub before each use, and the ETag answers 304 when nothing
    changed, so a deploy is seen on the next load and old modules never run against a new hub. API
    answers are `no-store`: a revealed value or a new token is never kept anywhere."""
    response.headers["Cache-Control"] = "no-store" if _is_api(request.path) else "no-cache"


def _done(future):
    if not future.done():
        future.set_result(None)


def app(hub):
    turns = Turns()

    @web.middleware
    async def in_turn(request, handler):
        await turns.take()
        return await handler(request)

    async def knock(request):
        """`POST /knock`: a knock in, a knock_reply or error out (docs/protocol.md)."""
        try:
            message = await hub.off_loop(decode, await request.read())
            if message["type"] != "knock":
                raise ProtocolError(f"expected a knock, got {message['type']}")
            reply = await hub.off_loop(hub.knock, message)       # the database (brief 53)
        except ProtocolError as e:
            reply = error("bad_message", str(e), "Send a knock as docs/protocol.md defines it.")
        status = 200 if reply["type"] == "knock_reply" else 400 if reply["code"] == "bad_message" else 403
        return web.Response(body=encode(reply), status=status, content_type="application/json")

    application = web.Application(middlewares=[in_turn])
    application.on_response_prepare.append(cache_header)
    application.router.add_post("/knock", knock)
    link.add_routes(application, hub)
    door.add_routes(application, hub)
    events.add_routes(application, hub)
    site.add_routes(application, hub)
    return application


async def serve(hub, host, port):
    """Start serving. Returns (runner, (host, port)); `await runner.cleanup()` stops it."""
    runner = web.AppRunner(app(hub), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner, runner.addresses[0][:2]
