"""render_film.py stills OUTDIR t1 t2 ... | video OUT.mp4 F0 F1  (serves this folder over http; WebGL via SwiftShader)"""
import os, sys, subprocess, pathlib, threading, functools, http.server
from playwright.sync_api import sync_playwright
ROOT = pathlib.Path(__file__).resolve().parent
class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Q, directory=str(ROOT)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
FPS = 30
mode, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None, args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--force-color-profile=srgb'])
    pg = b.new_page(viewport={'width': 1920, 'height': 1080})
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(f'http://127.0.0.1:{srv.server_address[1]}/film.html')
    try: pg.wait_for_function('window.READY === true', timeout=120000)
    except Exception: print('ERRORS', errs); raise
    stage = pg.locator('#stage')
    if mode == 'stills':
        pathlib.Path(out).mkdir(parents=True, exist_ok=True)
        for ts in sys.argv[3:]:
            t = float(ts); pg.evaluate(f'render({t}, {round(t * FPS)})'); stage.screenshot(path=f'{out}/t{t:06.2f}.png')
    else:
        f0, f1 = int(sys.argv[3]), int(sys.argv[4])
        ff = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'image2pipe', '-c:v', 'mjpeg', '-framerate', str(FPS), '-i', '-',
                               '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-pix_fmt', 'yuv420p', '-r', str(FPS), out], stdin=subprocess.PIPE)
        for f in range(f0, f1):
            pg.evaluate(f'render({f / FPS}, {f})')
            ff.stdin.write(stage.screenshot(type='jpeg', quality=93))
            if f % 150 == 0: print(out, f, flush=True)
        ff.stdin.close(); ff.wait()
    if errs: print('ERRORS', errs[:5])
    b.close()
srv.shutdown()
