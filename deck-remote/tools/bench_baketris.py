"""Baketris's frame times, headless (SDL's dummy video and audio: no window, no sound
device): update + draw of each frame, for the cases that cost the most. One line a
case: frames, p50 / p95 / max in ms. Under 20 s on a Steam Deck.

On the Deck (install.sh copies tools/ beside the app's rookmote/ from now on;
or run it from the source folder you installed from):
    ~/.local/share/rookmote/venv/bin/python ~/rookmote-src/deck-remote/tools/bench_baketris.py

A frame at 60 fps has 16.7 ms; the display's own flip is not in these numbers."""

from __future__ import annotations

import os
import platform
import random
import sys
import tempfile
import time
from pathlib import Path

os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
HERE = Path(__file__).resolve().parent
for p in (HERE.parent, HERE.parent / "app", Path.cwd()):
    if (p / "rookmote" / "games" / "baketris.py").is_file():
        sys.path.insert(0, str(p))
        break

import pygame  # noqa: E402

from rookmote import VERSION  # noqa: E402
from rookmote.games import baketris as B  # noqa: E402
from rookmote.games import rules as G  # noqa: E402

FRAME = 1000 / 60


def cpu() -> str:
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "?"


def line(name, ts):
    ts = sorted(ts)
    n = len(ts)
    p = lambda q: ts[min(n - 1, int(q * n))]                     # noqa: E731
    over = sum(1 for t in ts if t > FRAME)
    print(f"{name:16s} frames {n:4d}  p50 {p(0.5):6.2f}  p95 {p(0.95):6.2f}  max {ts[-1]:6.2f} ms"
          f"  over 16.7 ms: {over}", flush=True)


def frames(bk, screen, n, each=None):
    out = []
    for i in range(n):
        if each:
            each(i)
        t = time.perf_counter()
        bk.update(FRAME)
        bk.draw(screen)
        out.append((time.perf_counter() - t) * 1000)
    return out


def playing(bk, mode):
    bk.start_game(mode)
    while bk.state != B.S_PLAY:
        bk.update(FRAME)


def inputs(bk):
    def each(i):
        if i % 6 == 0:
            a = (B.A_LEFT, B.A_RIGHT, B.A_CW, B.A_CCW)[(i // 6) % 4]
            bk.press(a)
            bk.release(a)
        if i % 45 == 44:
            bk.last_drop_t = 0
            bk.press(B.A_HARD)
    return each


def quad(bk, rows_above=0, seed=1):
    g = bk.game
    rng = random.Random(seed)
    for r in range(G.H):
        g.grid[r] = [0] * G.W
    for r in range(4):
        g.grid[r] = [1 + rng.randrange(7) for _ in range(9)] + [0]
    for r in range(4, 4 + rows_above):
        g.grid[r] = [1 + rng.randrange(7) if rng.random() < 0.85 else 0 for _ in range(9)] + [0]
    g.cur, g.rot, g.px, g.py = G.I, 1, 7, 18
    g.hard_drop()


def main() -> int:
    t_all = time.perf_counter()
    pygame.init()
    screen = pygame.display.set_mode((1280, 800))
    print(f"bench_baketris  rookmote {VERSION}  python {platform.python_version()}  pygame-ce {pygame.version.ver}"
          f"  cpu {cpu()}", flush=True)
    bk = B.Baketris(prefs_path=Path(tempfile.mkdtemp()) / "baketris.json", seed=7)
    t = time.perf_counter()
    playing(bk, G.MARATHON)
    while bk._warm is not None:                      # play does this in spare frame time; here, all first
        bk.warm(50)
    print(f"{'sprite warm-up':16s} {1000 * (time.perf_counter() - t):7.0f} ms (spread over frames in play)", flush=True)

    line("normal play", frames(bk, screen, 300, inputs(bk)))

    playing(bk, G.MARATHON)
    quad(bk, rows_above=6)
    line("kaboom", frames(bk, screen, 120))

    playing(bk, G.BAKETRIS)
    for _ in range(30):
        bk.update(FRAME)
    quad(bk, rows_above=14)                          # a full well's worth goes up
    ts, n, blasts, flying = [], 0, 0, False
    while (bk.blast is not None or bk.blast_pending is not None or n < 15) and n < 600:
        ts += frames(bk, screen, 1)
        n += 1
        if bk.blast is not None and not flying:
            blasts += 1
        flying = bk.blast is not None
    line("baketris blast", ts)
    print(f"{'  blast to piece':16s} {n * FRAME / 1000:5.2f} s game time, {blasts} blast(s)", flush=True)

    for step in range(4):
        playing(bk, G.MARATHON)
        e = (0.0, 0.3, 0.6, 0.95)[step]
        go = inputs(bk)

        def each(i, e=e, go=go):
            bk.energy.e = max(bk.energy.e, e)
            go(i)
        line(f"energy step {step}", frames(bk, screen, 150, each))
    print(f"{'total':16s} {time.perf_counter() - t_all:5.1f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
