"""Baketris on the Deck: GameView.java's screen, ported to pygame.

The look (seven skins, each with its own blocks, particles, line-clear flash,
ambient life and sounds), the effects (screen shake, kick, the quad's kaboom:
punch zoom, shock waves, fire, hitstop, ducking), ghost, hold, next queue, the
menus, the settings (DAS, ARR, soft drop, shake, skin, music, ...) and the
controller mapping are the Android build's. The rules are rules.py's.

Drawn for 60 fps in Python: blocks, particles and translucent shapes are
pre-rendered sprites (antialiased by drawing at 4x and scaling down once), text
renders are cached, and the board is one surface per skin. Sound comes from
audio.py's cache; nothing is synthesized here."""

from __future__ import annotations

import json
import math
import random
import time

import pygame

from ..paths import data_dir
from . import audio as A
from . import blast
from . import rules as G

S_MENU, S_SETTINGS, S_READY, S_PLAY, S_PAUSE, S_OVER, S_CONTROLS = range(7)
A_LEFT, A_RIGHT, A_SOFT, A_HARD, A_CW, A_CCW, A_180, A_HOLD, A_PAUSE = range(1, 10)


def _c(argb: int) -> tuple:
    return ((argb >> 16) & 255, (argb >> 8) & 255, argb & 255)


SKIN_COLORS = [[_c(x) for x in row] for row in (
    (0xFF36D6E7, 0xFFF7C948, 0xFFA66CFF, 0xFF4ADE80, 0xFFF8566B, 0xFF4C7DFF, 0xFFFF9442),
    (0xFF0F380F, 0xFF306230, 0xFF0F380F, 0xFF306230, 0xFF0F380F, 0xFF306230, 0xFF4A7A26),
    (0xFF5EF2FF, 0xFFFFE066, 0xFFD08CFF, 0xFF7CFF9E, 0xFFFF6B8B, 0xFF6E9BFF, 0xFFFFB35C),
    (0xFF8EE3E0, 0xFFFFE89A, 0xFFCFA8F5, 0xFF9FF0B8, 0xFFFF9FB2, 0xFF94B8FF, 0xFFFFC98F),
    (0xFFFFB347, 0xFFFFE066, 0xFFFF5E3A, 0xFFE8461E, 0xFFC0261A, 0xFFFF8A2B, 0xFFFFCB52),
    (0xFF7DF9FF, 0xFFFFF1A8, 0xFFC77DFF, 0xFF72F2C0, 0xFFFF7AC6, 0xFF7D9BFF, 0xFFFFB27D),
    (0xFF39FF14, 0xFF00CC44, 0xFF7CFC00, 0xFF00FF88, 0xFF22AA22, 0xFF66FF66, 0xFF00E676))]
SKIN_L = ["NEON", "RETRO", "GLASS", "CANDY", "LAVA", "COSMOS", "CODE"]
SKIN_BOARD = [_c(x) for x in (0xFF0B0E16, 0xFF9BBC0F, 0xFF04050A, 0xFF2A2440, 0xFF140806, 0xFF05031A, 0xFF000800)]
SKIN_GRID = [_c(x) for x in (0xFF151A27, 0xFF8EAE0E, 0xFF0E1220, 0xFF352E52, 0xFF26100A, 0xFF100C2E, 0xFF002200)]
SKIN_BG = [_c(x) for x in (0xFF070910, 0xFF0F2A0F, 0xFF000000, 0xFF1A1630, 0xFF0A0302, 0xFF020010, 0xFF000000)]
SKIN_WAVES = [[_c(x) for x in row] for row in (
    (0xFFFFFFFF, 0xFFFF9A3C, 0xFFF7C948), (0xFF0F380F, 0xFF306230, 0xFF8BAC0F), (0xFFFFFFFF, 0xFF9FF6FF, 0xFFD08CFF),
    (0xFFFFFFFF, 0xFFFF9FB2, 0xFF8EE3E0), (0xFFFFF1C0, 0xFFFF6A1A, 0xFFC0261A), (0xFFFFFFFF, 0xFFC77DFF, 0xFF7DF9FF),
    (0xFFB8FFB0, 0xFF39FF14, 0xFF00AA33))]
SKIN_GRAV = [1.0, 1.0, 1.25, -0.3, -0.28, 0.05, 0.45]
GLYPHS = "01ABCDEF0123456789<>/=*+"
SDF = [5, 10, 20, 40, 41]
SDF_L = ["5x", "10x", "20x", "40x", "INSTANT"]
ARRS = [0, 8, 16, 25, 33, 50, 66, 83, 100]
SHAKE_L = ["OFF", "LOW", "MAX"]
SHAKE_M = [0.0, 0.55, 1.0]
MODE_NAMES = ["MARATHON", "SPRINT 40L", "ULTRA 2:00", "BAKETRIS"]
NMODES = 4
SETTINGS_ROWS = ["CONTROLS", "D-PAD UP", "SKIN", "MUSIC", "SCREEN SHAKE", "SOUND", "HAPTICS", "GHOST", "DRAG SPEED",
                 "DAS (PAD)", "ARR (PAD)", "SOFT DROP (PAD)", "ANNOUNCER", "ANNOUNCER VOLUME", "BACK"]
CHAIN_MAX = 3          # BAKETRIS mode: blasts in a row at most (a settle can fill rows again)
GREY, ACCENT, GOLD = _c(0xFF7F8AA3), _c(0xFF36D6E7), _c(0xFFF7C948)
WHITE, BLACK = (255, 255, 255), (0, 0, 0)
PMAX, WMAX, AMB = 1600, 8, 70
SONG_NAMES = ["KOROBEINIKI", "MOUNTAIN KING", "MINUET IN G", "ODE TO JOY"]
SONGS = len(SONG_NAMES)
SHUFFLE, CYCLE, MUSIC_OFF = SONGS, SONGS + 1, SONGS + 2
PBTN_ACT = [A_CW, A_CCW, A_HOLD, A_HARD]
PART_KIND = {0: "sq", 2: "glass", 3: "candy", 4: "lava", 6: "glyph"}     # 1: snapped squares, 5: streaks
GLASS_ANGLES = 12
DOTS_MAX = 16000
PBTN_L = ["A", "B", "HOLD", "DROP"]
DENSITY = 1.5          # the Deck's 7" 1280x800 against Android's dp
SS = 4                 # sprites are drawn this much bigger, then scaled down

# The Android build's controller mapping (GameView.keyAction), by SDL controller button name.
PAD_ACTION = {"dpleft": A_LEFT, "dpright": A_RIGHT, "dpdown": A_SOFT, "dpup": A_HARD,
              "a": A_CW, "b": A_CCW, "y": A_180, "x": A_HOLD, "leftshoulder": A_HOLD, "rightshoulder": A_HOLD,
              "start": A_PAUSE, "back": A_PAUSE}
KEY_ACTION = {pygame.K_LEFT: A_LEFT, pygame.K_RIGHT: A_RIGHT, pygame.K_DOWN: A_SOFT, pygame.K_s: A_SOFT,
              pygame.K_UP: A_HARD, pygame.K_SPACE: A_HARD, pygame.K_x: A_CW, pygame.K_RETURN: A_CW,
              pygame.K_z: A_CCW, pygame.K_LCTRL: A_CCW, pygame.K_a: A_180, pygame.K_c: A_HOLD,
              pygame.K_LSHIFT: A_HOLD, pygame.K_RSHIFT: A_HOLD, pygame.K_ESCAPE: A_PAUSE, pygame.K_p: A_PAUSE}

CONTROLS = [
    ("D-pad left / right, left stick", "move (hold: DAS, then ARR)"),
    ("D-pad down, stick down", "soft drop"),
    ("D-pad up, stick up, R2", "hard drop"),
    ("A", "rotate clockwise"),
    ("B", "rotate counter-clockwise"),
    ("Y", "rotate 180"),
    ("X, L1, R1, L2", "hold"),
    ("START, VIEW", "pause"),
    ("Menus", "d-pad, A pick, B back, left/right change"),
    ("Touch", "drag move, tap rotate, swipe up hold, down drop"),
    ("Hold VIEW + START", "leave the Rook remote"),
]


def blend(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def fmt_time(ms: float) -> str:
    t = int(max(0, ms))
    return f"{t // 60000}:{(t // 1000) % 60:02d}.{(t // 10) % 100:02d}"


def fmt_num(v: int) -> str:
    return f"{v:,}"


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# -- sprites ----------------------------------------------------------------------------

class _Pen:
    """Draws Canvas-style primitives (float coordinates, per-primitive alpha, centred
    strokes) into a sprite, SS times bigger; done() scales it down (antialiasing)."""

    def __init__(self, w: float, h: float, ox: float = 0, oy: float = 0):
        self.w, self.h = max(1, math.ceil(w)), max(1, math.ceil(h))
        self.ox, self.oy = ox, oy
        self.big = pygame.Surface((self.w * SS, self.h * SS), pygame.SRCALPHA)

    def _r(self, l, t, r, b):
        return pygame.Rect(round((l + self.ox) * SS), round((t + self.oy) * SS),
                           max(1, round((r - l) * SS)), max(1, round((b - t) * SS)))

    def _layer(self, alpha):
        if alpha >= 255:
            return self.big, None
        return pygame.Surface(self.big.get_size(), pygame.SRCALPHA), alpha

    def _done_layer(self, lay, alpha):
        if alpha is not None:
            lay.set_alpha(int(alpha))
            self.big.blit(lay, (0, 0))

    def rect(self, l, t, r, b, col, alpha=255, rad=0.0, stroke=0.0):
        lay, a = self._layer(alpha)
        if stroke:
            h = stroke / 2
            rr = self._r(l - h, t - h, r + h, b + h)
            pygame.draw.rect(lay, col, rr, max(1, round(stroke * SS)), border_radius=round((rad + h) * SS) if rad else 0)
        else:
            pygame.draw.rect(lay, col, self._r(l, t, r, b), 0, border_radius=round(rad * SS) if rad else 0)
        self._done_layer(lay, a)

    def oval(self, l, t, r, b, col, alpha=255):
        lay, a = self._layer(alpha)
        pygame.draw.ellipse(lay, col, self._r(l, t, r, b))
        self._done_layer(lay, a)

    def circle(self, cx, cy, rad, col, alpha=255, stroke=0.0):
        lay, a = self._layer(alpha)
        c = ((cx + self.ox) * SS, (cy + self.oy) * SS)
        if stroke:
            pygame.draw.circle(lay, col, c, (rad + stroke / 2) * SS, max(1, round(stroke * SS)))
        else:
            pygame.draw.circle(lay, col, c, rad * SS)
        self._done_layer(lay, a)

    def line(self, x1, y1, x2, y2, col, width, alpha=255):
        lay, a = self._layer(alpha)
        p1 = pygame.Vector2((x1 + self.ox) * SS, (y1 + self.oy) * SS)
        p2 = pygame.Vector2((x2 + self.ox) * SS, (y2 + self.oy) * SS)
        d = p2 - p1
        if d.length() > 0:
            n = pygame.Vector2(-d.y, d.x).normalize() * (width * SS / 2)
            pygame.draw.polygon(lay, col, [p1 + n, p2 + n, p2 - n, p1 - n])
            pygame.draw.circle(lay, col, p1, width * SS / 2)
            pygame.draw.circle(lay, col, p2, width * SS / 2)
        self._done_layer(lay, a)

    def poly(self, pts, col, alpha=255):
        lay, a = self._layer(alpha)
        pygame.draw.polygon(lay, col, [((x + self.ox) * SS, (y + self.oy) * SS) for x, y in pts])
        self._done_layer(lay, a)

    def text(self, s, cx, baseline, size, col, alpha=255):
        f = _font(size * SS)
        img = f.render(s, True, col)
        if alpha < 255:
            img.set_alpha(int(alpha))
        self.big.blit(img, img.get_rect(midleft=(0, 0)).move(
            round((cx + self.ox) * SS - img.get_width() / 2), round((baseline + self.oy) * SS - f.get_ascent())).topleft)

    def done(self) -> pygame.Surface:
        return pygame.transform.smoothscale(self.big, (self.w, self.h))


_fonts: dict = {}


def _font(px: float) -> pygame.font.Font:
    """Android's textSize (the em, in px) to pygame's default font (its size is ~1.45 em)."""
    k = max(4, int(px * 1.4))
    f = _fonts.get(k)
    if f is None:
        f = pygame.font.Font(None, k)
        f.set_bold(True)
        _fonts[k] = f
    return f


class Baketris(G.Listener):
    def __init__(self, size=(1280, 800), audio: A.Audio | None = None, rumble=None, prefs_path=None,
                 seed: int | None = None):
        self.vw, self.vh = size
        self.audio = audio or A.Audio(enabled=False)
        self.rumble_fn = rumble
        self.prefs_path = prefs_path or (data_dir() / "baketris.json")
        self.rnd = random.Random(seed)
        self.game = G.Game()
        self.game.listener = self
        self.done = False
        self.state, self.mode, self.sel, self.menu_down = S_MENU, 0, 0, -1
        p = self._load_prefs()
        self.das = int(p.get("das", 150))
        self.arr_idx = clamp(int(p.get("arr", 4)), 0, len(ARRS) - 1)
        self.sdf_idx = clamp(int(p.get("sdf", 2)), 0, len(SDF) - 1)
        self.shake_lvl = clamp(int(p.get("shake", 2)), 0, 2)
        self.drag_spd = clamp(int(p.get("drag", 10)), 5, 20)
        self.skin = clamp(int(p.get("skin", 0)), 0, len(SKIN_L) - 1)
        self.music_sel = clamp(int(p.get("song", 0)), 0, SONGS + 2)
        self.pad_mode = bool(p.get("pad", False))
        self.up_drop = bool(p.get("updrop", False))
        self.ghost = bool(p.get("ghost", True))
        self.sound = bool(p.get("sound", True))
        self.haptics = bool(p.get("haptics", True))
        self.announcer = bool(p.get("announcer", True))
        self.ann_vol = clamp(int(p.get("annvol", 4)), 1, 4)          # quarters
        self.best = [int(p.get(f"best{i}", 0)) for i in range(NMODES)]
        self.music = self.audio.music
        self.cur_song, self.song_toast_t, self.song_shown = 0, 0.0, -1
        self.previewing = False
        self.last_drop_t = 0.0
        self.pad_connected = True               # the Deck's own controller
        # input
        self.left_held = self.right_held = self.soft_held = self.charged = False
        self.last_dir = 0
        self.das_t = self.arr_t = 0.0
        self.ptrs: list = []
        self.joy_x = self.joy_y = 0
        self.stick = [0.0, 0.0]
        self.trig_l = self.trig_r = False
        self.dpad_pid, self.dpad_dir = None, 0
        self.pbtn_pid = [None] * 4
        # effects
        self.ready_t = self.anim_t = self.action_t = self.flash_t = self.trail_t = self.pc_t = 0.0
        self.over_t = self.hitstop_t = self.screen_flash_t = self.fire_t = self.duck_t = self.quad_t = 0.0
        self.trauma = self.kick_y = self.sh_x = self.sh_y = self.punch = 0.0
        self.music_rate = 1.0
        self.go_played = self.move_snd = self.new_best = self.danger = False
        self.action, self.action_col, self.flash_rows = [], [], []
        self.tr = (0, 0, 0, 0, 0)
        self.last_level = 1
        self.parts: list = []                   # [x, y, vx, vy, life, max, size, col, rot, spin]
        self.waves: list = []                   # [x, y, r, t, dur, col]
        self.amb = [[self.rnd.random(), self.rnd.random(), 0.3 + self.rnd.random(), self.rnd.random(),
                     self.rnd.choice(GLYPHS)] for _ in range(AMB)]
        self.menu_rects: list = []
        self.close_rect = None
        self._cache: dict = {}
        self._dots: dict = {}
        self._text: dict = {}
        self._warm = None
        self.game.reset(G.MARATHON, self._seed())
        self.layout()
        self.apply_skin()
        self.layer = pygame.Surface((self.vw, self.vh))
        self.overlay = pygame.Surface((self.vw, self.vh), pygame.SRCALPHA)
        self.tint = pygame.Surface((self.vw, self.vh))

    # -- prefs -------------------------------------------------------------------------
    def _load_prefs(self) -> dict:
        try:
            with open(self.prefs_path) as f:
                p = json.load(f)
            return p if isinstance(p, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_prefs(self) -> None:
        p = {"das": self.das, "arr": self.arr_idx, "sdf": self.sdf_idx, "shake": self.shake_lvl, "drag": self.drag_spd,
             "skin": self.skin, "song": self.music_sel, "pad": self.pad_mode, "updrop": self.up_drop,
             "ghost": self.ghost, "sound": self.sound, "haptics": self.haptics,
             "announcer": self.announcer, "annvol": self.ann_vol}
        for i in range(NMODES):
            p[f"best{i}"] = self.best[i]
        try:
            tmp = self.prefs_path.with_name(self.prefs_path.name + ".tmp")
            tmp.write_text(json.dumps(p))
            tmp.replace(self.prefs_path)
        except OSError:
            pass

    def _seed(self) -> int:
        return time.time_ns()

    # -- layout (GameView.layout, the landscape branch: the Deck's screen) --------------
    def layout(self) -> None:
        d = DENSITY
        w, h = self.vw, self.vh
        top, bot, left, right = 8 * d, h - 8 * d, 8 * d, w - 8 * d
        aw = right - left
        self.cell = c = int(min((bot - top) / 21.6, aw / 26))
        self.bx = int(left + (aw - 10 * c) / 2)
        self.by = int(top + ((bot - top) - 20 * c) * 0.6)
        self.mc = c * 0.62
        bx, by, mc = self.bx, self.by, self.mc
        pw = min(bx - left - c * 0.4, c * 5)
        self.hold_box = (bx - c * 0.4 - pw, by + c * 0.8, bx - c * 0.4, by + c * 0.8 + mc * 3.4)
        self.next_box = (bx + 10 * c + c * 0.4, by + c * 0.8, bx + 10 * c + c * 0.4 + pw,
                         by + c * 0.8 + mc * (9.6 if self.pad_mode else 15.6))
        self.stats_top = self.hold_box[3] + c * 0.9
        self.dr = 0
        self.pbtn = [[0, 0, 0] for _ in range(4)]
        if self.pad_mode:
            side = bx - left
            self.dcx, self.dcy, self.dr = left + side * 0.5, by + 15.5 * c, min(side * 0.4, 4.2 * c)
            rx = bx + 10 * c
            rw = right - rx
            br = min(rw * 0.16, 1.6 * c)
            self.pbtn = [[rx + rw * 0.72, by + 14.6 * c, br], [rx + rw * 0.36, by + 17.2 * c, br],
                         [rx + rw * 0.72, by + 11.4 * c, br * 0.6], [rx + rw * 0.3, by + 11.4 * c, br * 0.6]]

    def apply_skin(self) -> None:
        self.col = SKIN_COLORS[self.skin]
        self.light = [blend(x, WHITE, 0.25 if self.skin == 1 else 0.45) for x in self.col]
        self.dark = [blend(x, BLACK, 0.35) for x in self.col]
        self.audio.use(self.skin)
        self._cache.clear()                       # the last skin's sprites
        self._dots.clear()
        c = self.cell
        b = pygame.Surface((10 * c, 20 * c))
        b.fill(SKIN_BOARD[self.skin])
        if self.skin != 1:
            for i in range(1, 10):
                pygame.draw.line(b, SKIN_GRID[self.skin], (i * c, 0), (i * c, 20 * c), 1)
            for j in range(1, 20):
                pygame.draw.line(b, SKIN_GRID[self.skin], (0, j * c), (10 * c, j * c), 1)
        self.board = b
        self._warm = self._warm_sprites()

    # -- sprites -----------------------------------------------------------------------
    def _block(self, s: int, t: int, x: float = 0, y: float = 0) -> tuple:
        """(sprite, margin) of GameView.block() at full alpha, size s; LAVA's pulse and
        CODE's glyph (both from the position) are quantized into the key."""
        skin = self.skin
        extra = None
        if skin == 4:
            pulse = 0.75 + 0.25 * math.sin(self.anim_t / 220.0 + x * 0.05 + y * 0.03)
            extra = round(pulse * 16)
        elif skin == 6:
            extra = int((x * 7 + y * 13) / max(1, s)) % len(GLYPHS)
        key = ("b", skin, s, t, extra)
        v = self._cache.get(key)
        if v is None:
            v = self._make_block(s, t, extra)
            self._cache[key] = v
        return v

    def _make_block(self, s: int, t: int, extra) -> tuple:
        m = math.ceil(s * 0.15) + 1
        p = _Pen(s + 2 * m, s + 2 * m, m, m)
        g = max(1.0, s * 0.06)
        col, light, dark = self.col[t], self.light[t], self.dark[t]
        skin = self.skin
        if skin == 1:
            p.rect(g * .5, g * .5, s - g * .5, s - g * .5, col)
            p.rect(s * .2, s * .2, s * .8, s * .8, blend(col, SKIN_BOARD[1], 0.55))
            p.rect(s * .38, s * .38, s * .62, s * .62, col)
        elif skin == 2:
            rad, sw = s * .12, max(1.5, s * .08)
            p.rect(g, g, s - g, s - g, col, 60, rad)
            p.rect(g, g, s - g, s - g, col, 50, rad, stroke=sw * 2.4)
            p.rect(g + sw / 2, g + sw / 2, s - g - sw / 2, s - g - sw / 2, light, 255, rad, stroke=sw)
            p.rect(s * .22, s * .2, s * .55, s * .27, WHITE, 90)
        elif skin == 3:
            rad = s * .3
            p.rect(g, g, s - g, s - g, col, 255, rad)
            p.rect(g * 2, s * .6, s - g * 2, s - g * 1.5, dark, 70, rad * .7)
            p.oval(s * .2, s * .16, s * .5, s * .36, WHITE, 120)
        elif skin == 4:
            rad = s * .1
            pulse = extra / 16
            p.rect(g, g, s - g, s - g, blend(col, BLACK, 0.62), 255, rad)
            p.rect(s * .2, s * .2, s * .8, s * .8, col, 255 * pulse, rad)
            lw = max(1.0, s * .05)
            p.line(s * .2, s * .5, s * .45, s * .62, light, lw, 255 * pulse)
            p.line(s * .45, s * .62, s * .8, s * .42, light, lw, 255 * pulse)
        elif skin == 5:
            p.circle(s / 2, s / 2, s * .62, col, 50)
            p.rect(g * 1.5, g * 1.5, s - g * 1.5, s - g * 1.5, col, 210, s * .22)
            p.circle(s * .5, s * .5, s * .13, WHITE, 200)
        elif skin == 6:
            p.rect(g, g, s - g, s - g, _c(0xFF001A08))
            p.rect(g, g, s - g, s - g, col, 255, 0, stroke=max(1.0, s * .06))
            p.text(GLYPHS[extra], s / 2, s * .72, s * .62, col)
        else:
            rad = s * .14
            p.rect(g, g, s - g, s - g, col, 255, rad)
            p.rect(g * 2, g * 1.6, s - g * 2, g + s * .17, light, 190, rad * .6)
            p.rect(g * 1.5, s - g - s * .14, s - g * 1.5, s - g * 1.4, dark, 160, rad * .6)
        return p.done(), m

    def block(self, surf, x: float, y: float, s: int, t: int, alpha: int = 255) -> None:
        spr, m = self._block(s, t, x, y)
        if alpha < 255:
            spr.set_alpha(alpha)
            surf.blit(spr, (round(x) - m, round(y) - m))
            spr.set_alpha(255)
        else:
            surf.blit(spr, (round(x) - m, round(y) - m))

    def ghost_block(self, surf, x, y, s, t) -> None:
        key = ("g", self.skin, s, t)
        spr = self._cache.get(key)
        if spr is None:
            g, rad, sw = max(1.0, s * .06), 0 if self.skin == 1 else s * .14, max(1.5, s * .07)
            p = _Pen(s, s)
            p.rect(g, g, s - g, s - g, self.col[t], 60 if self.skin == 1 else 34, rad)
            p.rect(g + sw / 2, g + sw / 2, s - g - sw / 2, s - g - sw / 2, self.col[t], 150, rad, stroke=sw)
            spr = self._cache[key] = p.done()
        surf.blit(spr, (round(x), round(y)))

    def mini(self, surf, t, cx, cy, s, alpha) -> None:
        cl = G.CELLS[t][0]
        min_c = min(q[0] for q in cl)
        max_c = max(q[0] for q in cl)
        min_r = min(q[1] for q in cl)
        max_r = max(q[1] for q in cl)
        si = max(2, round(s))
        w, h = (max_c - min_c + 1) * si, (max_r - min_r + 1) * si
        x0, y0 = cx - w / 2 - min_c * si, cy - h / 2 - min_r * si
        for q in cl:
            self.block(surf, x0 + q[0] * si, y0 + q[1] * si, si, t, alpha)

    def _alpha_shape(self, kind, w, h, col, rad=0) -> pygame.Surface:
        """A filled shape (rect, rounded rect: kind 'r'; circle: 'c') at full alpha, cached; set_alpha to draw."""
        key = ("s", kind, round(w), round(h), col, round(rad))
        spr = self._cache.get(key)
        if spr is None:
            if kind == "r" and not rad:
                spr = self._solid(w, h, col)
            else:
                p = _Pen(w, h)
                if kind == "c":
                    p.circle(w / 2, h / 2, w / 2, col)
                else:
                    p.rect(0, 0, w, h, col, 255, rad)
                spr = p.done()
            self._cache[key] = spr
        return spr

    def _solid(self, w, h, col) -> pygame.Surface:
        s = pygame.Surface((max(1, round(w)), max(1, round(h))))
        s.fill(col)
        return s

    def fill_a(self, surf, l, t, r, b, col, alpha, rad=0) -> None:
        """A translucent rect (or rounded rect) with Canvas coordinates."""
        if alpha <= 0 or r <= l or b <= t:
            return
        spr = self._alpha_shape("r", r - l, b - t, col, rad)
        spr.set_alpha(int(min(255, alpha)))
        surf.blit(spr, (round(l), round(t)))

    # -- text --------------------------------------------------------------------------
    def _render(self, s: str, size: float, col) -> tuple:
        key = (s, round(size * 2), col)
        v = self._text.get(key)
        if v is None:
            if len(self._text) > 600:
                self._text.clear()
            f = _font(size)
            v = self._text[key] = (f.render(s, True, col), f.get_ascent(), f.get_descent())
        return v

    def text(self, surf, s, x, y, size, col, align="left", alpha=255) -> float:
        """Canvas.drawText: y is the baseline. Answers the width."""
        img, asc, _ = self._render(s, size, col)
        w = img.get_width()
        lx = x if align == "left" else x - w / 2 if align == "center" else x - w
        if alpha < 255:
            img.set_alpha(max(0, int(alpha)))
            surf.blit(img, (round(lx), round(y - asc)))
            img.set_alpha(255)
        else:
            surf.blit(img, (round(lx), round(y - asc)))
        return w

    def _text_c(self, surf, s, x, cy, size, col, align, alpha) -> float:
        _, asc, desc = self._render(s, size, col)
        # Canvas: baseline = cy - (descent + ascent) / 2 with ascent < 0; pygame's ascent is > 0, descent < 0
        return self.text(surf, s, x, cy + (asc + desc) / 2, size, col, align, alpha)

    def fit_text(self, surf, s, cx, cy, size, max_w, col) -> None:
        w = _font(size).size(s)[0] / 1.0
        if w > max_w:
            size *= max_w / w
        self._text_c(surf, s, cx, cy, size, col, "center", 255)

    def pps(self) -> str:
        g = self.game
        return f"{g.pieces / (g.time_ms / 1000.0):.2f}" if g.time_ms > 0 else "0.00"

    # -- game flow ---------------------------------------------------------------------
    def start_game(self, m: int) -> None:
        self.mode = m
        self.seed = self._seed()
        self.game.reset(m, self.seed)
        self.blast = None             # BAKETRIS mode: the blast flying (blast.Blast), and what waits for it
        self.blast_pending = None
        self.stash = None
        self.chain = 0
        self.clear_inputs()
        self.state = S_READY
        self.ready_t = 1300.0
        self.go_played = self.new_best = self.previewing = False
        self.action = []
        self.action_t = self.flash_t = self.trail_t = self.pc_t = self.over_t = self.hitstop_t = 0.0
        self.screen_flash_t = self.fire_t = self.duck_t = self.quad_t = 0.0
        self.trauma = self.kick_y = self.punch = 0.0
        self.parts, self.waves = [], []
        self.last_level = 1
        self.music_rate = 1.0
        self.cur_song = self.rnd.randrange(SONGS) if self.music_sel in (SHUFFLE, CYCLE) else min(self.music_sel, SONGS - 1)
        self.music.set_cycle(self.music_sel == CYCLE)
        self.song_shown, self.song_toast_t = -1, 0.0
        self.music.select(self.cur_song)
        self.music.restart()
        self.music.set_rate(1.0)
        self.music.set_volume(0.55)
        if self.sound:
            self.audio.play_shared("tick", 0.6)

    def pause_game(self) -> None:
        self.state, self.sel, self.menu_down = S_PAUSE, 0, -1
        self.clear_inputs()
        self.music.pause()

    def resume_game(self) -> None:
        self.state, self.ready_t, self.go_played = S_READY, 450.0, False
        self.clear_inputs()

    def clear_inputs(self) -> None:
        self.left_held = self.right_held = self.soft_held = False
        self.last_dir = 0
        self.ptrs = []
        self.dpad_pid, self.dpad_dir = None, 0
        self.pbtn_pid = [None] * 4
        self.joy_x = self.joy_y = 0

    def host_pause(self) -> None:
        """The window lost focus (Steam's overlay, another app): GameView.hostPause."""
        if self.state in (S_PLAY, S_READY):
            self.pause_game()
        self.music.pause()
        self.previewing = False
        self.clear_inputs()

    def leave(self) -> None:
        """Back to the Rook remote's Games tab (Android: back on the main menu)."""
        self.music.pause()
        self.previewing = False
        self.done = True

    def update(self, dt: float) -> None:
        dt = min(100.0, dt)
        self.anim_t += dt
        self.move_snd = False
        g = self.game
        hi = -1
        for r in range(G.H - 1, -1, -1):
            if any(g.grid[r]):
                hi = r
                break
        self.danger = hi >= 15 and self.state in (S_PLAY, S_READY)
        if self.state == S_READY:
            self.ready_t -= dt
            if self.ready_t <= 500 and not self.go_played:
                self.go_played = True
                if self.sound:
                    self.audio.play_shared("go", 0.7)
            if self.ready_t <= 0:
                self.state = S_PLAY
                if g.cur < 0:
                    g.next_piece()
                if self.music_sel != MUSIC_OFF:
                    self.music.play()
        elif self.state == S_PLAY:
            if self.blast is not None or self.blast_pending is not None:
                self.tick_blast(dt)
            elif self.hitstop_t > 0:
                self.hitstop_t -= dt
            else:
                self.tick_das(dt)
                g.update(dt, self.soft_held, SDF[self.sdf_idx])
            cs = self.music.current()
            if cs != self.song_shown and cs >= 0:
                self.song_shown = cs
                if self.music_sel != MUSIC_OFF:
                    self.song_toast_t = 2600.0
            target = 1 + (min(0.4, (g.level - 1) * 0.028) if self.mode in (G.MARATHON, G.BAKETRIS) else 0) + (0.07 if self.danger else 0)
            if abs(target - self.music_rate) > 0.004:
                self.music_rate = target
                self.music.set_rate(target)
        if self.duck_t > 0:
            self.duck_t = max(0.0, self.duck_t - dt)
            self.music.set_volume(0.55 * (1 - 0.8 * (self.duck_t / 1400)))
        if self.move_snd and self.sound:
            self.audio.play(A.MOVE, 0.5)
        for k in ("action_t", "flash_t", "trail_t", "pc_t", "quad_t", "song_toast_t", "screen_flash_t", "fire_t"):
            setattr(self, k, max(0.0, getattr(self, k) - dt))
        if self.state == S_OVER:
            self.over_t += dt
        s = dt / 1000.0
        self.trauma = max(0.0, self.trauma - s * 1.3)
        m = self.trauma * self.trauma * self.cell * 0.85
        self.sh_x = m * (self.rnd.random() * 2 - 1)
        self.sh_y = m * (self.rnd.random() * 2 - 1)
        self.kick_y *= 0.0006 ** s
        if abs(self.kick_y) < 0.3:
            self.kick_y = 0.0
        self.punch *= 0.002 ** s
        if self.punch < 0.001:
            self.punch = 0.0
        self.tick_particles(dt)
        live = []
        for w in self.waves:
            w[3] += dt
            if w[3] < w[4]:
                live.append(w)
        self.waves = live
        self.music.tick()
        self.audio.tick_announcer()
        self.warm(1.5)

    def add_trauma(self, t: float) -> None:
        self.trauma = min(1.0, self.trauma + t * SHAKE_M[self.shake_lvl])

    def tick_das(self, dt: float) -> None:
        d = self.last_dir
        if d == -1 and not self.left_held:
            d = 0
        if d == 1 and not self.right_held:
            d = 0
        if d == 0:
            return
        self.das_t += dt
        if self.das_t < self.das:
            return
        arr = ARRS[self.arr_idx]
        g = self.game
        if not self.charged:
            self.charged, self.arr_t = True, 0.0
            if arr == 0:
                while g.shift(d):
                    pass
            else:
                g.shift(d)
            return
        if arr == 0:
            while g.shift(d):
                pass
            return
        self.arr_t += dt
        while self.arr_t >= arr:
            self.arr_t -= arr
            if not g.shift(d):
                self.arr_t = 0.0
                break

    def blasting(self) -> bool:
        return getattr(self, "blast", None) is not None or getattr(self, "blast_pending", None) is not None

    def press(self, a: int) -> None:
        if a == A_PAUSE:
            self.pause_game()
            return
        if self.blasting() or self.game.cur < 0:     # the piece is held off while the blast flies
            if a in (A_LEFT, A_RIGHT, A_SOFT):
                pass
            else:
                return
        g, play = self.game, self.state == S_PLAY and not self.blasting() and self.game.cur >= 0
        if a == A_LEFT:
            self.left_held, self.last_dir, self.das_t, self.charged = True, -1, 0.0, False
            if play:
                g.shift(-1)
        elif a == A_RIGHT:
            self.right_held, self.last_dir, self.das_t, self.charged = True, 1, 0.0, False
            if play:
                g.shift(1)
        elif a == A_SOFT:
            self.soft_held = True
        elif a == A_HARD:
            now = time.monotonic() * 1000
            if play and now - self.last_drop_t > 120:
                self.last_drop_t = now
                g.hard_drop()
        elif a == A_CW and play:
            g.rotate(1)
        elif a == A_CCW and play:
            g.rotate(-1)
        elif a == A_180 and play:
            g.rotate(2)
        elif a == A_HOLD and play:
            g.do_hold()

    def release(self, a: int) -> None:
        if a == A_LEFT:
            self.left_held = False
            if self.last_dir == -1:
                self.last_dir, self.das_t, self.charged = (1 if self.right_held else 0), 0.0, False
        elif a == A_RIGHT:
            self.right_held = False
            if self.last_dir == 1:
                self.last_dir, self.das_t, self.charged = (-1 if self.left_held else 0), 0.0, False
        elif a == A_SOFT:
            self.soft_held = False

    # -- particles & waves ---------------------------------------------------------------
    def spawn(self, x, y, vx, vy, life, size, col) -> None:
        if len(self.parts) >= PMAX:
            return
        r = self.rnd.random
        self.parts.append([x, y, vx, vy, life, life, size, col, r() * 6.28, (r() - 0.5) * 20])

    def wave(self, x, y, r, dur, col, delay) -> None:
        if len(self.waves) < WMAX:
            self.waves.append([x, y, r, -delay, dur, col])

    def tick_particles(self, dt: float) -> None:
        s = dt / 1000.0
        g = self.cell * 38 * SKIN_GRAV[self.skin] * s
        skin = self.skin
        floaty = skin in (3, 4)
        drag = max(0.0, 1 - (2.2 if floaty else 0.9) * s)
        candy = skin == 3
        wob = self.cell * 4 * s
        sin = math.sin
        live = []
        for i, p in enumerate(self.parts):
            life = p[4] - dt
            if life <= 0:
                continue
            p[4] = life
            vy = p[3] + g
            vx = p[2] * drag
            if floaty:
                vy *= drag
            if candy:
                vx += sin(life / 90.0 + i) * wob
            p[2], p[3] = vx, vy
            p[0] += vx * s
            p[1] += vy * s
            p[8] += p[9] * s
            live.append(p)
        self.parts = live
        self.tick_ambient(dt)

    def tick_ambient(self, dt: float) -> None:
        s = dt / 1000.0
        skin = self.skin
        if skin < 3:
            return
        r = self.rnd.random
        for a in self.amb:
            v = a[2]
            if skin == 5:
                a[1] += v * 0.03 * s
            elif skin == 4:
                a[1] -= v * 0.06 * s
            elif skin == 6:
                a[1] += v * 0.25 * s
                if r() < 0.02:
                    a[4] = self.rnd.choice(GLYPHS)
            elif skin == 3:
                a[1] -= v * 0.03 * s
            if a[1] > 1:
                a[1] -= 1
                a[0] = r()
            if a[1] < 0:
                a[1] += 1
                a[0] = r()

    def _dot(self, kind, col, size, alpha, extra=None) -> tuple:
        """A particle sprite: (surface, half w, half h); size in px, alpha 0..255 (quantized here)."""
        si = max(1, int(size + 0.5))
        aq = max(1, min(16, int(alpha / 16 + 0.5)))
        if kind == "glyph":
            si = max(2, 3 * int(si / 3 + 0.5))
        elif kind == "glass" and si > 8:
            si = 2 * int(si / 2 + 0.5)
        if kind in ("glyph", "glass"):                # many shapes a size: fewer alphas
            aq = min(16, (aq + 3) // 4 * 4)
        key = (kind, col, si, aq, extra)
        return self._dots.get(key) or self._make_dot(key)

    def _make_dot(self, key) -> tuple:
        kind, col, si, aq, extra = key
        a = aq * 16 - 1
        if kind == "sq":
            spr = pygame.Surface((si, si), pygame.SRCALPHA)
            spr.fill((*col, a))
        elif kind == "glass":
            r = si * 0.8
            t = extra * (2 * math.pi / GLASS_ANGLES)
            d = math.ceil(r) + 1
            p = _Pen(2 * d, 2 * d, d, d)
            p.poly([(math.cos(t) * r, math.sin(t) * r), (math.cos(t + 2.4) * r * .55, math.sin(t + 2.4) * r * .55),
                    (math.cos(t + 4.0) * r, math.sin(t + 4.0) * r)], col, a)
            spr = p.done()
        elif kind == "candy":
            d = math.ceil(si * .55) + 1
            p = _Pen(2 * d, 2 * d, d, d)
            p.circle(0, 0, si * .55, col, a * 3 / 4)
            p.circle(-si * .18, -si * .18, si * .16, WHITE, a / 2)
            spr = p.done()
        elif kind == "lava":
            d = math.ceil(si * .9) + 1
            p = _Pen(2 * d, 2 * d, d, d)
            p.circle(0, 0, si * .9, col, a / 4)
            p.circle(0, 0, si * .35, col, a)
            spr = p.done()
        elif kind == "glyph":
            img = _font(si * 1.6).render(extra, True, col)
            img.set_alpha(a)
            spr = pygame.Surface(img.get_size(), pygame.SRCALPHA)
            spr.blit(img, (0, 0))
        elif kind == "ring":
            d = math.ceil(si) + 1
            p = _Pen(2 * d, 2 * d, d, d)
            p.circle(0, 0, si, col, a, stroke=extra)
            spr = p.done()
        else:                                   # "circle"
            d = math.ceil(si) + 1
            p = _Pen(2 * d, 2 * d, d, d)
            p.circle(0, 0, si, col, a)
            spr = p.done()
        if len(self._dots) > DOTS_MAX:             # bounded: start again (and make the common ones ahead again)
            self._dots.clear()
            self._warm = self._warm_sprites()
        v = self._dots[key] = (spr, spr.get_width() / 2, spr.get_height() / 2)
        return v

    def _warm_sprites(self):
        """The skin's particle sprites, made ahead (a frame's spare time at a time: update()),
        so the first quad doesn't stall on making hundreds of them."""
        skin = self.skin
        for t in range(7):                            # the BAKETRIS mode's flying blocks, at 16 angles
            for q in range(16):
                key = ("rot", skin, self.cell, t, q)
                if key not in self._cache:
                    self._cache[key] = pygame.transform.rotozoom(self._block(self.cell, t)[0], q * 22.5, 1.0)
                    yield None
        kind = PART_KIND.get(skin)
        if kind is None:
            return
        cols = list(dict.fromkeys(self.col + self.light + [WHITE, _c(0xFFB8C0D0)] + SKIN_WAVES[skin]))
        top = int(self.cell * 0.65) + 2
        for aq in range(16, 0, -1) if kind == "sq" else (16,) if kind == "glass" else (16, 14, 12):
            for col in cols:
                for si in range(1, top):
                    if kind == "glass":
                        if si > 8 and si % 2:
                            continue
                        for e in range(GLASS_ANGLES):
                            yield self._dot(kind, col, si, aq * 16, e)
                    elif kind == "glyph":
                        if si % 3 == 0:
                            for g in dict.fromkeys(GLYPHS):
                                yield self._dot(kind, col, si, aq * 16, g)
                    else:
                        yield self._dot(kind, col, si, aq * 16)

    def warm(self, budget_ms: float) -> None:
        if self._warm is None:
            return
        end = time.perf_counter() + budget_ms / 1000
        try:
            while time.perf_counter() < end:
                next(self._warm)
        except StopIteration:
            self._warm = None

    def draw_particles(self, surf, ox=0.0, oy=0.0) -> None:
        skin, cell = self.skin, self.cell
        get, dot = self._dots.get, self._dot
        kind = PART_KIND.get(skin)
        blits = []
        lines = []
        two_pi = 2 * math.pi
        nglyph = len(GLYPHS)
        for p in self.parts:
            f = p[4] / p[5]
            sz = p[6] * (0.35 + 0.65 * f)
            x, y, col = p[0] + ox, p[1] + oy, p[7]
            if skin == 5:
                a = 255 * f * 1.6 if f < 0.625 else 255
                lines.append((col, a, max(1.5, sz * 0.35), x, y, x - p[2] * 0.035, y - p[3] * 0.035))
                continue
            if skin == 1:
                q = max(2.0, cell / 8)
                x, y = round(x / q) * q, round(y / q) * q
                sz = max(q, round(p[6] / q) * q)
                v = dot("sq", col, sz, 255 if f > 0.66 else 170 if f > 0.33 else 85)
            else:
                aq = 16 if f >= 0.625 else max(1, int(f * 25.5 + 0.5))
                si = int(sz + 0.5) or 1
                if kind == "glass":
                    v = dot(kind, col, sz, aq * 16, int((p[8] % two_pi) / two_pi * GLASS_ANGLES) % GLASS_ANGLES)
                elif kind == "glyph":
                    v = dot(kind, col, sz, aq * 16, GLYPHS[int(abs(p[8]) * 3) % nglyph])
                else:
                    v = get((kind, col, si, aq, None)) or dot(kind, col, sz, aq * 16)
            spr, hw, hh = v
            blits.append((spr, (x - hw, y - hh)))
        if blits:
            surf.fblits(blits)
        if lines or self.waves:
            ov = self.overlay
            ov.fill((0, 0, 0, 0))
            for col, a, w, x1, y1, x2, y2 in lines:
                pygame.draw.line(ov, (*col, int(a)), (x1, y1), (x2, y2), max(1, round(w)))
            for x, y, r, t, dur, col in self.waves:
                if t < 0:
                    continue
                f = t / dur
                e = 1 - (1 - f) * (1 - f)
                width = cell * 0.7 * (1 - f) + 2 * DENSITY
                rad = r * e
                if rad > 1:
                    pygame.draw.circle(ov, (*col, int(230 * (1 - f))), (x + ox, y + oy), rad + width / 2,
                                       max(1, round(width)))
            surf.blit(ov, (0, 0))

    def draw_ambient(self, surf, ox, oy) -> None:
        skin = self.skin
        if skin < 3:
            return
        c = self.cell
        bx, by = self.bx + ox, self.by + oy
        w, h = 10 * c, 20 * c
        clip = surf.get_clip()
        surf.set_clip(pygame.Rect(round(bx), round(by), w, h))
        blits = []
        for i, a in enumerate(self.amb):
            x, y = bx + a[0] * w, by + a[1] * h
            if skin == 3:
                spr, hw, hh = self._dot("ring", WHITE, c * (0.15 + a[3] * 0.35), 30, DENSITY)
            elif skin == 4:
                spr, hw, hh = self._dot("circle", _c(0xFFFF6A1A) if a[3] > 0.5 else _c(0xFFFFB347),
                                        c * (0.03 + a[3] * 0.05), 30 + 100 * a[1])
            elif skin == 5:
                spr, hw, hh = self._dot("circle", WHITE, DENSITY * (0.6 + a[3] * 1.4),
                                        40 + 140 * a[3] * (0.6 + 0.4 * math.sin(self.anim_t / 300.0 + i)))
            else:
                spr, hw, hh = self._dot("glyph", _c(0xFF00FF66), c * 0.5 / 1.6, 25 + 40 * a[3], a[4])
                blits.append((spr, (x - hw, y - spr.get_height() * 0.75)))
                continue
            blits.append((spr, (x - hw, y - hh)))
        surf.fblits(blits)
        surf.set_clip(clip)
        if skin == 4:
            pulse = 0.8 + 0.2 * math.sin(self.anim_t / 400.0)
            for k in range(6):
                self.fill_a(surf, bx, by + h - (6 - k) * c * 0.5, bx + w, by + h, _c(0xFFFF4A10), 7 * (k + 1) * pulse)

    # -- the listener (rules.Game's events) ------------------------------------------------
    def on_move(self):
        self.move_snd = True

    def on_rotate(self):
        if self.sound:
            self.audio.play(A.ROT, 0.5)

    def on_hold(self):
        if self.sound:
            self.audio.play(A.HOLD, 0.6)
        self.buzz(0)

    def row_y(self, r: int) -> float:
        return self.by + (G.VIS - 1 - r) * self.cell

    def on_hard_drop(self, type_, rot, x, from_y, to_y):
        self.tr = (type_, rot, x, from_y, to_y)
        self.trail_t = 170.0
        dist = from_y - to_y
        self.add_trauma(0.3 + min(dist, 20) * 0.013)
        c = self.cell
        self.kick_y = c * 0.4 * SHAKE_M[self.shake_lvl]
        cl = G.CELLS[type_][rot]
        r = self.rnd.random
        for q in cl:
            if any(o[0] == q[0] and o[1] == q[1] + 1 for o in cl):
                continue
            cx, cy = self.bx + (x + q[0] + 0.5) * c, self.row_y(to_y - q[1]) + c
            for k in range(5):
                d = 1 if self.rnd.random() < 0.5 else -1
                self.spawn(cx + d * c * 0.3, cy, d * c * (3 + r() * 7), -c * (2 + r() * 6), 260 + r() * 200,
                           c * (0.1 + r() * 0.12), self.light[type_] if k % 2 == 0 else _c(0xFFB8C0D0))
        if self.sound:
            self.audio.play(A.HARD, 1.0, 0.95 + r() * 0.1)
        self.buzz(1)

    def on_lock(self, cleared, rows, spin, b2b_bonus, combo, pc, hard):
        lines, cols = [], []
        names = ["", "SINGLE", "DOUBLE", "TRIPLE", "BAKETRIS!"]
        T = G.T
        if spin > 0:
            lines.append("T-SPIN" if spin == 2 else "T-SPIN MINI")
            cols.append(self.col[T])
            if cleared > 0:
                lines.append(names[cleared])
                cols.append(self.col[T])
        elif cleared > 1:
            lines.append(names[cleared])
            cols.append(GOLD if cleared == 4 else WHITE)
        if b2b_bonus:
            lines.append("BACK-TO-BACK")
            cols.append(GOLD)
        if combo >= 1:
            lines.append(f"COMBO {combo}")
            cols.append(_c(0xFFFF9442))
        if self.game.level != self.last_level:
            self.last_level = self.game.level
            lines.append(f"LEVEL {self.game.level}")
            cols.append(ACCENT)
            if self.sound:
                self.audio.play_shared("level", 0.6)
        self.call_lock(cleared, spin, b2b_bonus, combo, pc)
        if lines:
            self.action, self.action_col = lines, cols
            self.action_t = 2200.0 if cleared == 4 else 1600.0
        c = self.cell
        if cleared == 4:
            self.kaboom(rows)
            if self.mode == G.BAKETRIS and self.chain < CHAIN_MAX:
                self.blast_pending = list(rows)       # after the kaboom's hitstop: everything goes up
        elif cleared > 0:
            self.flash_rows = list(rows[:cleared])
            self.flash_t = 260.0
            big = spin == 2
            self.hitstop_t = 120.0 if big else 35.0 + 20 * cleared
            self.add_trauma(0.22 * cleared + (0.35 if big else 0))
            self.kick_y += c * 0.15 * cleared * SHAKE_M[self.shake_lvl]
            if big or pc:
                self.screen_flash_t = 180.0
            if big:
                self.wave(self.bx + 5 * c, self.row_y(rows[0]) + c / 2, c * 9, 500, self.col[T], 0)
            self.burst(rows, cleared, 5 if big else 3, 26 if big else 16, 18 if big else 12)
            if self.sound:
                self.audio.play(A.C1 + cleared - 1, 1.0, 1 + max(0, combo) * 0.05)
                if spin > 0:
                    self.audio.play_shared("spin", 0.8)
            self.buzz(4 if big else 2 + (1 if cleared >= 2 else 0))
        else:
            if spin > 0 and self.sound:
                self.audio.play_shared("spin", 0.7)
            if not hard:
                if self.sound:
                    self.audio.play(A.LOCK, 0.8)
                self.add_trauma(0.08)
                self.buzz(0)
        if pc:
            self.pc_t = 2000.0
            if self.sound:
                self.audio.play_shared("pc", 1.0)
            self.add_trauma(0.6)

    def say(self, *names, force=False) -> None:
        """The announcer (a setting, on by default, with its own volume)."""
        if (self.announcer or force) and self.sound:
            self.audio.announce(list(names), self.ann_vol / 4)

    def call_lock(self, cleared, spin, b2b_bonus, combo, pc) -> None:
        """What the announcer says for a lock: the clear (or the T-spin), then back-to-back and the combo."""
        main = "allclear" if pc else "tspin" if spin else {1: "single", 2: "double", 3: "triple", 4: "baketris"}.get(cleared)
        if not main:
            return
        self.say(main, "backtoback" if b2b_bonus else None,
                 "megacombo" if combo >= 5 else "combo" if combo >= 2 else None)

    def burst(self, rows, cleared, per, spread, up) -> None:
        c, r = self.cell, self.rnd.random
        for k in range(cleared):
            if rows[k] >= G.VIS + 2:
                continue
            cy = self.row_y(rows[k]) + c / 2
            for col in range(G.W):
                v = self.game.clear_colors[k][col]
                color = self.col[v - 1] if v > 0 else WHITE
                cx = self.bx + (col + 0.5) * c
                for j in range(per):
                    self.spawn(cx + (r() - 0.5) * c * 0.6, cy + (r() - 0.5) * c * 0.6,
                               (r() - 0.5) * c * spread + (col - 4.5) * c * 1.4, -c * (4 + r() * up),
                               550 + r() * 450, c * (0.18 + r() * 0.3), WHITE if j == 0 else color)

    def kaboom(self, rows) -> None:
        """Four lines: the big one."""
        c, r = self.cell, self.rnd.random
        sm = SHAKE_M[self.shake_lvl]
        self.flash_rows = list(rows[:4])
        self.flash_t = 320.0
        self.hitstop_t = 210.0
        self.trauma = max(self.trauma, sm)
        self.kick_y += c * 0.9 * sm
        self.punch = 0.09 * (0.4 if self.shake_lvl == 0 else 1.0)
        self.screen_flash_t, self.fire_t, self.quad_t, self.duck_t = 280.0, 900.0, 2200.0, 1400.0
        cx = self.bx + 5 * c
        cy = sum(self.row_y(rows[k]) + c / 2 for k in range(4)) / 4
        diag = math.hypot(self.vw, self.vh)
        wc = SKIN_WAVES[self.skin]
        self.wave(cx, cy, diag * 0.7, 520, wc[0], 0)
        self.wave(cx, cy, diag * 0.55, 700, wc[1], 90)
        self.wave(cx, cy, diag * 0.4, 850, wc[2], 200)
        for k in range(4):
            ry = self.row_y(rows[k]) + c / 2
            for col in range(G.W):
                v = self.game.clear_colors[k][col]
                color = self.col[v - 1] if v > 0 else WHITE
                px = self.bx + (col + 0.5) * c
                for j in range(9):
                    a = math.atan2(ry - cy, px - cx) + (r() - 0.5) * 1.6
                    sp = c * (12 + r() * 34)
                    self.spawn(px, ry, math.cos(a) * sp, math.sin(a) * sp - c * 10, 700 + r() * 700,
                               c * (0.2 + r() * 0.45), WHITE if j < 2 else color)
        for j in range(140):
            a = r() * math.pi * 2
            sp = c * (30 + r() * 50)
            self.spawn(cx, cy, math.cos(a) * sp, math.sin(a) * sp, 350 + r() * 350, c * (0.08 + r() * 0.12), wc[j % 3])
        for j in range(80):
            self.spawn(cx + (r() - 0.5) * c * 10, cy + (r() - 0.5) * c * 3, (r() - 0.5) * c * 10, -c * (8 + r() * 22),
                       1200 + r() * 900, c * (0.1 + r() * 0.18), wc[1] if r() < 0.5 else wc[2])
        if self.sound:
            self.audio.play(A.KABOOM, 1.0, 0.92 + r() * 0.1)
            self.audio.play(A.C4, 0.9)
        self.rumble(1.0, 1.0, 450)

    # -- BAKETRIS mode: the blast ------------------------------------------------------
    def tick_blast(self, dt: float) -> None:
        """Hold the piece off; once the kaboom's hitstop is over, blow the stack up, fly it
        two physics steps a frame, then land it, clear what it filled, and give the piece back."""
        g = self.game
        if self.blast is None:
            if self.hitstop_t > 0:
                self.hitstop_t -= dt
                return
            rows, self.blast_pending = self.blast_pending, None
            if self.stash is None and g.cur >= 0:
                self.stash = (g.cur, g.rot, g.px, g.py)
                g.cur = -1
            self.chain += 1
            if self.chain >= 2:
                self.say("blast")
            self.blast = blast.Blast(g.grid, rows, (self.seed, g.pieces, self.chain))
            self.add_trauma(0.5)
            if self.sound:
                self.audio.play(A.HARD, 1.0, 0.8)
            return
        self.blast.step()
        self.blast.step()
        if not self.blast.done:
            return
        g.grid = self.blast.grid
        self.blast = None
        self.add_trauma(0.25)
        n = blast.settle_clear(g)                 # may set blast_pending again: a chain
        if self.blast_pending is not None:
            return
        self.chain = 0
        if self.stash is not None:
            g.cur, g.rot, g.px, g.py = self.stash
            self.stash = None
            g.grav_acc = g.lock_timer = 0.0
            if not g.fits(g.cur, g.rot, g.px, g.py):
                g._finish(False)                     # the landing buried where the piece comes in
        del n

    def draw_blast(self, s, bx, by) -> None:
        c = self.cell
        top = by + (G.VIS - 1) * c
        for b in self.blast.bodies:
            t = b.col - 1
            q = int(b.ang / (2 * math.pi) * 16) % 16
            key = ("rot", self.skin, c, t, q)
            spr = self._cache.get(key)
            if spr is None:
                base, m = self._block(c, t)
                spr = pygame.transform.rotozoom(base, q * 22.5, 1.0)
                self._cache[key] = spr
            x = bx + (b.x + 0.5) * c
            y = top - b.y * c + c / 2
            s.blit(spr, spr.get_rect(center=(round(x), round(y))))

    def on_end(self, win):
        self.state, self.sel, self.menu_down, self.over_t = S_OVER, 0, -1, 0.0
        self.clear_inputs()
        self.music.pause()
        self.new_best = False
        g, m = self.game, self.mode
        if m == G.SPRINT:
            t = int(g.time_ms)
            if win and (self.best[1] == 0 or t < self.best[1]):
                self.best[1], self.new_best = t, True
        elif g.score > self.best[m]:
            self.best[m], self.new_best = g.score, True
        if self.new_best:
            self._save_prefs()
        if self.sound:
            self.audio.play_shared("win" if win else "over", 1.0)
        if not win:
            self.add_trauma(0.7)
        self.buzz(2 if win else 4)

    def buzz(self, kind: int) -> None:
        """0 tick, 1 hard-drop ka-chunk, 2 clear, 3 multi clear, 4 big: the Deck's rumble."""
        self.rumble(*[(0.15, 0.35, 12), (0.8, 1.0, 48), (1.0, 0.6, 100), (1.0, 0.8, 140), (1.0, 1.0, 260)][kind])

    def rumble(self, low: float, high: float, ms: int) -> None:
        if self.haptics and self.rumble_fn:
            try:
                self.rumble_fn(low, high, ms)
            except Exception:
                pass

    # -- input ----------------------------------------------------------------------------
    def menu_count(self) -> int:
        return {S_MENU: NMODES + 2, S_SETTINGS: len(SETTINGS_ROWS), S_PAUSE: 3, S_OVER: 2, S_CONTROLS: 1}.get(self.state, 0)

    def pad_down(self, b: str) -> None:
        """A controller button (the app's names: a, b, x, y, dpup, ..., leftshoulder, start, back)."""
        if self.state in (S_PLAY, S_READY):
            a = PAD_ACTION.get(b)
            if a:
                self.press(a)
            return
        self._menu_key(b)

    def pad_up(self, b: str) -> None:
        if self.state in (S_PLAY, S_READY):
            a = PAD_ACTION.get(b)
            if a:
                self.release(a)

    def trigger(self, which: str, on: bool) -> None:
        """The analog triggers: L2 hold, R2 hard drop (GameView.onGenericMotionEvent)."""
        if which == "lefttrigger":
            if on and not self.trig_l and self.state in (S_PLAY, S_READY):
                self.press(A_HOLD)
            self.trig_l = on
        else:
            if on and not self.trig_r and self.state in (S_PLAY, S_READY):
                self.press(A_HARD)
            self.trig_r = on

    def stick_motion(self, sx: float, sy: float) -> None:
        """The left stick, -1..1 (GameView's thresholds, with hysteresis)."""
        tx = 0.35 if self.joy_x != 0 else 0.55
        nx = -1 if sx < -tx else 1 if sx > tx else 0
        ny = -1 if sy < -0.75 else 1 if sy > (0.35 if self.joy_y == 1 else 0.6) else 0
        if nx and ny:
            if abs(sx) >= abs(sy):
                ny = 0
            else:
                nx = 0
        if nx != self.joy_x:
            if self.joy_x:
                self._axis_release(A_LEFT if self.joy_x < 0 else A_RIGHT)
            if nx:
                self._axis_press(A_LEFT if nx < 0 else A_RIGHT)
            self.joy_x = nx
        if ny != self.joy_y:
            if self.joy_y == 1:
                self._axis_release(A_SOFT)
            if ny == 1:
                self._axis_press(A_SOFT)
            if ny == -1:
                self._axis_press(A_HARD)
            self.joy_y = ny

    def _axis_press(self, a):
        if self.state in (S_PLAY, S_READY):
            self.press(a)
        else:
            self.menu_nav(a)

    def _axis_release(self, a):
        if self.state in (S_PLAY, S_READY):
            self.release(a)

    def _menu_key(self, b: str) -> None:
        if b == "dpup":
            self.menu_nav(A_HARD)
        elif b == "dpdown":
            self.menu_nav(A_SOFT)
        elif b == "dpleft":
            self.menu_nav(A_LEFT)
        elif b == "dpright":
            self.menu_nav(A_RIGHT)
        elif b in ("a", "start"):
            self.activate(self.sel, -1)
        elif b in ("b", "back"):
            self.back()

    def back(self) -> None:
        if self.state == S_MENU:
            self.leave()
        elif self.state == S_SETTINGS:
            self.leave_settings()
        elif self.state == S_CONTROLS:
            self.state, self.sel = S_MENU, NMODES + 1
        elif self.state == S_PAUSE:
            self.resume_game()
        elif self.state == S_OVER:
            self.state, self.sel = S_MENU, self.mode

    def menu_nav(self, a: int) -> None:
        n = self.menu_count()
        if n == 0:
            return
        if a == A_HARD:
            self.sel = (self.sel - 1) % n
        elif a == A_SOFT:
            self.sel = (self.sel + 1) % n
        elif a == A_LEFT and self.state == S_SETTINGS:
            self.adjust(self.sel, -1)
        elif a == A_RIGHT and self.state == S_SETTINGS:
            self.adjust(self.sel, 1)

    def key_down(self, key: int) -> None:
        """A keyboard key (a desk; the Deck's own keyboard)."""
        if self.state == S_PLAY and key == pygame.K_F12:          # GameView's test quad
            g = self.game
            for r in range(4):
                for col in range(9):
                    g.grid[r][col] = 7 - (col % 7)
            g.cur, g.rot, g.px, g.py = G.I, 1, 7, 18
            g.hard_drop()
            return
        if self.state in (S_PLAY, S_READY):
            a = KEY_ACTION.get(key)
            if a:
                self.press(a)
            return
        m = {pygame.K_UP: "dpup", pygame.K_w: "dpup", pygame.K_DOWN: "dpdown", pygame.K_s: "dpdown",
             pygame.K_LEFT: "dpleft", pygame.K_RIGHT: "dpright", pygame.K_RETURN: "a", pygame.K_SPACE: "a",
             pygame.K_ESCAPE: "b", pygame.K_BACKSPACE: "b"}.get(key)
        if m:
            self._menu_key(m)

    def key_up(self, key: int) -> None:
        if self.state in (S_PLAY, S_READY):
            a = KEY_ACTION.get(key)
            if a:
                self.release(a)

    # touch (GameView.onTouchEvent): pid is the finger's id, x/y in pixels
    def in_hud(self, y: float) -> bool:
        return y < self.vh * 0.12

    def dpad_dir_at(self, x, y) -> int:
        dx, dy = x - self.dcx, y - self.dcy
        if math.hypot(dx, dy) < self.dr * 0.22:
            return 0
        if abs(dx) > abs(dy):
            return A_LEFT if dx < 0 else A_RIGHT
        if dy < 0:
            return A_HARD if self.up_drop and abs(dy) > abs(dx) * 1.5 and math.hypot(dx, dy) > self.dr * 0.45 else 0
        return A_SOFT

    def set_dpad(self, d: int) -> None:
        if d == self.dpad_dir:
            return
        if self.dpad_dir:
            self.release(self.dpad_dir)
        self.dpad_dir = d
        if d:
            self.press(d)
            self.buzz(0)

    def touch_down(self, pid, x, y) -> None:
        if self.state in (S_PLAY, S_READY):
            if self.pad_mode and self.dr > 0:
                if math.hypot(x - self.dcx, y - self.dcy) < self.dr * 1.3:
                    self.dpad_pid = pid
                    self.set_dpad(self.dpad_dir_at(x, y))
                    return
                for i in range(4):
                    bx, by, br = self.pbtn[i]
                    if math.hypot(x - bx, y - by) < br * (1.7 if i >= 2 else 1.35):
                        self.pbtn_pid[i] = pid
                        self.press(PBTN_ACT[i])
                        self.buzz(0)
                        return
            if self.in_hud(y):
                self.pause_game()
                return
            self.ptrs.append({"id": pid, "x0": x, "y0": y, "ax": x, "ay": y, "cols": 0, "tap": True,
                              "swiped": False, "primary": not any(q["primary"] for q in self.ptrs)})
            return
        if self.close_rect and self.state == S_MENU and self.close_rect.collidepoint(x, y):
            self.leave()
            return
        self.menu_down = self.hit(x, y)
        if self.menu_down >= 0:
            self.sel = self.menu_down

    def touch_move(self, pid, x, y) -> None:
        if self.state not in (S_PLAY, S_READY):
            return
        if pid == self.dpad_pid:
            self.set_dpad(self.dpad_dir_at(x, y))
            return
        for q in self.ptrs:
            if q["id"] == pid:
                self.drag_move(q, x, y)

    def touch_up(self, pid, x, y) -> None:
        if self.state in (S_PLAY, S_READY):
            if pid == self.dpad_pid:
                self.set_dpad(0)
                self.dpad_pid = None
                return
            for i in range(4):
                if self.pbtn_pid[i] == pid:
                    self.pbtn_pid[i] = None
                    self.release(PBTN_ACT[i])
                    return
            for q in self.ptrs:
                if q["id"] == pid:
                    self.drag_move(q, x, y)
                    if q["tap"] and not q["swiped"] and not self.blasting() and self.game.cur >= 0:
                        self.game.rotate(1)
                    self.ptrs.remove(q)
                    break
            return
        h = self.hit(x, y)
        if h >= 0 and h == self.menu_down:
            self.activate(h, x)
        self.menu_down = -1

    def drag_move(self, q, x, y) -> None:
        """The piece follows the finger column for column; a mostly vertical stroke is a swipe (up hold, down drop)."""
        d = DENSITY
        if self.blasting() or self.game.cur < 0:
            q["tap"] = False
            return
        if math.hypot(x - q["x0"], y - q["y0"]) > 12 * d:
            q["tap"] = False
        if not q["primary"] or q["swiped"]:
            return
        vdx, vdy = x - q["ax"], y - q["ay"]
        if abs(vdy) > 30 * d and abs(vdy) > abs(vdx) * 1.4:
            q["swiped"] = True
            if vdy < 0:
                self.game.do_hold()
            else:
                self.game.hard_drop()
            return
        if abs(vdy) > 12 * d and abs(vdy) > abs(vdx) * 1.4:
            return
        step = self.cell * 10.0 / self.drag_spd
        want = round((x - q["x0"]) / step)
        moved = False
        while q["cols"] < want:
            if self.game.shift(1):
                q["cols"] += 1
                moved = True
            else:
                q["cols"] = want
                break
        while q["cols"] > want:
            if self.game.shift(-1):
                q["cols"] -= 1
                moved = True
            else:
                q["cols"] = want
                break
        if moved:
            q["ax"], q["ay"], q["tap"] = x, y, False

    def hit(self, x, y) -> int:
        for i, r in enumerate(self.menu_rects):
            if r.collidepoint(x, y):
                return i
        return -1

    def leave_settings(self) -> None:
        self.state, self.sel = S_MENU, NMODES
        if self.previewing:
            self.music.pause()
            self.previewing = False

    def activate(self, i: int, x: float) -> None:
        if self.state == S_MENU:
            if i < NMODES:
                self.start_game(i)
            elif i == NMODES:
                self.state, self.sel = S_SETTINGS, 0
            else:
                self.state, self.sel = S_CONTROLS, 0
        elif self.state == S_SETTINGS:
            if i == len(SETTINGS_ROWS) - 1:
                self.leave_settings()
            else:
                d = 1
                if x >= 0 and i < len(self.menu_rects):
                    d = -1 if x < self.menu_rects[i].centerx else 1
                self.adjust(i, d)
        elif self.state == S_CONTROLS:
            self.state, self.sel = S_MENU, NMODES + 1
        elif self.state == S_PAUSE:
            if i == 0:
                self.resume_game()
            elif i == 1:
                self.start_game(self.mode)
            else:
                self.state, self.sel = S_MENU, self.mode
        elif self.state == S_OVER:
            if i == 0:
                self.start_game(self.mode)
            else:
                self.state, self.sel = S_MENU, self.mode
        if self.sound:
            self.audio.play(A.ROT, 0.4, 0.8)

    def adjust(self, i: int, d: int) -> None:
        if i == 0:
            self.pad_mode = not self.pad_mode
            self.layout()
        elif i == 1:
            self.up_drop = not self.up_drop
        elif i == 2:
            self.skin = (self.skin + d) % len(SKIN_L)
            self.apply_skin()
            if self.sound:
                self.audio.play(A.HARD, 0.8)
            self.add_trauma(0.3)
        elif i == 3:
            self.music_sel = (self.music_sel + d) % (SONGS + 3)
            self.music.set_cycle(False)
            if self.music_sel < SONGS:
                self.music.select(self.music_sel)
                self.music.restart()
                self.music.set_rate(1.0)
                self.music.set_volume(0.55)
                self.music.play()
                self.previewing = True
            else:
                self.music.pause()
                self.previewing = False
        elif i == 4:
            self.shake_lvl = clamp(self.shake_lvl + d, 0, 2)
            self.add_trauma(0.6)
        elif i == 5:
            self.sound = not self.sound
        elif i == 6:
            self.haptics = not self.haptics
            self.buzz(1)
        elif i == 7:
            self.ghost = not self.ghost
        elif i == 8:
            self.drag_spd = clamp(self.drag_spd + d, 5, 20)
        elif i == 9:
            self.das = clamp(self.das + d * 10, 40, 300)
        elif i == 10:
            self.arr_idx = clamp(self.arr_idx + d, 0, len(ARRS) - 1)
        elif i == 11:
            self.sdf_idx = clamp(self.sdf_idx + d, 0, len(SDF) - 1)
        elif i == 12:
            self.announcer = not self.announcer
            if self.announcer:
                self.say("baketris")
        elif i == 13:
            self.ann_vol = clamp(self.ann_vol + d, 1, 4)
            self.say("single", force=True)
        else:
            return
        self._save_prefs()

    # -- drawing --------------------------------------------------------------------------
    def draw(self, screen: pygame.Surface) -> None:
        screen.fill(SKIN_BG[self.skin])
        self.close_rect = None
        if self.state == S_MENU:
            self.draw_main_menu(screen)
        elif self.state == S_SETTINGS:
            self.draw_settings(screen)
        elif self.state == S_CONTROLS:
            self.draw_controls(screen)
        else:
            ox, oy = self.sh_x, self.sh_y + self.kick_y
            if self.skin == 1:
                q = max(2.0, self.cell / 8)
                ox, oy = round(ox / q) * q, round(oy / q) * q
            if self.punch > 0:
                lay = self.layer
                lay.fill(SKIN_BG[self.skin])
                self.draw_game(lay, ox, oy)
                k = 1 + self.punch
                cx, cy = self.bx + 5 * self.cell, self.by + 10 * self.cell
                sw, sh = self.vw / k, self.vh / k
                src = pygame.Rect(round(cx - cx / k), round(cy - cy / k), round(sw), round(sh)).clip(lay.get_rect())
                pygame.transform.scale(lay.subsurface(src), (self.vw, self.vh), screen)
            else:
                self.draw_game(screen, ox, oy)
            if self.pad_mode and self.dr > 0:
                self.draw_pad(screen)
            # the fire tint, then the white flash: one translucent fill that is both
            a1 = (70 * (self.fire_t / 900) ** 2 if self.fire_t > 0 else 0) / 255
            a2 = (120 * self.screen_flash_t / 280 if self.screen_flash_t > 0 else 0) / 255
            if a1 > 0 or a2 > 0:
                al = 1 - (1 - a1) * (1 - a2)
                fire = SKIN_WAVES[self.skin][1]
                col = tuple(min(255, int((f * a1 * (1 - a2) + 255 * a2) / al)) for f in fire)
                self.tint.fill(col)
                self.tint.set_alpha(int(255 * al))
                screen.blit(self.tint, (0, 0))
            if self.state == S_PAUSE:
                self.draw_pause(screen)
            elif self.state == S_OVER:
                self.draw_over(screen)
            elif self.state == S_READY:
                self.draw_ready(screen)

    def draw_game(self, s, ox, oy) -> None:
        c, g = self.cell, self.game
        bx, by = self.bx + ox, self.by + oy
        s.blit(self.board, (round(bx), round(by)))
        self.draw_ambient(s, ox, oy)
        if self.danger:
            a = 0.5 + 0.5 * math.sin(self.anim_t / 140.0)
            self.fill_a(s, bx, by, bx + 10 * c, by + 5 * c, _c(0xFFE0465A), 20 + 40 * a)
        self.draw_hud_side(s, ox, oy)

        def row_y(r):
            return by + (G.VIS - 1 - r) * c
        if self.trail_t > 0:
            a = 90 * self.trail_t / 170
            t, rot, x, frm, to = self.tr
            for q in G.CELLS[t][rot]:
                col = x + q[0]
                top, bottom = row_y(min(frm - q[1], 21)), row_y(to - q[1])
                self.fill_a(s, bx + col * c + c * 0.1, top, bx + (col + 1) * c - c * 0.1, bottom, self.col[t], a)
        dead = self.state == S_OVER and not g.won
        for r in range(22):
            row = g.grid[r]
            y = row_y(r)
            for col in range(G.W):
                v = row[col]
                if v:
                    self.block(s, bx + col * c, y, c, v - 1, 140 if dead else 255)
        if g.cur >= 0 and self.state in (S_PLAY, S_PAUSE, S_OVER):
            cl = G.CELLS[g.cur][g.rot]
            if self.ghost and not g.over:
                gy = g.ghost_y()
                if gy != g.py:
                    for q in cl:
                        self.ghost_block(s, bx + (g.px + q[0]) * c, row_y(gy - q[1]), c, g.cur)
            alpha = 255 - int(min(1.0, g.lock_timer / G.LOCK_MS) * 90)
            for q in cl:
                r = g.py - q[1]
                if r < 22:
                    self.block(s, bx + (g.px + q[0]) * c, row_y(r), c, g.cur, alpha)
        if self.flash_t > 0:
            self.draw_flash(s, bx, row_y)
        if getattr(self, "blast", None) is not None:
            self.draw_blast(s, bx, by)
        self.draw_particles(s, ox, oy)
        if self.action_t > 0:
            a = 255 * min(1.0, self.action_t / 450)
            total = 2200 if self.quad_t > 0 else 1600
            rise = (1 - self.action_t / total) * c * 1.2
            ty = by + 6.5 * c - rise
            for i, line in enumerate(self.action):
                size = c * (0.95 if i == 0 else 0.7)
                if i == 0 and self.quad_t > 0:
                    age = 2200 - self.quad_t
                    pop = 2.4 - 1.2 * age / 160 if age < 160 else 1.2 + 0.05 * math.sin(self.anim_t / 60.0)
                    self.big_text(s, line, bx + 5 * c, ty, c * 1.25 * pop, self.action_col[i], a)
                    size = c * 1.25 * pop
                else:
                    self.text(s, line, bx + 5 * c + c * 0.07, ty + c * 0.07, size, BLACK, "center", a * 2 / 3)
                    self.text(s, line, bx + 5 * c, ty, size, self.action_col[i], "center", a)
                ty += size * 1.05
        if self.song_toast_t > 0 and self.song_shown >= 0:
            a = 220 * min(1.0, self.song_toast_t / 500) * min(1.0, (2600 - self.song_toast_t) / 250)
            self.text(s, "♪  " + SONG_NAMES[self.song_shown], bx + 5 * c, by + c * 1.3, c * 0.5, WHITE, "center", a)
        if self.pc_t > 0:
            a = 255 * min(1.0, self.pc_t / 600)
            sc = 1 + max(0.0, (self.pc_t - 1700) / 300.0) * 0.5
            self.big_text(s, "ALL CLEAR", bx + 5 * c, by + 9 * c, c * 1.2 * sc, GOLD, a, shadow=False)

    def big_text(self, s, line, cx, baseline, size, col, alpha, shadow=True) -> None:
        """Text whose size changes every frame: rendered once big, scaled (no font per size)."""
        base = self.cell * 3
        key = ("bt", line, col)
        v = self._cache.get(key)
        if v is None:
            f = _font(base)
            v = self._cache[key] = (f.render(line, True, col), f.render(line, True, BLACK), f.get_ascent())
        img, shd, asc = v
        k = size / base
        w, h = max(1, round(img.get_width() * k)), max(1, round(img.get_height() * k))
        top = baseline - asc * k
        if shadow:
            sh = pygame.transform.smoothscale(shd, (w, h))
            sh.set_alpha(int(alpha * 2 / 3))
            s.blit(sh, (round(cx - w / 2 + self.cell * 0.07), round(top + self.cell * 0.07)))
        im = pygame.transform.smoothscale(img, (w, h))
        im.set_alpha(int(alpha))
        s.blit(im, (round(cx - w / 2), round(top)))

    def draw_flash(self, s, bx, row_y) -> None:
        c, skin, ft = self.cell, self.skin, self.flash_t
        f = min(1.0, ft / 260)
        for r in self.flash_rows:
            if r >= G.VIS:
                continue
            ty = row_y(r)
            grow = max(0.0, 1 - f) * c * 0.5
            if skin == 1:
                if int(ft / 60) % 2 == 0:
                    s.fill(_c(0xFF0F380F), pygame.Rect(round(bx), round(ty), 10 * c, c))
            elif skin == 2:
                self.fill_a(s, bx, ty, bx + 10 * c, ty + c, WHITE, 120 * f)
                sx = bx + (1 - f) * 12 * c - c
                self.fill_a(s, sx, ty, sx + c * 1.2, ty + c, WHITE, 230 * f)
            elif skin == 3:
                self.fill_a(s, bx - grow, ty - grow, bx + 10 * c + grow, ty + c + grow, WHITE, 200 * f, c * 0.5)
            elif skin == 4:
                self.fill_a(s, bx, ty - grow, bx + 10 * c, ty + c + grow, _c(0xFFFF7A1A), 240 * f)
            elif skin == 5:
                hh, wd = c * 0.5 * f, 5 * c * (1.4 - f * 0.4)
                self.fill_a(s, bx + 5 * c - wd, ty + c / 2 - hh, bx + 5 * c + wd, ty + c / 2 + hh, WHITE, 255 * f)
            elif skin == 6:
                for k in range(4):
                    off = (self.rnd.random() - 0.5) * c * 2 * f
                    self.fill_a(s, bx + off, ty + k * c / 4, bx + 10 * c + off, ty + (k + 0.6) * c / 4,
                                _c(0xFF39FF14), 200 * f)
            else:
                self.fill_a(s, bx, ty - grow, bx + 10 * c, ty + c + grow, WHITE, 240 * f)

    def draw_pad(self, s) -> None:
        dcx, dcy, dr = self.dcx, self.dcy, self.dr
        spr = self._alpha_shape("c", dr * 2, dr * 2, WHITE)
        spr.set_alpha(0x14)
        s.blit(spr, (round(dcx - dr), round(dcy - dr)))
        aw = dr * 0.36
        for d in (A_HARD, A_SOFT, A_LEFT, A_RIGHT):
            on = self.dpad_dir == d
            if d == A_HARD:
                r = (dcx - aw / 2, dcy - dr * 0.92, dcx + aw / 2, dcy - aw / 2)
            elif d == A_SOFT:
                r = (dcx - aw / 2, dcy + aw / 2, dcx + aw / 2, dcy + dr * 0.92)
            elif d == A_LEFT:
                r = (dcx - dr * 0.92, dcy - aw / 2, dcx - aw / 2, dcy + aw / 2)
            else:
                r = (dcx + aw / 2, dcy - aw / 2, dcx + dr * 0.92, dcy + aw / 2)
            self.fill_a(s, *r, WHITE, 0x66 if on else 0x2E, aw * 0.2)
        self.fill_a(s, dcx - aw / 2, dcy - aw / 2, dcx + aw / 2, dcy + aw / 2, WHITE, 0x2E)
        t = aw * 0.32
        k = dr * 0.82
        arrow = blend(SKIN_BG[self.skin], WHITE, 0xB0 / 255)       # 0xB0FFFFFF over the background
        for pts in ([(dcx, dcy - k), (dcx - t, dcy - k + t * 1.4), (dcx + t, dcy - k + t * 1.4)],
                    [(dcx, dcy + k), (dcx - t, dcy + k - t * 1.4), (dcx + t, dcy + k - t * 1.4)],
                    [(dcx - k, dcy), (dcx - k + t * 1.4, dcy - t), (dcx - k + t * 1.4, dcy + t)],
                    [(dcx + k, dcy), (dcx + k - t * 1.4, dcy - t), (dcx + k - t * 1.4, dcy + t)]):
            pygame.draw.polygon(s, arrow, pts)
        for i in range(4):
            on = self.pbtn_pid[i] is not None
            x, y, r = self.pbtn[i]
            if i >= 2:
                self.fill_a(s, x - r * 1.6, y - r * 0.7, x + r * 1.6, y + r * 0.7, WHITE, 0x70 if on else 0x26, r * 0.7)
                pygame.draw.rect(s, blend(SKIN_BG[self.skin], WHITE, 0x55 / 255),
                                 pygame.Rect(round(x - r * 1.6), round(y - r * 0.7), round(r * 3.2), round(r * 1.4)),
                                 round(2 * DENSITY), border_radius=round(r * 0.7))
            else:
                spr = self._alpha_shape("c", r * 2, r * 2, WHITE)
                spr.set_alpha(0x70 if on else 0x26)
                s.blit(spr, (round(x - r), round(y - r)))
                pygame.draw.circle(s, blend(SKIN_BG[self.skin], WHITE, 0x55 / 255), (x, y), r, round(2 * DENSITY))
            self._text_c(s, PBTN_L[i], x, y, r * (0.6 if i >= 2 else 0.8), _c(0xFFFF9A8A) if i == 3 else WHITE,
                         "center", 0xD8)

    def draw_hud_side(self, s, ox, oy) -> None:
        c, g = self.cell, self.game
        hb = [v + (ox if i % 2 == 0 else oy) for i, v in enumerate(self.hold_box)]
        nb = [v + (ox if i % 2 == 0 else oy) for i, v in enumerate(self.next_box)]
        lab = c * 0.45
        self.text(s, "HOLD", hb[0], hb[1] - c * 0.22, lab, GREY)
        self.text(s, "NEXT", nb[0], nb[1] - c * 0.22, lab, GREY)
        self.fill_a(s, *hb, WHITE, 0x22, c * 0.25)
        self.fill_a(s, *nb, WHITE, 0x22, c * 0.25)
        mc = self.mc
        if g.hold >= 0:
            self.mini(s, g.hold, (hb[0] + hb[2]) / 2, (hb[1] + hb[3]) / 2, mc, 90 if g.hold_used else 255)
        n = 3 if self.pad_mode else 5
        for i in range(min(n, len(g.queue))):
            cy = nb[1] + mc * 0.3 + mc * 3 * (i + 0.5)
            self.mini(s, g.queue[i], (nb[0] + nb[2]) / 2, cy, mc if i == 0 else mc * 0.86, 255)
        x, y = hb[0], self.stats_top + oy
        if self.mode in (G.MARATHON, G.BAKETRIS):
            y = self.stat(s, "SCORE", fmt_num(g.score), x, y)
            y = self.stat(s, "LEVEL", str(g.level), x, y)
            self.stat(s, "LINES", str(g.lines), x, y)
        elif self.mode == G.SPRINT:
            y = self.stat(s, "TIME", fmt_time(g.time_ms), x, y)
            y = self.stat(s, "LINES", f"{max(0, G.SPRINT_LINES - g.lines)} LEFT", x, y)
            self.stat(s, "PPS", self.pps(), x, y)
        else:
            y = self.stat(s, "TIME", fmt_time(G.ULTRA_MS - g.time_ms), x, y)
            y = self.stat(s, "SCORE", fmt_num(g.score), x, y)
            self.stat(s, "LINES", str(g.lines), x, y)

    def stat(self, s, label, value, x, y) -> float:
        c = self.cell
        self.text(s, label, x, y, c * 0.42, GREY)
        self.text(s, value, x, y + c * 0.78, c * 0.66, WHITE)
        return y + c * 1.55

    def draw_ready(self, s) -> None:
        c = self.cell
        go = self.ready_t <= 500
        k = 1.0 if not go else 1 + (500 - self.ready_t) / 500.0 * 0.3
        size = c * 1.6 * k
        if go:
            self.big_text(s, "GO!", self.bx + 5 * c, self.by + 9 * c + size * 0.36, size, ACCENT, 255, shadow=False)
        else:
            self._text_c(s, "READY", self.bx + 5 * c, self.by + 9 * c, size, WHITE, "center", 255)

    def dim(self, s, a, rgb=BLACK) -> None:
        self.fill_a(s, 0, 0, self.vw, self.vh, rgb, a)

    def draw_pause(self, s) -> None:
        self.dim(s, 0xC0)
        self._text_c(s, "PAUSED", self.vw / 2, self.vh * 0.28, min(self.vw * 0.11, self.vh * 0.07), WHITE, "center", 255)
        self.draw_items(s, ["RESUME", "RESTART", "MENU"], None, -1, 1.0)

    def draw_over(self, s) -> None:
        g, m = self.game, self.mode
        self.dim(s, min(0xC8, self.over_t / 2))
        if self.over_t < 350:
            self.menu_rects = []
            return
        if g.won:
            title = "40 LINES" if m == G.SPRINT else "TIME!" if m == G.ULTRA else "COMPLETE"
        else:
            title = "GAME OVER"
        vw, vh = self.vw, self.vh
        ts = min(vw * 0.11, vh * 0.07)
        self._text_c(s, title, vw / 2, vh * 0.2, ts, GOLD if g.won else _c(0xFFF8566B), "center", 255)
        if m == G.SPRINT:
            rows = [("TIME", fmt_time(g.time_ms) if g.won else "--"), ("LINES", f"{g.lines} / {G.SPRINT_LINES}"),
                    ("PIECES", str(g.pieces)), ("PPS", self.pps()),
                    ("BEST", "--" if self.best[1] == 0 else fmt_time(self.best[1]))]
        else:
            rows = [("SCORE", fmt_num(g.score)), ("LINES", str(g.lines))]
            if m in (G.MARATHON, G.BAKETRIS):
                rows.append(("LEVEL", str(g.level)))
            rows += [("TIME", fmt_time(g.time_ms)), ("PPS", self.pps()), ("BEST", fmt_num(self.best[m]))]
        rs, y, w = min(vw * 0.055, vh * 0.032), vh * 0.3, min(vw * 0.7, 380 * DENSITY)
        lx, rx = (vw - w) / 2, (vw + w) / 2
        for a, b in rows:
            self._text_c(s, a, lx, y, rs * 0.8, GREY, "left", 255)
            self._text_c(s, b, rx, y, rs, WHITE, "right", 255)
            y += rs * 1.6
        if self.new_best:
            self._text_c(s, "NEW BEST", vw / 2, y + rs * 0.4, rs * 1.1, GOLD, "center", 255)
        self.draw_items(s, ["RETRY", "MENU"], None, max(y + rs * 2, vh * 0.66), 1.0)

    def draw_main_menu(self, s) -> None:
        vw, vh = self.vw, self.vh
        title = "BAKETRIS"
        ts = min(vw * 0.14, vh * 0.09)
        f = _font(ts)
        x = (vw - f.size(title)[0]) / 2
        ty = vh * 0.2
        tc = SKIN_COLORS[0 if self.skin == 1 else self.skin]
        for i, ch in enumerate(title):
            bob = math.sin(self.anim_t / 380.0 + i * 0.6) * ts * 0.04
            x += self.text(s, ch, x, ty + bob, ts, _c(0xFF9BBC0F) if self.skin == 1 else tc[i % 7])
        sz = min(vw / 34, vh / 50)
        gx = vw / 2 - 3 * 4.6 * sz
        for t in range(7):
            self.mini(s, t, gx + t * 4.6 * sz, ty + ts * 0.9, sz, 220)
        b = self.best
        subs = [f"BEST {fmt_num(b[0])}" if b[0] else "150 LINES", f"BEST {fmt_time(b[1])}" if b[1] else "RACE",
                f"BEST {fmt_num(b[2])}" if b[2] else "SCORE ATTACK",
                f"BEST {fmt_num(b[3])}" if b[3] else "4 LINES BLOW UP", None, None]
        self.draw_items(s, MODE_NAMES + ["SETTINGS", "CONTROLS"], subs, ty + ts * 1.55, 0.9)
        hs = min(vw * 0.034, vh * 0.02)
        hints = ["d-pad / stick move  ·  up or R2 drop  ·  down soft  ·  A / B rotate  ·  Y 180",
                 "X / L1 / R1 / L2 hold  ·  START pause  ·  B back to the Rook remote"]
        for i, h in enumerate(hints):
            self._text_c(s, h, vw / 2, vh - hs * (2.0 + 1.8 * (len(hints) - 1 - i)), hs, GREY, "center", 255)
        r = pygame.Rect(vw - 70, 14, 52, 44)                       # close: back to the remote (touch)
        pygame.draw.rect(s, _c(0xFF121725), r, 0, border_radius=10)
        pygame.draw.line(s, GREY, (r.x + 16, r.y + 12), (r.right - 16, r.bottom - 12), 4)
        pygame.draw.line(s, GREY, (r.x + 16, r.bottom - 12), (r.right - 16, r.y + 12), 4)
        self.close_rect = r

    def draw_settings(self, s) -> None:
        vw, vh = self.vw, self.vh
        self._text_c(s, "SETTINGS", vw / 2, vh * 0.06, min(vw * 0.09, vh * 0.05), WHITE, "center", 255)
        ms = self.music_sel
        song_l = SONG_NAMES[ms] if ms < SONGS else "SHUFFLE" if ms == SHUFFLE else "CYCLE" if ms == CYCLE else "OFF"
        labels = SETTINGS_ROWS
        lr = lambda v: f"‹  {v}  ›"                           # noqa: E731
        vals = ["ON-SCREEN PAD" if self.pad_mode else "TOUCH", "DROP" if self.up_drop else "NOTHING",
                lr(SKIN_L[self.skin]), lr(song_l), lr(SHAKE_L[self.shake_lvl]),
                "ON" if self.sound else "OFF", "ON" if self.haptics else "OFF", "ON" if self.ghost else "OFF",
                lr(f"{self.drag_spd / 10:.1f}x"), lr(f"{self.das} ms"),
                lr("INSTANT" if ARRS[self.arr_idx] == 0 else f"{ARRS[self.arr_idx]} ms"), lr(SDF_L[self.sdf_idx]),
                "ON" if self.announcer else "OFF", lr(f"{self.ann_vol * 25}%"), None]
        self.draw_items(s, labels, vals, vh * 0.1, 0.78, self.sh_x, self.sh_y)
        bs = round(min(vw / 16, vh / 40))
        for t in range(7):
            self.block(s, vw / 2 - 3.5 * bs * 1.1 + t * bs * 1.1, vh - bs * 1.4, bs, t, 255)

    def draw_controls(self, s) -> None:
        vw, vh = self.vw, self.vh
        self._text_c(s, "CONTROLS", vw / 2, vh * 0.08, min(vw * 0.09, vh * 0.05), WHITE, "center", 255)
        w = min(vw * 0.86, 760 * DENSITY)
        x0 = (vw - w) / 2
        y = vh * 0.17
        rh = vh * 0.062
        for i, (what, does) in enumerate(CONTROLS):
            if i % 2 == 0:
                self.fill_a(s, x0, y, x0 + w, y + rh, _c(0xFF121725), 255, rh * 0.22)
            self._text_c(s, what, x0 + rh * 0.4, y + rh / 2, rh * 0.36, ACCENT, "left", 255)
            self._text_c(s, does, x0 + w - rh * 0.4, y + rh / 2, rh * 0.36, WHITE, "right", 255)
            y += rh
        self.draw_items(s, ["BACK"], None, y + rh * 0.6, 0.9)

    def draw_items(self, s, items, subs, start_y, scale, ox=0.0, oy=0.0) -> None:
        vw, vh = self.vw, self.vh
        self.menu_rects = []
        w = min(vw * 0.86, 460 * DENSITY)
        ih = min(vh * 0.085, 64 * DENSITY) * scale
        if start_y > 0:
            ih = min(ih, (vh - start_y - vh * 0.06) / (len(items) * 1.26))
        gap = ih * 0.26
        x = (vw - w) / 2
        total = len(items) * ih + (len(items) - 1) * gap
        y = start_y if start_y > 0 else (vh - total) / 2 + vh * 0.07
        for i, item in enumerate(items):
            r = pygame.Rect(round(x), round(y), round(w), round(ih))
            self.menu_rects.append(r)
            dr = r.move(round(ox), round(oy))
            on = i == self.sel
            rad = round(ih * 0.22)
            pygame.draw.rect(s, _c(0xFF1D2436) if on else _c(0xFF121725), dr, 0, border_radius=rad)
            if on:
                pygame.draw.rect(s, ACCENT, dr, round(2 * DENSITY), border_radius=rad)
            self._text_c(s, item, dr.x + ih * 0.4, dr.centery, ih * 0.36, WHITE, "left", 255)
            if subs is not None and subs[i] is not None:
                self._text_c(s, subs[i], dr.right - ih * 0.4, dr.centery, ih * 0.3, ACCENT if on else GREY, "right", 255)
            y += ih + gap
