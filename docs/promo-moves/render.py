"""Render Rook Moves clips from index.html with headless Chromium.

    python render.py meta 1                     # clip timing, voice lines, sound cues -> out/c1.json
    python render.py stills 1 2.5 9 14          # preview frames -> out/c1_t002.50.png
    python render.py video 1 [--fps 30]         # picture only -> out/c1_video.mp4
    env GL=gpu to try the GPU (default: SwiftShader, which works everywhere)
"""
import asyncio, json, os, subprocess, sys, time
from pathlib import Path
from aiohttp import web
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / 'out'
OUT.mkdir(exist_ok=True)


async def serve():
    app = web.Application()
    app.router.add_static('/', HERE, show_index=False)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    return runner, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/index.html'


def gl_args():
    if os.environ.get('GL') == 'gpu':
        return ['--use-angle=vulkan', '--enable-features=Vulkan,VulkanFromANGLE', '--ignore-gpu-blocklist', '--enable-gpu-rasterization']
    return ['--use-angle=swiftshader', '--enable-unsafe-swiftshader']


async def open_clip(pw, n):
    exe = os.environ.get('CHROME')
    browser = await pw.chromium.launch(args=gl_args() + ['--disable-web-security'], executable_path=exe or None)
    page = await browser.new_page(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: m.type == 'error' and errors.append(m.text))
    runner, url = await serve()
    await page.goto(f'{url}?clip={n}')
    try:
        await page.wait_for_function('window.ready === true', timeout=90000)
    except Exception:
        raise SystemExit('clip did not load: ' + ' | '.join(errors[:5]))
    meta = await page.evaluate('window.CLIP')
    return browser, page, runner, meta, errors


async def main():
    mode, n = sys.argv[1], sys.argv[2]
    async with async_playwright() as pw:
        browser, page, runner, meta, errors = await open_clip(pw, n)
        (OUT / f'c{n}.json').write_text(json.dumps(meta, indent=1))
        if '--js' in sys.argv:
            i = sys.argv.index('--js'); await page.evaluate(sys.argv[i + 1]); del sys.argv[i:i + 2]
        tag = ''
        if '--tag' in sys.argv:
            i = sys.argv.index('--tag'); tag = sys.argv[i + 1]; del sys.argv[i:i + 2]
        if mode == 'meta':
            print('meta', n, meta['T'])
        elif mode == 'stills':
            for s in sys.argv[3:]:
                await page.evaluate(f'render({float(s)})')
                await page.screenshot(path=str(OUT / f'{tag}c{n}_t{float(s):06.2f}.png'))
            print('stills done', errors[:3])
        elif mode == 'video':
            fps = int(sys.argv[sys.argv.index('--fps') + 1]) if '--fps' in sys.argv else 30
            nf = int(round(meta['T'] * fps))
            out = OUT / f'c{n}_video.mp4'
            ff = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'image2pipe', '-framerate', str(fps), '-c:v', 'mjpeg', '-i', '-',
                                   '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-pix_fmt', 'yuv420p', '-r', str(fps), str(out)],
                                  stdin=subprocess.PIPE)
            t0 = time.time()
            for f in range(nf):
                await page.evaluate(f'render({f / fps})')
                ff.stdin.write(await page.screenshot(type='jpeg', quality=93))
                if f % 150 == 0:
                    print(f'c{n} frame {f}/{nf} {time.time() - t0:.0f}s', flush=True)
            ff.stdin.close(); ff.wait()
            print('video done', out, errors[:3])
        await browser.close(); await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
