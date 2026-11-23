"""What the remote's small games (2048, Towers, Swarm) share on the Deck.

Their rules are the firmware's (firmware-remote/src/*.cpp), ported as they are:
each has start(ms) and tick(ms, keys) -> changed, fed the buttons held now as
the firmware's game keys (tetris.h's bits), and finds the presses itself.
Screen turns the Deck's buttons, stick, keyboard and touch into those bits,
runs the game each frame, keeps the best scores, and draws it Deck-sized."""

from __future__ import annotations

import json
import random
import time

import pygame

from ..paths import data_dir

LEFT, RIGHT, DOWN, ROTATE, DROP, PAUSE, QUIT, ROTATE_BACK, HOLD = 1, 2, 4, 8, 16, 32, 64, 128, 256

# The firmware's game_key() for a flat game: up is ROTATE (up), ok is DROP, back is QUIT; the
# bridge's volume keys (ROTATE_BACK) are Y and the shoulders here, its home key (HOLD) is X.
PAD = {"dpleft": LEFT, "dpright": RIGHT, "dpdown": DOWN, "dpup": ROTATE, "a": DROP, "start": PAUSE,
       "b": QUIT, "back": QUIT, "y": ROTATE_BACK, "leftshoulder": ROTATE_BACK, "rightshoulder": ROTATE_BACK,
       "x": HOLD}
KEYS = {pygame.K_LEFT: LEFT, pygame.K_RIGHT: RIGHT, pygame.K_DOWN: DOWN, pygame.K_UP: ROTATE,
        pygame.K_RETURN: DROP, pygame.K_SPACE: DROP, pygame.K_p: PAUSE, pygame.K_ESCAPE: QUIT,
        pygame.K_BACKSPACE: QUIT, pygame.K_TAB: ROTATE_BACK, pygame.K_h: HOLD}


def cdiv(a: int, b: int) -> int:
    """C's integer division: toward zero."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def due(ms: int, t: int) -> bool:
    """(int32_t)(ms - t) >= 0"""
    return ((ms - t) & 0xFFFFFFFF) < 0x80000000


def esp_random() -> int:
    return random.getrandbits(32)


class Bests:
    """~/.local/share/rookmote/games.json: {game: best}."""

    def __init__(self, path=None):
        self.path = path or (data_dir() / "games.json")

    def get(self, game: str) -> int:
        try:
            return int(json.loads(self.path.read_text()).get(game, 0))
        except (OSError, ValueError, AttributeError):
            return 0

    def put(self, game: str, v: int) -> None:
        try:
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        if v <= int(d.get(game, 0) or 0):
            return
        d[game] = int(v)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(d))
            tmp.replace(self.path)
        except OSError:
            pass


class Screen:
    """A firmware game on the Deck's screen. Subclasses set NAME, make self.g (the
    rules, with start/tick/quit/score) and draw(surface)."""

    NAME = ""

    def __init__(self, size=(1280, 800), bests: Bests | None = None, rumble=None):
        self.vw, self.vh = size
        self.bests = bests or Bests()
        self.best = self.bests.get(self.NAME)
        self.rumble_fn = rumble
        self.done = False
        self.held = 0                     # pad and keyboard bits
        self.stick = 0                    # the left stick's bits
        self.pulse = 0                    # one tick's worth of a touch gesture
        self.t0 = time.monotonic()
        self.ms = 1
        self.paused = False

    # the rules' clock: milliseconds, as millis()
    def update(self, dt: float) -> None:
        self.ms = (self.ms + max(1, int(round(dt)))) & 0xFFFFFFFF
        keys = self.held | self.stick | self.pulse
        self.pulse = 0
        self.g.tick(self.ms, keys)
        s = self.g.score()
        if s > self.best:
            self.best = s
            self.bests.put(self.NAME, s)
        if self.g.quit():
            self.done = True

    # input (the app's handle_game calls these)
    def pad_down(self, b: str) -> None:
        self.held |= PAD.get(b, 0)

    def pad_up(self, b: str) -> None:
        self.held &= ~PAD.get(b, 0)

    def trigger(self, which: str, on: bool) -> None:
        pass

    def stick_motion(self, sx: float, sy: float) -> None:
        t = 0.5
        self.stick = (LEFT if sx < -t else RIGHT if sx > t else 0) | (ROTATE if sy < -t else DOWN if sy > t else 0)

    def key_down(self, k: int) -> None:
        self.held |= KEYS.get(k, 0)

    def key_up(self, k: int) -> None:
        self.held &= ~KEYS.get(k, 0)

    def touch_down(self, pid, x, y) -> None:
        pass

    def touch_move(self, pid, x, y) -> None:
        pass

    def touch_up(self, pid, x, y) -> None:
        pass

    def host_pause(self) -> None:
        self.held = self.stick = 0

    def rumble(self, low, high, ms) -> None:
        if self.rumble_fn:
            try:
                self.rumble_fn(low, high, ms)
            except Exception:
                pass

    def tap(self, bits: int) -> None:
        """A touch gesture: these keys pressed for one tick (then let go)."""
        self.pulse |= bits


_fonts: dict = {}


def font(size: int) -> pygame.font.Font:
    f = _fonts.get(size)
    if f is None:
        f = _fonts[size] = pygame.font.Font(None, size)
        f.set_bold(True)
    return f


def text(surf, s, pos, size, col, anchor="center") -> pygame.Rect:
    img = font(size).render(s, True, col)
    r = img.get_rect(**{anchor: pos})
    surf.blit(img, r)
    return r


def close_button(surf, vw) -> pygame.Rect:
    r = pygame.Rect(vw - 70, 14, 52, 44)
    pygame.draw.rect(surf, (32, 39, 51), r, border_radius=10)
    pygame.draw.line(surf, (140, 150, 168), (r.x + 16, r.y + 12), (r.right - 16, r.bottom - 12), 4)
    pygame.draw.line(surf, (140, 150, 168), (r.x + 16, r.bottom - 12), (r.right - 16, r.y + 12), 4)
    return r
