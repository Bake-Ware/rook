"""The hub's one HTTP server: the knock endpoint, the MCP door, webhooks and the site.

aiohttp, because the hub is asyncio already: a handler awaits a call on a
worker directly, and the same server can later serve the site and live
streams (WebSocket) from the same address.
"""

from aiohttp import web

from rook_protocol import ProtocolError, decode, encode

from . import door, events, site
from .errors import error


def app(hub):
    async def knock(request):
        """`POST /knock`: a knock in, a knock_reply or error out (docs/protocol.md)."""
        try:
            message = decode(await request.read())
            if message["type"] != "knock":
                raise ProtocolError(f"expected a knock, got {message['type']}")
            reply = hub.knock(message)
        except ProtocolError as e:
            reply = error("bad_message", str(e), "Send a knock as docs/protocol.md defines it.")
        status = 200 if reply["type"] == "knock_reply" else 400 if reply["code"] == "bad_message" else 403
        return web.Response(body=encode(reply), status=status, content_type="application/json")

    application = web.Application()
    application.router.add_post("/knock", knock)
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
