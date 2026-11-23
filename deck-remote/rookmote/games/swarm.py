"""Swarm, the remote's shooter (firmware-remote/src/swarm.cpp), on the Deck.

Rules line for line: a formation of 5 x 4 (the top row bosses that take two
hits) that sways and dives at the ship, more often and faster each stage;
bombs, two shots on screen, a held fire key firing about four a second, three
ships, a moment safe after losing one, 500 for a stage cleared; 33 ms steps
on a 64 x 128 field. Drawn 6x, upright, in the gamepad build's colours.
Left/right (d-pad or stick) move, A (or up, down, Y) fires, Start or X pauses,
B leaves; touch: hold a finger, the ship goes toward it and fires."""

from __future__ import annotations

import pygame

from . import mini
from .mini import DOWN, DROP, HOLD, LEFT, PAUSE, QUIT, RIGHT, ROTATE, ROTATE_BACK, cdiv, due

W, H = 64, 128
COLS, ROWS = 5, 4
N = COLS * ROWS
CELL_X, CELL_Y = 11, 10
FORM_X, FORM_Y = (W - COLS * CELL_X) // 2 + 2, 18
FOE_W, FOE_H, SHIP_W, SHIP_H, SHIP_Y = 7, 6, 9, 6, 118
STEP_MS = 33
GONE, FORM, DIVE, BACK = 0, 1, 2, 3

BUG = [[0x44, 0x38, 0xFE, 0xBA, 0x82, 0x00], [0x82, 0x7C, 0xFE, 0x38, 0x44, 0x00]]
BOSS = [[0x38, 0x7C, 0xD6, 0xFE, 0x54, 0x82], [0x38, 0x7C, 0xD6, 0xFE, 0x54, 0x44]]
SHIP = [0x08, 0x00, 0x08, 0x00, 0x1C, 0x00, 0x9C, 0x80, 0xFF, 0x80, 0xDD, 0x80]


class Foe:
    __slots__ = ("state", "hp", "boss", "x", "y", "vx")

    def __init__(self, state=GONE, hp=0, boss=0):
        self.state, self.hp, self.boss, self.x, self.y, self.vx = state, hp, boss, 0, 0, 0


class Rules:
    def __init__(self, rand=mini.esp_random):
        self.rand = rand
        self.foes = [Foe() for _ in range(N)]
        self.shots = [[False, 0, 0] for _ in range(2)]
        self.bombs = [[False, 0, 0] for _ in range(4)]
        self.booms = [[0, 0, 0] for _ in range(4)]          # x y t
        self.star_x, self.star_y = [0] * 12, [0] * 12
        self.ship_x = self.lives = 0
        self.stage, self.sway, self.sway_dir, self.enter_y, self.frame, self.fire_wait = 1, 0, 1, 0, 0, 0
        self.points = self.next_step = self.dead_until = self.safe_until = self.clear_at = self.next_dive = 0
        self.over = self.paused = self.quit_asked = False
        self.prev_keys = 0

    def rnd(self, n):
        return self.rand() % n

    def slot_x(self, i):
        return FORM_X + (i % COLS) * CELL_X + self.sway

    def slot_y(self, i):
        return FORM_Y + (i // COLS) * CELL_Y + self.enter_y

    def foe_x(self, i):
        f = self.foes[i]
        return self.slot_x(i) if f.state == FORM else cdiv(f.x, 16)

    def foe_y(self, i):
        f = self.foes[i]
        return self.slot_y(i) if f.state == FORM else cdiv(f.y, 16)

    def boom(self, x, y):
        b = self.booms[0]
        for c in self.booms:
            if c[2] < b[2]:
                b = c
        b[:] = [x, y, 8]

    def new_stage(self, ms):
        self.foes = [Foe(FORM, 2 if i < COLS else 1, 1 if i < COLS else 0) for i in range(N)]
        for s in self.shots:
            s[0] = False
        for b in self.bombs:
            b[0] = False
        self.enter_y, self.sway, self.clear_at = -64, 0, 0
        self.next_dive = (ms + 2600) & 0xFFFFFFFF

    def step(self, ms, keys):
        self.frame += 1
        for i in range(12):
            if (self.frame & 1) or i < 4:
                self.star_y[i] = (self.star_y[i] + 1) % H
        if self.enter_y < 0:
            self.enter_y += 2
        if not self.frame & 3:
            self.sway += self.sway_dir
            if self.sway >= 4 or self.sway <= -4:
                self.sway_dir = -self.sway_dir
        alive = due(ms, self.dead_until)
        if alive:
            if keys & LEFT:
                self.ship_x -= 2
            if keys & RIGHT:
                self.ship_x += 2
            self.ship_x = max(0, min(W - SHIP_W, self.ship_x))
            if keys & (DROP | ROTATE | ROTATE_BACK | DOWN):
                if self.fire_wait > 0:
                    self.fire_wait -= 1
                else:
                    for s in self.shots:
                        if not s[0]:
                            s[:] = [True, self.ship_x + SHIP_W // 2, SHIP_Y - 3]
                            self.fire_wait = 7
                            break
            else:
                self.fire_wait = 0
        for s in self.shots:
            if s[0]:
                s[2] -= 5
                if s[2] < 9:
                    s[0] = False
        for b in self.bombs:
            if b[0]:
                b[2] += 2 + self.stage // 4
                if b[2] > H:
                    b[0] = False
        foes = self.foes
        if alive and self.enter_y == 0 and not self.clear_at and due(ms, self.next_dive):
            at = self.rnd(N)
            for k in range(N):
                i = (at + k) % N
                if foes[i].state != FORM:
                    continue
                foes[i].x = self.slot_x(i) * 16
                foes[i].y = self.slot_y(i) * 16
                foes[i].vx = 20 if self.rnd(2) else -20
                foes[i].state = DIVE
                break
            gap = 2300 - self.stage * 220
            self.next_dive = (ms + max(gap, 500) + self.rnd(700)) & 0xFFFFFFFF
        top, fall = min(44, 26 + self.stage * 2), min(52, 26 + self.stage * 3)
        for i in range(N):
            f = foes[i]
            if f.state == DIVE:
                f.vx += 3 if cdiv(f.x, 16) < self.ship_x + 1 else -3
                f.vx = max(-top, min(top, f.vx))
                f.x += f.vx
                f.y += fall
                if f.x < 0:
                    f.x, f.vx = 0, -f.vx
                if f.x > (W - FOE_W) * 16:
                    f.x, f.vx = (W - FOE_W) * 16, -f.vx
                if alive and cdiv(f.y, 16) < 92 and not self.rnd(34):
                    for b in self.bombs:
                        if not b[0]:
                            b[:] = [True, cdiv(f.x, 16) + 3, cdiv(f.y, 16) + FOE_H]
                            break
                if cdiv(f.y, 16) > H:
                    f.state = BACK
                    f.y = -12 * 16
            elif f.state == BACK:
                tx, ty = self.slot_x(i) * 16, self.slot_y(i) * 16
                f.x += 24 if f.x < tx - 24 else -24 if f.x > tx + 24 else tx - f.x
                f.y += 30
                if f.y >= ty:
                    f.state = FORM
        for s in self.shots:
            if not s[0]:
                continue
            for i in range(N):
                f = foes[i]
                if f.state == GONE:
                    continue
                x, y = self.foe_x(i), self.foe_y(i)
                if s[1] < x - 1 or s[1] > x + FOE_W or s[2] > y + FOE_H or s[2] + 4 < y:
                    continue
                s[0] = False
                f.hp -= 1
                if f.hp:
                    break
                diving = f.state != FORM
                self.points += (400 if diving else 150) if f.boss else (100 if diving else 50)
                f.state = GONE
                self.boom(x + 3, y + 3)
                break
        if alive and due(ms, self.safe_until):
            hit = False
            sx = self.ship_x
            for b in self.bombs:
                if b[0] and sx <= b[1] < sx + SHIP_W and b[2] + 2 >= SHIP_Y + 1 and b[2] <= SHIP_Y + SHIP_H:
                    b[0] = False
                    hit = True
            for f in foes:
                if f.state != DIVE:
                    continue
                x, y = cdiv(f.x, 16), cdiv(f.y, 16)
                if x + FOE_W <= sx or x >= sx + SHIP_W or y + FOE_H <= SHIP_Y + 1 or y >= SHIP_Y + SHIP_H:
                    continue
                f.state = GONE
                self.boom(x + 3, y + 3)
                hit = True
            if hit:
                self.boom(sx + 4, SHIP_Y + 3)
                self.boom(sx + 1, SHIP_Y + 1)
                for b in self.bombs:
                    b[0] = False
                self.dead_until = (ms + 1100) & 0xFFFFFFFF
                self.safe_until = (ms + 2900) & 0xFFFFFFFF
                self.lives -= 1
                if self.lives <= 0:
                    self.over = True
        if not any(f.state != GONE for f in foes):
            if not self.clear_at:
                self.clear_at = (ms + 1000) & 0xFFFFFFFF
            elif due(ms, self.clear_at):
                self.points += 500
                self.stage += 1
                self.new_stage(ms)
        for b in self.booms:
            if b[2]:
                b[2] -= 1

    def start(self, ms):
        self.points, self.lives, self.stage = 0, 3, 1
        self.ship_x = (W - SHIP_W) // 2
        self.over = self.paused = self.quit_asked = False
        self.dead_until = self.safe_until = 0
        self.fire_wait = 7
        self.prev_keys = 0xFFFF
        for i in range(12):
            self.star_x[i] = self.rnd(W)
            self.star_y[i] = self.rnd(H)
        for b in self.booms:
            b[2] = 0
        self.new_stage(ms)
        self.next_step = ms

    def tick(self, ms, keys) -> bool:
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
        if pressed & (PAUSE | HOLD):
            self.paused = not self.paused
            self.next_step = ms
            return True
        if self.paused or not due(ms, self.next_step):
            return False
        self.next_step = (ms + STEP_MS) if ((ms - self.next_step) & 0xFFFFFFFF) > 200 else (self.next_step + STEP_MS)
        self.next_step &= 0xFFFFFFFF
        self.step(ms, keys)
        return True

    def quit(self):
        return self.quit_asked

    def score(self):
        return self.points


S = 6
ROWC = [(255, 70, 100), (255, 200, 60), (70, 230, 100), (70, 200, 255)]
HURT, SHIPC, SHOT, HOT = (255, 150, 60), (130, 225, 255), (255, 255, 130), (255, 90, 50)
BG, FIELD, WHITE, DIM, GOLD, RED = (14, 17, 23), (4, 5, 14), (240, 240, 240), (140, 150, 168), (255, 215, 0), (230, 92, 92)


class SwarmGame(mini.Screen):
    NAME = "Swarm"

    def __init__(self, size=(1280, 800), bests=None, rumble=None, rand=mini.esp_random):
        super().__init__(size, bests, rumble)
        self.g = Rules(rand)
        self.g.start(self.ms)
        self.ox = (self.vw - W * S) // 2
        self.oy = (self.vh - H * S) // 2 + 4
        self.fingers: dict = {}
        self.sprites: dict = {}
        self.close = None
        self._lives = self.g.lives

    def update(self, dt):
        if self.fingers:                            # touch: toward the finger, and fire
            fx = list(self.fingers.values())[-1]
            mid = self.g.ship_x + SHIP_W / 2
            self.pulse |= DROP | (LEFT if fx < mid - 1.5 else RIGHT if fx > mid + 1.5 else 0)
        super().update(dt)
        if self.g.lives < self._lives:
            self.rumble(1.0, 0.8, 250)
        self._lives = self.g.lives

    def touch_down(self, pid, x, y):
        if self.close and self.close.collidepoint(x, y):
            self.done = True
            return
        if self.g.over:
            self.tap(DROP)
            return
        self.fingers[pid] = (x - self.ox) / S

    def touch_move(self, pid, x, y):
        if pid in self.fingers:
            self.fingers[pid] = (x - self.ox) / S

    def touch_up(self, pid, x, y):
        self.fingers.pop(pid, None)

    def host_pause(self):
        super().host_pause()
        self.fingers.clear()

    def bitmap(self, rows, w, col, two_bytes=False):
        key = (tuple(rows), col)
        spr = self.sprites.get(key)
        if spr is None:
            h = len(rows) // (2 if two_bytes else 1)
            spr = pygame.Surface((w * S, h * S), pygame.SRCALPHA)
            for r in range(h):
                bits = (rows[2 * r] << 8 | rows[2 * r + 1]) if two_bytes else rows[r] << 8
                for c in range(w):
                    if bits & (0x8000 >> c):
                        spr.fill(col, (c * S, r * S, S - 1, S - 1))
            self.sprites[key] = spr
        return spr

    def p(self, x, y):
        return self.ox + x * S, self.oy + y * S

    def draw(self, s):
        g = self.g
        s.fill(BG)
        pygame.draw.rect(s, FIELD, (self.ox, self.oy, W * S, H * S))
        for i in range(12):
            s.fill((150, 160, 200) if i < 4 else (70, 80, 120), (*self.p(g.star_x[i], g.star_y[i]), S - 2, S - 2))
        f2 = (g.frame // 10) & 1
        for i in range(N):
            f = g.foes[i]
            if f.state == GONE:
                continue
            wing = f2 if f.state == FORM else (g.frame // 3) & 1
            hurt = f.boss and f.hp < 2
            col = HURT if hurt else ROWC[(i // COLS) % ROWS]
            x, y = g.foe_x(i), g.foe_y(i)
            if y < -FOE_H or y > H:
                continue
            s.blit(self.bitmap(BOSS[wing] if f.boss else BUG[wing], FOE_W, col), self.p(x, y))
            if hurt:
                s.fill(FIELD, (*self.p(x + 3, y + 2), S, S))
        alive = due(self.ms, g.dead_until)
        safe = not due(self.ms, g.safe_until)
        if not g.over and alive and not (safe and (g.frame & 2)):
            s.blit(self.bitmap(SHIP, SHIP_W, SHIPC, True), self.p(g.ship_x, SHIP_Y))
        for on, x, y in g.shots:
            if on:
                s.fill(SHOT, (*self.p(x, y), S - 2, 4 * S))
        for on, x, y in g.bombs:
            if on:
                for k, dx in enumerate(((y // 2) & 1, ((y // 2) + 1) & 1, (y // 2) & 1)):
                    s.fill(HOT, (*self.p(x + dx, y + k), S, S))
        for x, y, t in g.booms:
            if t:
                r = ((8 - t) // 2 + 1) * S
                pygame.draw.circle(s, (255, 240, 150) if t > 4 else (255, 140, 40), self.p(x, y), r, 4)
        s.fill(BG, (self.ox, self.oy - 40, W * S, 40))
        # the figures, beside the field
        lx = self.ox - 40
        mini.text(s, "SWARM", (lx, self.oy + 10), 72, ROWC[0], "topright")
        mini.text(s, "SCORE", (lx, self.oy + 120), 34, DIM, "topright")
        mini.text(s, f"{g.points:,}", (lx, self.oy + 150), 64, WHITE, "topright")
        mini.text(s, "BEST", (lx, self.oy + 240), 34, DIM, "topright")
        mini.text(s, f"{max(self.best, g.points):,}", (lx, self.oy + 270), 64, WHITE, "topright")
        mini.text(s, f"STAGE {g.stage}", (lx, self.oy + 360), 48, GOLD, "topright")
        for k in range(max(0, min(3, g.lives - 1))):           # the ships in reserve
            s.blit(self.bitmap(SHIP, SHIP_W, SHIPC, True), (lx - 70 - k * 70, self.oy + 430))
        rx = self.ox + W * S + 40
        for i, line in enumerate(["d-pad / stick: move", "A, up, Y: fire (hold)", "START or X: pause", "B: back",
                                  "touch: hold a finger"]):
            mini.text(s, line, (rx, self.oy + 20 + i * 40), 32, DIM, "topleft")
        cx = self.ox + W * S // 2
        if g.over:
            r = pygame.Rect(0, 0, W * S - 40, 260)
            r.center = (cx, self.oy + 66 * S)
            pygame.draw.rect(s, BG, r, border_radius=18)
            pygame.draw.rect(s, WHITE, r, 3, border_radius=18)
            mini.text(s, "GAME OVER", (cx, r.y + 60), 64, RED)
            mini.text(s, f"{g.points:,}", (cx, r.y + 130), 56, WHITE)
            mini.text(s, "A: again", (cx, r.y + 200), 40, DIM)
        elif g.paused:
            mini.text(s, "PAUSED", (cx, self.oy + 61 * S), 72, WHITE)
        elif g.enter_y < 0:
            mini.text(s, f"STAGE {g.stage}", (cx, self.oy + 76 * S), 64, GOLD)
        self.close = mini.close_button(s, self.vw)
