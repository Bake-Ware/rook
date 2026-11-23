"""2048, the remote's (firmware-remote/src/g2048.cpp), on the Deck.

Rules line for line: slide and merge (a tile merges once a move), a new tile
(2, or 4 one time in ten) after a move that moved something, 2048 wins (and
play goes on), stuck is over; DROP (A) starts again when over, QUIT (B) leaves.
Drawn big in the gamepad build's colours, with the new tile's pop."""

from __future__ import annotations

import pygame

from . import mini
from .mini import DOWN, DROP, LEFT, QUIT, RIGHT, ROTATE


class Rules:
    def __init__(self, rand=mini.esp_random):
        self.rand = rand
        self.g = [0] * 16
        self.points = self.best = self.pop_until = self.won_until = 0
        self.pop_at = -1
        self.over = self.won = self.quit_asked = False
        self.prev_keys = 0

    @staticmethod
    def at(line, k, d):
        if d == 0:
            return line * 4 + k
        if d == 1:
            return line * 4 + 3 - k
        if d == 2:
            return k * 4 + line
        return (3 - k) * 4 + line

    def slide(self, d: int, ms: int) -> bool:
        moved = False
        g = self.g
        for line in range(4):
            out = [0, 0, 0, 0]
            n = 0
            merged = False
            for k in range(4):
                v = g[self.at(line, k, d)]
                if not v:
                    continue
                if n and out[n - 1] == v and not merged:
                    out[n - 1] += 1
                    self.points = (self.points + (1 << out[n - 1])) & 0xFFFFFFFF
                    if out[n - 1] == 11 and not self.won:
                        self.won = True
                        self.won_until = (ms + 4000) & 0xFFFFFFFF
                    merged = True
                else:
                    out[n] = v
                    n += 1
                    merged = False
            for k in range(4):
                i = self.at(line, k, d)
                if g[i] != out[k]:
                    moved = True
                g[i] = out[k]
        return moved

    def spawn(self, ms: int) -> None:
        free = self.g.count(0)
        if not free:
            return
        pick = self.rand() % free
        for i in range(16):
            if self.g[i]:
                continue
            if pick:
                pick -= 1
                continue
            self.g[i] = 1 if self.rand() % 10 else 2
            self.pop_at = i
            self.pop_until = (ms + 140) & 0xFFFFFFFF
            return

    def stuck(self) -> bool:
        g = self.g
        for i in range(16):
            if not g[i]:
                return False
            if i % 4 < 3 and g[i] == g[i + 1]:
                return False
            if i < 12 and g[i] == g[i + 4]:
                return False
        return True

    def start(self, ms: int) -> None:
        self.g = [0] * 16
        self.points = 0
        self.over = self.won = self.quit_asked = False
        self.won_until = 0
        self.prev_keys = 0xFFFF
        self.spawn(ms)
        self.spawn(ms)
        self.pop_at = -1

    @staticmethod
    def _due(ms, t) -> bool:
        return ((ms - t) & 0xFFFFFFFF) < 0x80000000      # (int32_t)(ms - t) >= 0

    def tick(self, ms: int, keys: int) -> bool:
        pressed = keys & ~self.prev_keys & 0xFFFF
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
        d = 0 if pressed & LEFT else 1 if pressed & RIGHT else 2 if pressed & ROTATE else 3 if pressed & DOWN else -1
        if d >= 0 and self.slide(d, ms):
            self.spawn(ms)
            if self.points > self.best:
                self.best = self.points
            self.over = self.stuck()
            return True
        if self.pop_at >= 0 and self._due(ms, self.pop_until):
            self.pop_at = -1
            return True
        if self.won_until and self._due(ms, self.won_until):
            self.won_until = 0
            return True
        return False

    def quit(self) -> bool:
        return self.quit_asked

    def score(self) -> int:
        return self.points


TILE = {1: (238, 228, 218), 2: (237, 224, 200), 3: (242, 177, 121), 4: (245, 149, 99), 5: (246, 124, 95),
        6: (246, 94, 59), 7: (237, 207, 114), 8: (237, 204, 97), 9: (237, 200, 80), 10: (237, 197, 63),
        11: (237, 194, 46)}
BIG = (120, 80, 200)
BG, BOARD, EMPTY = (14, 17, 23), (60, 58, 70), (82, 80, 92)
WHITE, DARK, DIM, GOLD, RED = (249, 246, 242), (60, 54, 48), (140, 150, 168), (237, 194, 46), (230, 92, 92)


def number(v: int) -> str:
    return f"{v // 1000}k" if v >= 10000 else str(v)


class Game2048(mini.Screen):
    NAME = "2048"

    def __init__(self, size=(1280, 800), bests=None, rumble=None, rand=mini.esp_random):
        super().__init__(size, bests, rumble)
        self.g = Rules(rand)
        self.g.best = self.best
        self.g.start(self.ms)
        self.cell = 150
        self.gap = 14
        side = 4 * self.cell + 5 * self.gap
        self.board = pygame.Rect(140, (self.vh - side) // 2, side, side)
        self.swipe = None
        self.close = None
        self._last_won = False

    def update(self, dt):
        before = self.g.points
        super().update(dt)
        if self.g.points > before:
            self.rumble(0.3, 0.5, 40)
        if self.g.won and not self._last_won:
            self.rumble(1.0, 1.0, 300)
        self._last_won = self.g.won

    def touch_down(self, pid, x, y):
        if self.close and self.close.collidepoint(x, y):
            self.done = True
            return
        if self.g.over:
            self.tap(DROP)
            return
        self.swipe = (pid, x, y)

    def touch_up(self, pid, x, y):
        if not self.swipe or self.swipe[0] != pid:
            return
        dx, dy = x - self.swipe[1], y - self.swipe[2]
        self.swipe = None
        if max(abs(dx), abs(dy)) < 40:
            return
        self.tap((RIGHT if dx > 0 else LEFT) if abs(dx) > abs(dy) else (DOWN if dy > 0 else ROTATE))

    def draw(self, s):
        s.fill(BG)
        g, b, c, gap = self.g, self.board, self.cell, self.gap
        pygame.draw.rect(s, BOARD, b, border_radius=18)
        grow = 1.0
        if g.pop_at >= 0:
            left = (g.pop_until - self.ms) & 0xFFFFFFFF
            grow = 1.0 - min(140, left if left < 0x80000000 else 0) / 140 * 0.6
        for i in range(16):
            x = b.x + gap + (i % 4) * (c + gap)
            y = b.y + gap + (i // 4) * (c + gap)
            e = g.g[i]
            r = pygame.Rect(x, y, c, c)
            if not e:
                pygame.draw.rect(s, EMPTY, r, border_radius=10)
                continue
            if i == g.pop_at:
                pygame.draw.rect(s, EMPTY, r, border_radius=10)
                k = int(c * grow)
                r = pygame.Rect(0, 0, k, k)
                r.center = (x + c // 2, y + c // 2)
            pygame.draw.rect(s, TILE.get(e, BIG), r, border_radius=10)
            t = number(1 << e)
            size = 84 if len(t) <= 2 else 68 if len(t) == 3 else 54
            mini.text(s, t, r.center, int(size * r.w / c), DARK if e < 12 else WHITE)          # as the gamepad build: dark on its colours
        px = b.right + 70
        mini.text(s, "2048", (px, b.y + 10), 110, GOLD, "topleft")
        mini.text(s, "SCORE", (px, b.y + 150), 34, DIM, "topleft")
        mini.text(s, f"{g.points:,}", (px, b.y + 180), 72, WHITE, "topleft")
        mini.text(s, "BEST", (px, b.y + 270), 34, DIM, "topleft")
        mini.text(s, f"{max(self.best, g.points):,}", (px, b.y + 300), 72, WHITE, "topleft")
        if g.over:
            mini.text(s, "GAME OVER", (px, b.y + 410), 72, RED, "topleft")
            mini.text(s, "A: again", (px, b.y + 470), 40, DIM, "topleft")
        elif g.won_until:
            mini.text(s, "2048! YOU WIN", (px, b.y + 410), 64, GOLD, "topleft")
        mini.text(s, "d-pad, stick or swipe: slide", (px, b.bottom - 66), 30, DIM, "topleft")
        mini.text(s, "B: back to the remote", (px, b.bottom - 30), 30, DIM, "topleft")
        self.close = mini.close_button(s, self.vw)
