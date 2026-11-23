"""Towers, the remote's tower defence (firmware-remote/src/towers.cpp), on the Deck.

Rules line for line: a 16 x 7 field of 8-pixel tiles, the road from the left
to the right, waves (every fifth a few big ones, every third quick and frail
ones), guns (one target, often), blasts (everything near the mark, slowly) and
frost (halves the speed of what is in reach), three levels each, gold for
kills and for each wave seen off, lives lost to what gets through; 33 ms
steps. The firmware's pixels are kept (the rules are in them) and drawn S
times bigger, in colour. A builds or upgrades where the cursor is, Y or a
shoulder picks the next tower, X calls the next wave early (+2 gold), Start
pauses, B leaves; or touch a tile, and the buttons under the field."""

from __future__ import annotations

import pygame

from . import mini
from .mini import DOWN, DROP, HOLD, LEFT, PAUSE, QUIT, RIGHT, ROTATE, ROTATE_BACK, cdiv, due

GW, GH, T, TOP = 16, 7, 8, 8
NF, CELLS = 18, GW * GH
STEP_MS = 33
NONE, GUN, BLAST, FROST, ROAD = 0, 1, 2, 3, 255
KINDS = [("", 0, 0, 0, 0), ("GUN", 10, 20, 1, 10), ("BLAST", 25, 18, 2, 28), ("FROST", 20, 17, 0, 0)]  # tag cost range dmg rate
WP = [(-1, 1), (13, 1), (13, 3), (2, 3), (2, 5), (16, 5)]
DIRS = LEFT | RIGHT | ROTATE | DOWN


def wp_x(i):
    return WP[i][0] * T + T // 2


def wp_y(i):
    return TOP + WP[i][1] * T + T // 2


class Foe:
    __slots__ = ("on", "slow", "kind", "hp", "full", "x", "y", "d")

    def __init__(self):
        self.on = self.slow = False
        self.kind = self.hp = self.full = self.x = self.y = self.d = 0


class Rules:
    def __init__(self):
        self.cell = [0] * CELLS
        self.lvl = [0] * CELLS
        self.cool = [0] * CELLS
        self.foes = [Foe() for _ in range(NF)]
        self.flashes = [[0, 0, 0, 0, 0, 0] for _ in range(6)]       # x0 y0 x1 y1 t r
        self.cx, self.cy, self.sel = 7, 2, GUN
        self.gold = self.lives = self.wave = self.to_spawn = self.spawn_wait = self.frame = self.kills = 0
        self.next_step = self.wave_at = self.rep_at = 0
        self.over = self.paused = self.quit_asked = self.in_wave = False
        self.prev_keys = 0

    def place(self, f) -> bool:
        left = f.d
        for i in range(len(WP) - 1):
            ax, ay, bx, by = wp_x(i), wp_y(i), wp_x(i + 1), wp_y(i + 1)
            ln = (abs(bx - ax) + abs(by - ay)) * 16
            if left <= ln:
                f.x = ax + cdiv((bx - ax) * left, ln)
                f.y = ay + cdiv((by - ay) * left, ln)
                return True
            left -= ln
        return False

    def flash(self, x0, y0, x1, y1, r):
        f = self.flashes[0]
        for c in self.flashes:
            if c[4] < f[4]:
                f = c
        f[:] = [x0, y0, x1, y1, 3, r]

    def hurt(self, f, dmg):
        if not f.on:
            return
        f.hp -= dmg
        if f.hp > 0:
            return
        f.on = False
        self.kills += 1
        self.gold += 15 + self.wave if f.kind == 2 else 2 + self.wave // 4

    @staticmethod
    def near(ax, ay, bx, by, r):
        dx, dy = ax - bx, ay - by
        return dx * dx + dy * dy <= r * r

    def begin_wave(self):
        self.wave += 1
        self.in_wave = True
        w = self.wave
        self.to_spawn = min(40, 2 + w // 5 if w % 5 == 0 else 6 + w)
        self.spawn_wait = 0

    def spawn(self):
        w = self.wave
        for f in self.foes:
            if f.on:
                continue
            base = 3 + w * 2 + w * w // 6
            kind = 2 if w % 5 == 0 else 1 if w % 3 == 0 else 0
            hp = base * 6 if kind == 2 else base // 2 + 1 if kind == 1 else base
            f.on, f.slow, f.kind, f.hp, f.full, f.x, f.y, f.d = True, False, kind, hp, hp, 0, 0, 0
            self.place(f)
            return

    def step(self, ms):
        self.frame += 1
        w = self.wave
        if not self.in_wave:
            if due(ms, self.wave_at):
                self.begin_wave()
        elif self.to_spawn:
            self.spawn_wait -= 1
            if self.spawn_wait <= 0:
                self.spawn()
                self.to_spawn -= 1
                self.spawn_wait = 45 if self.wave % 5 == 0 else 12 if self.wave % 3 == 0 else 20
        w = self.wave
        foes, cell, lvl = self.foes, self.cell, self.lvl
        for f in foes:
            f.slow = False
        for i in range(CELLS):
            if cell[i] != FROST:
                continue
            tx, ty, r = (i % GW) * T + T // 2, TOP + (i // GW) * T + T // 2, KINDS[FROST][2] + (lvl[i] - 1) * 4
            for f in foes:
                if f.on and self.near(f.x, f.y, tx, ty, r):
                    f.slow = True
        for f in foes:
            if not f.on:
                continue
            v = 6 if f.kind == 2 else 9 + w // 3
            v = min(v, 16)
            if f.kind == 1:
                v = v * 17 // 10
            f.d += v // 2 if f.slow else v
            if not self.place(f):
                f.on = False
                self.lives -= 3 if f.kind == 2 else 1
                if self.lives <= 0:
                    self.lives = 0
                    self.over = True
        for i in range(CELLS):
            if cell[i] not in (GUN, BLAST):
                continue
            if self.cool[i]:
                self.cool[i] -= 1
                continue
            k = KINDS[cell[i]]
            tx, ty, r = (i % GW) * T + T // 2, TOP + (i // GW) * T + T // 2, k[2] + (lvl[i] - 1) * 3
            mark = None
            for f in foes:
                if f.on and self.near(f.x, f.y, tx, ty, r) and (mark is None or f.d > mark.d):
                    mark = f
            if mark is None:
                continue
            dmg, mx, my = k[3] + lvl[i] - 1, mark.x, mark.y
            if cell[i] == GUN:
                self.hurt(mark, dmg)
                self.flash(tx, ty, mx, my, 0)
            else:
                for f in foes:
                    if f.on and self.near(f.x, f.y, mx, my, 9):
                        self.hurt(f, dmg)
                self.flash(tx, ty, mx, my, 8)
            self.cool[i] = (k[4] - (lvl[i] - 1) * 2) & 255
        if self.in_wave and not self.to_spawn and not any(f.on for f in foes):
            self.in_wave = False
            self.gold += 5 + self.wave
            self.wave_at = (ms + 6000) & 0xFFFFFFFF
        for f in self.flashes:
            if f[4]:
                f[4] -= 1

    def start(self, ms):
        self.cell = [0] * CELLS
        self.lvl = [0] * CELLS
        self.cool = [0] * CELLS
        for i in range(len(WP) - 1):
            x, y = WP[i]
            bx, by = WP[i + 1]
            while True:
                if 0 <= x < GW and 0 <= y < GH:
                    self.cell[y * GW + x] = ROAD
                if x == bx and y == by:
                    break
                x += (bx > x) - (bx < x)
                y += (by > y) - (by < y)
        for f in self.foes:
            f.on = False
        for f in self.flashes:
            f[4] = 0
        self.cx, self.cy, self.sel = 7, 2, GUN
        self.gold, self.lives = 30, 10
        self.wave = self.kills = 0
        self.over = self.paused = self.quit_asked = self.in_wave = False
        self.wave_at = (ms + 8000) & 0xFFFFFFFF
        self.next_step = ms
        self.prev_keys = 0xFFFF

    def tick(self, ms, keys) -> bool:
        pressed = keys & ~self.prev_keys & 0xFFFF
        if pressed & DIRS:
            self.rep_at = (ms + 320) & 0xFFFFFFFF
        elif (keys & DIRS) and due(ms, self.rep_at):
            pressed |= keys & DIRS
            self.rep_at = (ms + 110) & 0xFFFFFFFF
        self.prev_keys = keys
        if pressed & QUIT:
            self.quit_asked = True
            return True
        if self.over:
            if not pressed & DROP:
                return False
            self.start(ms)
            self.prev_keys = keys
            return True
        if pressed & PAUSE:
            self.paused = not self.paused
            self.next_step = ms
            return True
        if pressed & LEFT:
            self.cx -= 1
        if pressed & RIGHT:
            self.cx += 1
        if pressed & ROTATE:
            self.cy -= 1
        if pressed & DOWN:
            self.cy += 1
        self.cx = max(0, min(GW - 1, self.cx))
        self.cy = max(0, min(GH - 1, self.cy))
        if pressed & ROTATE_BACK:
            self.sel = self.sel % 3 + 1
        if pressed & DROP:
            i = self.cy * GW + self.cx
            c = self.cell[i]
            if c == NONE and self.gold >= KINDS[self.sel][1]:
                self.gold -= KINDS[self.sel][1]
                self.cell[i], self.lvl[i], self.cool[i] = self.sel, 1, 0
            elif GUN <= c <= FROST and self.lvl[i] < 3 and self.gold >= KINDS[c][1] * self.lvl[i]:
                self.gold -= KINDS[c][1] * self.lvl[i]
                self.lvl[i] += 1
        if (pressed & HOLD) and not self.in_wave and not self.paused:
            self.wave_at = ms
            self.gold += 2
        changed = bool(pressed)
        if self.paused or not due(ms, self.next_step):
            return changed
        self.next_step = (ms + STEP_MS) if ((ms - self.next_step) & 0xFFFFFFFF) > 200 else (self.next_step + STEP_MS)
        self.next_step &= 0xFFFFFFFF
        self.step(ms)
        return True

    def quit(self):
        return self.quit_asked

    def score(self):
        return self.wave * 100 + self.kills


S = 9                                    # firmware pixels to the Deck's
BG, FIELD, ROADC = (14, 17, 23), (22, 30, 26), (58, 52, 40)
GOLDC, RED, WHITE, DIM = (255, 215, 0), (255, 60, 90), (240, 240, 240), (140, 150, 168)
GUNC, BLASTC, FROSTC = (80, 220, 255), (255, 150, 40), (170, 205, 255)
KCOL = [None, GUNC, BLASTC, FROSTC]


class TowersGame(mini.Screen):
    NAME = "Towers"

    def __init__(self, size=(1280, 800), bests=None, rumble=None):
        super().__init__(size, bests, rumble)
        self.g = Rules()
        self.g.start(self.ms)
        self.ox = (self.vw - 128 * S) // 2
        self.oy = 40
        self.buttons = []
        self.close = None
        self._lives = self.g.lives

    def update(self, dt):
        super().update(dt)
        if self.g.lives < self._lives:
            self.rumble(0.8, 0.4, 120)
        self._lives = self.g.lives

    def px(self, x, y):
        return self.ox + x * S, self.oy + y * S

    def touch_down(self, pid, x, y):
        if self.close and self.close.collidepoint(x, y):
            self.done = True
            return
        for r, fn in self.buttons:
            if r.collidepoint(x, y):
                fn()
                return
        g = self.g
        if g.over:
            self.tap(DROP)
            return
        tx, ty = (x - self.ox) // (T * S), (y - self.oy - TOP * S) // (T * S)
        if 0 <= tx < GW and 0 <= ty < GH:
            if (tx, ty) == (g.cx, g.cy):
                self.tap(DROP)                   # the tile already picked: build or upgrade
            else:
                g.cx, g.cy = int(tx), int(ty)

    def _pick(self, k):
        self.g.sel = k

    def draw(self, s):
        g = self.g
        s.fill(BG)
        fx, fy = self.px(0, TOP)
        pygame.draw.rect(s, FIELD, (fx, fy, GW * T * S, GH * T * S))
        for i in range(CELLS):                   # the road's tiles
            if g.cell[i] == ROAD:
                x, y = self.px((i % GW) * T, TOP + (i // GW) * T)
                pygame.draw.rect(s, ROADC, (x, y, T * S, T * S))
        for i in range(len(WP) - 1):             # its dotted middle
            a, b = self.px(max(0, wp_x(i)), wp_y(i)), self.px(min(127, wp_x(i + 1)), wp_y(i + 1))
            pygame.draw.line(s, (110, 100, 80), a, b, 3)
        for i in range(CELLS):
            c = g.cell[i]
            if not GUN <= c <= FROST:
                continue
            x, y = self.px((i % GW) * T, TOP + (i // GW) * T)
            cx, cy, col = x + 4 * S, y + 4 * S, KCOL[c]
            if c == GUN:
                pygame.draw.circle(s, col, (cx, cy), 3 * S, 6)
                pygame.draw.circle(s, col, (cx, cy), S)
            elif c == BLAST:
                pygame.draw.rect(s, col, (x + S, y + S, 7 * S, 7 * S), 6, border_radius=6)
                pygame.draw.rect(s, col, (x + 3 * S, y + 3 * S, 3 * S, 3 * S))
            else:
                for a, b in (((1, 4), (7, 4)), ((4, 1), (4, 7)), ((2, 2), (6, 6)), ((6, 2), (2, 6))):
                    pygame.draw.line(s, col, (x + a[0] * S, y + a[1] * S), (x + b[0] * S, y + b[1] * S), 5)
            for p in range(1, g.lvl[i]):         # its level: a dot a corner
                pygame.draw.circle(s, WHITE, (x + (6 if p == 1 else 66), y + 6), 6)
        for f in g.foes:
            if not f.on:
                continue
            col = (150, 190, 255) if f.slow else (255, 70, 70) if f.kind == 2 else (255, 230, 70) if f.kind == 1 else (90, 230, 90)
            x, y = self.px(f.x, f.y)
            if f.kind == 2:
                pygame.draw.rect(s, col, (x - 3 * S, y - 3 * S, 7 * S, 7 * S), border_radius=8)
                for ex in (-1, 1):
                    pygame.draw.circle(s, (0, 0, 0), (x + ex * S, y - S), 5)
                pygame.draw.rect(s, (60, 20, 20), (x - 3 * S, y - 5 * S, 7 * S, 6))
                pygame.draw.rect(s, WHITE, (x - 3 * S, y - 5 * S, max(S, 7 * S * f.hp // f.full), 6))
            elif f.kind == 1:
                pygame.draw.line(s, col, (x - 2 * S, y), (x + 2 * S, y), 5)
                pygame.draw.line(s, col, (x, y - 2 * S), (x, y + 2 * S), 5)
                pygame.draw.rect(s, col, (x - S - 4, y - S - 4, 2 * S + 8, 2 * S + 8), 4)
            else:
                pygame.draw.rect(s, col, (x - 2 * S, y - 2 * S, 5 * S, 5 * S), 6, border_radius=4)
                if f.hp * 2 > f.full:
                    pygame.draw.rect(s, col, (x - S // 2 - 2, y - S // 2 - 2, S + 4, S + 4))
        for x0, y0, x1, y1, t, r in g.flashes:
            if not t:
                continue
            if r:
                rad = (r - t * 2) * S
                if rad > 0:
                    pygame.draw.circle(s, (255, 170, 50), self.px(x1, y1), rad, 4)
            else:
                pygame.draw.line(s, (255, 255, 170), self.px(x0, y0), self.px(x1, y1), 3)
        x, y = self.px(g.cx * T, TOP + g.cy * T)
        on = (self.ms // 250) & 1
        pygame.draw.rect(s, WHITE, (x, y, T * S, T * S), 4 if on else 2, border_radius=4)
        # the figures
        here = g.cell[g.cy * GW + g.cx]
        mini.text(s, f"${g.gold}", (self.ox, 8), 56, GOLDC, "topleft")
        hx, hy = self.ox + 200, 22                       # a heart, near enough (as the firmware's)
        pygame.draw.circle(s, RED, (hx + 9, hy + 8), 9)
        pygame.draw.circle(s, RED, (hx + 25, hy + 8), 9)
        pygame.draw.polygon(s, RED, [(hx + 1, hy + 12), (hx + 33, hy + 12), (hx + 17, hy + 30)])
        mini.text(s, str(g.lives), (self.ox + 244, 8), 56, RED, "topleft")
        left = (g.wave_at - self.ms) & 0xFFFFFFFF
        secs = left // 1000 + 1 if 0 < left < 0x80000000 else 0
        mini.text(s, f"WAVE {g.wave}" if g.in_wave else f"WAVE {g.wave + 1} IN {secs}", (self.ox + 380, 8), 56,
                  WHITE, "topleft")
        if GUN <= here <= FROST:
            what = "MAX" if g.lvl[g.cy * GW + g.cx] >= 3 else f"UPGRADE ${KINDS[here][1] * g.lvl[g.cy * GW + g.cx]}"
        else:
            what = f"{KINDS[g.sel][0]} ${KINDS[g.sel][1]}"
        mini.text(s, what, (self.ox + 128 * S - 80, 8), 56, KCOL[g.sel] if not GUN <= here <= FROST else WHITE,
                  "topright")
        # the buttons (touch) and the keys
        self.buttons = []
        by = self.oy + 64 * S + 16
        bx = self.ox
        for k in (GUN, BLAST, FROST):
            r = pygame.Rect(bx, by, 220, 64)
            pygame.draw.rect(s, (32, 39, 51), r, border_radius=12)
            if g.sel == k:
                pygame.draw.rect(s, KCOL[k], r, 4, border_radius=12)
            mini.text(s, f"{KINDS[k][0]} ${KINDS[k][1]}", r.center, 40, KCOL[k])
            self.buttons.append((r, lambda k=k: self._pick(k)))
            bx += 236
        for label, bits in (("NEXT WAVE", HOLD), ("PAUSE", PAUSE)):
            r = pygame.Rect(bx, by, 220, 64)
            pygame.draw.rect(s, (32, 39, 51), r, border_radius=12)
            mini.text(s, label, r.center, 40, WHITE)
            self.buttons.append((r, lambda b=bits: self.tap(b)))
            bx += 236
        mini.text(s, "A build/upgrade · Y or L1/R1 next tower · X next wave · START pause · B back",
                  (self.ox, self.vh - 22), 30, DIM, "midleft")
        mini.text(s, f"BEST: wave {self.best // 100}", (self.ox + 128 * S, self.vh - 22), 30, DIM, "midright")
        if g.over or g.paused:
            r = pygame.Rect(0, 0, 640, 200)
            r.center = (self.vw // 2, self.oy + 36 * S)
            pygame.draw.rect(s, (14, 17, 23), r, border_radius=18)
            pygame.draw.rect(s, WHITE, r, 3, border_radius=18)
            if g.over:
                mini.text(s, "GAME OVER", (r.centerx, r.y + 70), 90, RED)
                mini.text(s, f"wave {g.wave}  ·  A: again", (r.centerx, r.y + 150), 44, WHITE)
            else:
                mini.text(s, "PAUSED", (r.centerx, r.y + 70), 90, WHITE)
                mini.text(s, "START: go on", (r.centerx, r.y + 150), 44, DIM)
        self.close = mini.close_button(s, self.vw)
