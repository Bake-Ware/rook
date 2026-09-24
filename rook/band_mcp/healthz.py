"""GET /healthz: MCP session-table counters and the live worker count.

Bearer-gated with the static token, since the public hostname reaches this
too and the payload names client IPs. Read by rook/band_mcp/watchdog.py.
"""
import hmac
import time

from starlette.responses import JSONResponse
from starlette.routing import Route


def route(manager, client, token: str) -> Route:
    async def healthz(request):
        got = request.headers.get('authorization', '')
        if not token or not hmac.compare_digest(got.encode(), f'Bearer {token}'.encode()):
            return JSONResponse({'error': 'unauthorized'}, 401)
        now = time.time()
        workers = getattr(client, 'workers', {}) or {}
        live = [w for w in workers.values() if now - w.get('last_seen', 0.0) < 90]
        body = {'ok': True, 'workers': len(live), 'workers_known': len(workers)}
        if hasattr(manager, 'stats'):
            body['mcp'] = manager.stats()
        return JSONResponse(body, headers={'Cache-Control': 'no-store'})
    return Route('/healthz', healthz, methods=['GET'])
