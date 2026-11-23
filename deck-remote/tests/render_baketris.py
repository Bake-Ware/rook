"""Renders Baketris's screens to PNGs headless (SDL dummy video), driving the
screen object as the app does: menus, settings, controls, a game in every
skin, a line clear, the quad's kaboom, T-spin text, pause, game over, the
on-screen pad. Also times the frame (update + draw) through a kaboom.

Run: /tmp/deck/venv/bin/python tests/render_baketris.py /tmp/deck/bk   (from deck-remote/)"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pygame  # noqa: E402

from rookmote.games import baketris as B  # noqa: E402
from rookmote.games import rules as G  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/deck/bk")
OUT.mkdir(parents=True, exist_ok=True)
pygame.init()
screen = pygame.display.set_mode((1280, 800))
prefs = Path(tempfile.mkdtemp()) / "baketris.json"


def shot(bk, name):
    bk.draw(screen)
    pygame.image.save(screen, str(OUT / f"{name}.png"))


def frames(bk, ms, step=16.667):
    t = 0.0
    while t < ms:
        bk.update(step)
        t += step


def stack(bk, rows=6):
    g = bk.game
    for r in range(rows):
        for c in range(G.W):
            if c != (r * 3) % 10:
                g.grid[r][c] = 1 + (r + c) % 7


bk = B.Baketris(prefs_path=prefs, seed=1)
frames(bk, 500)
shot(bk, "01-menu")
bk.state = B.S_SETTINGS
bk.sel = 2
shot(bk, "02-settings")
bk.state, bk.sel = B.S_CONTROLS, 0
shot(bk, "03-controls")
bk.state, bk.sel = B.S_MENU, 0

for skin in range(7):
    bk.skin = skin
    bk.apply_skin()
    bk.start_game(G.MARATHON)
    frames(bk, 700)
    if skin == 0:
        shot(bk, "04-ready")
    frames(bk, 800)
    stack(bk)
    for _ in range(3):
        bk.press(B.A_HARD)
        bk.last_drop_t = 0
        frames(bk, 50)
    bk.game.hold = G.T
    frames(bk, 900)
    shot(bk, f"10-play-{skin}-{B.SKIN_L[skin].lower()}")
    # a line clear, mid-flash
    g = bk.game
    for c in range(G.W):
        g.grid[0][c] = 0
    for r in range(G.H):
        g.grid[r] = [0] * G.W
    for c in range(G.W - 1):
        g.grid[0][c] = 1 + c % 7
        g.grid[1][c] = 1 + (c + 2) % 7
    g.cur, g.rot, g.px, g.py = G.I, 1, 7, 18
    g.hard_drop()
    frames(bk, 50)
    shot(bk, f"20-clear-{skin}")
    frames(bk, 2000)
    # the quad (GameView's F12)
    bk.key_down(pygame.K_F12)
    frames(bk, 30)
    shot(bk, f"30-kaboom-a-{skin}")
    frames(bk, 230)
    shot(bk, f"31-kaboom-b-{skin}")
    frames(bk, 350)
    shot(bk, f"32-kaboom-c-{skin}")
    frames(bk, 3000)

# frame time through a kaboom (update + draw), NEON
bk.skin = 0
bk.apply_skin()
bk.start_game(G.MARATHON)
frames(bk, 1600)
stack(bk, 4)
bk.key_down(pygame.K_F12)
times = []
for i in range(150):
    t0 = time.perf_counter()
    bk.update(16.667)
    bk.draw(screen)
    times.append((time.perf_counter() - t0) * 1000)
times.sort()
print(f"kaboom frames: median {times[75]:.2f} ms, p95 {times[142]:.2f} ms, max {times[-1]:.2f} ms")
times = []
for i in range(300):
    t0 = time.perf_counter()
    bk.update(16.667)
    bk.draw(screen)
    times.append((time.perf_counter() - t0) * 1000)
times.sort()
print(f"plain frames: median {times[150]:.2f} ms, p95 {times[285]:.2f} ms, max {times[-1]:.2f} ms")

# T-spin text, pause, game over, sprint/ultra HUDs, the on-screen pad
bk.start_game(G.MARATHON)
frames(bk, 1500)
bk.on_lock(2, [0, 1], 2, True, 3, False, True)
frames(bk, 300)
shot(bk, "40-tspin-text")
bk.pause_game()
shot(bk, "41-pause")
bk.state = B.S_PLAY
stack(bk, 18)
frames(bk, 100)
shot(bk, "42-danger")
bk.on_end(False)
frames(bk, 800)
shot(bk, "43-over")
bk.start_game(G.SPRINT)
frames(bk, 2500)
shot(bk, "44-sprint")
bk.pad_mode = True
bk.layout()
bk.start_game(G.ULTRA)
frames(bk, 2500)
bk.press(B.A_LEFT)
shot(bk, "45-ultra-pad")
print("shots in", OUT)
