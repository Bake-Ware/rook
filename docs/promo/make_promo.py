"""Render the Rook promo video: docs/promo/promo.html (animation) plus
docs/promo/soundtrack.js (music, synthesized with Web Audio), muxed by ffmpeg.

    .venv/bin/python docs/promo/make_promo.py [--out rook-promo.mp4] [--fps 30]
    .venv/bin/python docs/promo/make_promo.py --preview   # key frames only -> preview.png
    .venv/bin/python docs/promo/make_promo.py --audio     # soundtrack only -> soundtrack.wav

Each frame is rendered at an exact time (render(t)), so the picture and the
120 BPM soundtrack stay in sync. Uses the real rook scene; the copy served
here only exposes its rotation (as in docs/screenshots/make_rook_gif.py).
"""
import asyncio
import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from aiohttp import web
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
WEB, IMG = ROOT / 'rook/web', ROOT / 'docs/img'
DURATION = 48.0
HOOK_FROM = 'c.rotation.y=-.42,o.add(c);'
HOOK_TO = HOOK_FROM + 'window.__rook={group:c,render:()=>a.render(o,l)};'
PREVIEW_TIMES = [1.5, 4.6, 7.4, 10.5, 14, 17.5, 21.5, 26.5, 30.8, 34, 35.8, 39.8, 43.2, 45.5]


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


async def serve():
    scene = (WEB / 'rook-scene.js').read_text()
    if HOOK_FROM not in scene:
        raise SystemExit('rook-scene.js changed; update HOOK_FROM')
    app = web.Application()
    app.router.add_get('/', lambda r: web.FileResponse(HERE / 'promo.html'))
    app.router.add_get('/soundtrack.js', lambda r: web.FileResponse(HERE / 'soundtrack.js'))
    app.router.add_get('/rook-scene.js', lambda r: web.Response(text=scene.replace(HOOK_FROM, HOOK_TO),
                                                                content_type='text/javascript'))
    app.router.add_get('/img/{name}', lambda r: web.FileResponse(IMG / r.match_info['name']))
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    return runner, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'


async def main():
    out = Path(arg('--out', str(HERE / 'rook-promo.mp4')))
    fps = arg('--fps', 30)
    preview, audio_only = '--preview' in sys.argv, '--audio' in sys.argv
    runner, base = await serve()
    work = Path(tempfile.mkdtemp(prefix='rook-promo-'))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader',
                                                 '--autoplay-policy=no-user-gesture-required'])
        page = await browser.new_page(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(base)
        await page.wait_for_function('window.ready && window.rookReady && window.__rook', timeout=30000)
        await page.evaluate('document.fonts.ready')
        await page.wait_for_timeout(500)

        wav = work / 'soundtrack.wav'
        if not preview:
            res = await page.evaluate(f"import('/soundtrack.js').then(m => m.renderSoundtrack({DURATION}))")
            wav.write_bytes(base64.b64decode(res['wav']))
            print(f'soundtrack: {wav.stat().st_size / 1e6:.1f} MB, peak {res["peak"]:.2f}')
            if audio_only:
                shutil.copy(wav, HERE / 'soundtrack.wav'); print('wrote', HERE / 'soundtrack.wav')

        if preview:
            for i, t in enumerate(PREVIEW_TIMES):
                await page.evaluate(f'render({t})')
                await page.screenshot(path=str(work / f'p{i:02d}.jpg'), type='jpeg', quality=85)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', '1', '-i', str(work / 'p%02d.jpg'),
                            '-vf', 'scale=640:-1,tile=2x7:padding=6:color=black', '-frames:v', '1',
                            str(HERE / 'preview.png')], check=True)
            print('wrote', HERE / 'preview.png')
        elif not audio_only:
            n = int(DURATION * fps)
            for k in range(n):
                await page.evaluate(f'render({k / fps})')
                await page.screenshot(path=str(work / f'f{k:05d}.jpg'), type='jpeg', quality=93)
                if k % (fps * 4) == 0:
                    print(f'frame {k}/{n}', flush=True)
        await browser.close()
    await runner.cleanup()
    if errors:
        print('page errors:', *errors, sep='\n  ')

    if not preview and not audio_only:
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', str(fps), '-i', str(work / 'f%05d.jpg'),
                        '-i', str(wav), '-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', str(out)], check=True)
        print(f'wrote {out} ({out.stat().st_size / 1e6:.1f} MB)')
    shutil.rmtree(work)


asyncio.run(main())
