"""Serve the Rook Moves review site on the LAN: http://<host>:8777/ lists the clips with their
renders and notes; index.html?clip=N&director=1 is the director page for one clip.
Saved camera keys go to clips/cN.keys.json (the renderer picks them up); notes for Claude
go to clips/cN.notes.json."""
import asyncio, json, re, sys
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


def final(n):
    hits = sorted((HERE / 'out' / 'final').glob(f'Rook Moves {n} - *.mp4'))
    return hits[0] if hits else None


async def clips(req):
    """Every clip: title, tagline, length, render, saved keys, notes."""
    out = []
    for src in sorted((HERE / 'clips').glob('c[0-9]*.js'), key=lambda p: int(p.stem[1:])):
        n = int(src.stem[1:]); js = src.read_text()
        title = re.search(r"title: '([^']*)'", js); tag = re.search(r"tagline: '([^']*)'", js); body = re.search(r'body: (\d+)', js)
        f = final(n); kp = HERE / 'clips' / f'c{n}.keys.json'; np_ = HERE / 'clips' / f'c{n}.notes.json'
        out.append({
            'n': n, 'title': title and title.group(1), 'tagline': tag and re.sub('<[^>]+>', ' ', tag.group(1)),
            'body': body and int(body.group(1)),
            'video': f and f'/video/{n}', 'rendered': f and f.stat().st_mtime,
            'keys': kp.exists() and kp.stat().st_mtime,
            'notes': json.loads(np_.read_text()) if np_.exists() else [],
        })
    return web.json_response(out)


async def video(req):
    """The full render (?full=1, for download) or a light 720p copy for playing in the page."""
    n = req.match_info['n']; f = final(n)
    if not f:
        raise web.HTTPNotFound()
    if req.query.get('full'):
        return web.FileResponse(f, headers={'Content-Disposition': f'attachment; filename="{f.name}"'})
    w = HERE / 'out' / 'web' / f'c{n}.mp4'
    if not w.exists() or w.stat().st_mtime < f.stat().st_mtime:
        w.parent.mkdir(parents=True, exist_ok=True)
        tmp = w.with_suffix('.tmp.mp4')
        p = await asyncio.create_subprocess_exec(
            'ffmpeg', '-v', 'error', '-y', '-i', str(f), '-vf', 'scale=1280:-2:out_range=tv,format=yuv420p',
            '-c:v', 'libx264', '-preset', 'veryfast', '-profile:v', 'main', '-crf', '24', '-maxrate', '2500k', '-bufsize', '5000k',
            '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(tmp))
        await p.wait()
        tmp.replace(w)
    return web.FileResponse(w)


async def thumb(req):
    n = req.match_info['n']; f = final(n)
    if not f:
        raise web.HTTPNotFound()
    t = HERE / 'out' / 'thumbs' / f'c{n}.jpg'
    if not t.exists() or t.stat().st_mtime < f.stat().st_mtime:
        t.parent.mkdir(parents=True, exist_ok=True)
        p = await asyncio.create_subprocess_exec('ffmpeg', '-v', 'error', '-y', '-ss', req.query.get('at', '9'), '-i', str(f),
                                                 '-frames:v', '1', '-vf', 'scale=640:-1', str(t))
        await p.wait()
    return web.FileResponse(t)


@web.middleware
async def no_cache(req, handler):
    # the page is edited while you use it: never let the browser keep an old copy
    res = await handler(req)
    if not req.path.startswith(('/video/', '/thumb/')):   # media may cache; pages and scripts never do
        res.headers['Cache-Control'] = 'no-store'
    return res


app = web.Application(middlewares=[no_cache])
app.router.add_route('POST', '/save/{n}', save)
app.router.add_route('DELETE', '/save/{n}', save)
app.router.add_route('GET', '/notes/{n}', notes)
app.router.add_route('POST', '/notes/{n}', notes)
app.router.add_get('/clips', clips)
app.router.add_get('/video/{n}', video)
app.router.add_get('/thumb/{n}', thumb)
app.router.add_get('/', lambda r: web.FileResponse(HERE / 'home.html'))
app.router.add_static('/', HERE)
web.run_app(app, host='0.0.0.0', port=int(sys.argv[1]) if len(sys.argv) > 1 else 8777)
