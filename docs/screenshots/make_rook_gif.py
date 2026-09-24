"""Render the dashboard's rook artwork turning a full circle, as a seamless
looping GIF (for the README) and MP4 (for social posts).

    .venv/bin/python docs/screenshots/make_rook_gif.py [--out docs/img] [--seconds 8] [--fps 20]

rook.gif (~4 MB, 400 px) goes in the README; rook.mp4 (1120x1280, three loops)
is for social posts and is gitignored.

Uses the real rook-scene.js. The copy served here only exposes the rook's
rotation and a render call, so each frame is set to an exact angle instead of
depending on wall-clock timing; frame k shows k/N of a full turn, so the last
frame flows into the first. Needs ffmpeg.
"""
import asyncio
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from aiohttp import web
from playwright.async_api import async_playwright

WEB = ROOT / 'rook/web'
HOOK_FROM = 'c.rotation.y=-.42,o.add(c);'
HOOK_TO = HOOK_FROM + 'window.__rook={group:c,render:()=>a.render(o,l)};'
DPR_FROM = 'a.setPixelRatio(Math.min(devicePixelRatio||1,1.25))'   # the dashboard caps resolution; a capture shouldn't

PAGE = '''<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/rook-art.css">
<style>
 html,body{margin:0;background:var(--bg);overflow:hidden}
 #stage{position:relative;width:WIDTHpx;height:HEIGHTpx;background:var(--bg)}
 #stage .rook-art{position:absolute;inset:0;right:auto;top:0;left:0;width:100%;height:100%;min-height:0;opacity:.9}
 #stage .rook-art .plate-number{font-size:11px;left:9%;top:12%}#stage .rook-art .plate-caption{font-size:10px;letter-spacing:1.7px;left:auto;right:6%;bottom:5%;text-align:right}
 .mark{position:absolute;left:34px;top:30px;z-index:2;font:500 20px/1 var(--sans);letter-spacing:.34em;color:#e7e2cf}
</style></head><body>
<div id="stage">
  <div class="mark">&#9820; ROOK</div>
  ART
</div>
<button id="art-toggle" hidden></button>
<script type="module">
 localStorage.setItem('rook.art.motion','off');          // no built-in animation; frames are set explicitly
 const {mountRook}=await import('/rook-scene.js');
 mountRook(document.getElementById('rook-art'),document.getElementById('art-toggle'));
 window.ready=true;
</script></body></html>'''


async def main():
    arg = lambda k, d: type(d)(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else d
    out = Path(arg('--out', str(ROOT / 'docs/img')))
    seconds, fps = arg('--seconds', 8.0), arg('--fps', 20)
    width, height = 560, 640
    if not shutil.which('ffmpeg'):
        raise SystemExit('ffmpeg is required')
    scene = (WEB / 'rook-scene.js').read_text()
    if HOOK_FROM not in scene or DPR_FROM not in scene:
        raise SystemExit('rook-scene.js changed; update HOOK_FROM in this script')
    index = (WEB / 'index.html').read_text()
    art = index[index.index('<div class="rook-art"'):]
    art = art[:art.index('<span class="plate-caption">')]
    art += index[index.index('<span class="plate-caption">'):].split('</span>', 1)[0] + '</span>\n</div>'   # same plate as the site
    page = PAGE.replace('WIDTH', str(width)).replace('HEIGHT', str(height)).replace('ART', art)

    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text=page, content_type='text/html'))
    app.router.add_get('/rook-scene.js', lambda r: web.Response(text=scene.replace(HOOK_FROM, HOOK_TO).replace(DPR_FROM, 'a.setPixelRatio(devicePixelRatio||1)'),
                                                                content_type='text/javascript'))
    app.router.add_get('/{name}', lambda r: web.FileResponse(WEB / r.match_info['name']))
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'

    frames = Path(tempfile.mkdtemp(prefix='rook-frames-'))
    n = int(seconds * fps)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page_ = await browser.new_page(viewport={'width': width, 'height': height}, device_scale_factor=2)
        errors = []
        page_.on('pageerror', lambda e: errors.append(str(e)))
        await page_.goto(base)
        await page_.wait_for_function('window.ready && window.__rook', timeout=20000)
        if await page_.get_attribute('#rook-art', 'data-mode') != 'webgl':
            raise SystemExit('WebGL did not start: ' + '; '.join(errors))
        await page_.evaluate('document.fonts.ready')
        stage = page_.locator('#stage')
        for k in range(n):
            angle = -0.42 + 2 * math.pi * k / n
            await page_.evaluate(f'__rook.group.rotation.y={angle};__rook.render()')
            await stage.screenshot(path=str(frames / f'f{k:04d}.png'))
        await browser.close()
    await runner.cleanup()

    out.mkdir(parents=True, exist_ok=True)
    src = ['-framerate', str(fps), '-i', str(frames / 'f%04d.png')]
    gif, mp4 = out / 'rook.gif', out / 'rook.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *src, '-vf',
                    'scale=400:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=full[p];'
                    '[b][p]paletteuse=dither=bayer:bayer_scale=4', '-loop', '0', str(gif)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-stream_loop', '2', *src, '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', '-r', '30', '-crf', '18', '-preset', 'slow', '-movflags', '+faststart', str(mp4)],
                   check=True)
    shutil.rmtree(frames)
    for f in (gif, mp4):
        print(f'wrote {f} ({f.stat().st_size / 1e6:.1f} MB)')


asyncio.run(main())
