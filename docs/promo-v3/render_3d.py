"""render_3d.py MODE OUTDIR FRAME0 FRAME1 [step] [ext]  -> frames named f%04d (global promo frame numbers)"""
import os, sys, threading, functools, http.server, pathlib
from playwright.sync_api import sync_playwright
HERE = pathlib.Path(__file__).resolve().parent
mode, out, f0, f1 = sys.argv[1], pathlib.Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
step = int(sys.argv[5]) if len(sys.argv) > 5 else 1
ext = sys.argv[6] if len(sys.argv) > 6 else 'png'
out.mkdir(parents=True, exist_ok=True)
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
class Q(H.func):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Q, directory=str(HERE)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
BEAT = 60 / 128
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None, args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'])
    pg = b.new_page(viewport={'width': 1920, 'height': 1080})
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e))); pg.on('console', lambda m: errs.append(m.text) if m.type == 'error' else None)
    pg.goto(f'http://127.0.0.1:{port}/scene3d.html?mode={mode}')
    try: pg.wait_for_function('window.READY===true', timeout=60000)
    except Exception: print('ERRORS', errs); raise
    for f in range(f0, f1, step):
        tb = f / 30 / BEAT
        pg.evaluate(f'setTime({tb})')
        if ext == 'png':
            pg.locator('canvas').screenshot(path=str(out / f'f{f:04d}.png'), omit_background=True)
        else:
            pg.locator('canvas').screenshot(path=str(out / f'f{f:04d}.jpg'), type='jpeg', quality=92)
        if f % 30 == 0: print(mode, f, flush=True)
    if errs: print('ERRORS', errs)
    b.close()
srv.shutdown()
