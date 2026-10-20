"""Serve the director page on the LAN: http://<host>:8777/index.html?clip=1&director=1
Saved camera keys go to clips/cN.keys.json (the renderer picks them up); notes for Claude
go to clips/cN.notes.json."""
import sys
from pathlib import Path
from aiohttp import web

HERE = Path(__file__).resolve().parent


async def save(req):
    n = req.match_info['n']
    if not n.isdigit():
        raise web.HTTPBadRequest()
    p = HERE / 'clips' / f'c{n}.keys.json'
    if req.method == 'DELETE':
        p.unlink(missing_ok=True)
    else:
        p.write_text(await req.text())
    return web.Response(text='ok')


async def notes(req):
    n = req.match_info['n']
    if not n.isdigit():
        raise web.HTTPBadRequest()
    p = HERE / 'clips' / f'c{n}.notes.json'
    if req.method == 'GET':
        return web.Response(text=p.read_text() if p.exists() else '[]', content_type='application/json')
    p.write_text(await req.text())
    return web.Response(text='ok')


@web.middleware
async def no_cache(req, handler):
    # the page is edited while you use it: never let the browser keep an old copy
    res = await handler(req)
    res.headers['Cache-Control'] = 'no-store'
    return res


app = web.Application(middlewares=[no_cache])
app.router.add_route('POST', '/save/{n}', save)
app.router.add_route('DELETE', '/save/{n}', save)
app.router.add_route('GET', '/notes/{n}', notes)
app.router.add_route('POST', '/notes/{n}', notes)
app.router.add_get('/', lambda r: web.HTTPFound('/index.html?clip=1&director=1'))
app.router.add_static('/', HERE)
web.run_app(app, host='0.0.0.0', port=int(sys.argv[1]) if len(sys.argv) > 1 else 8777)
