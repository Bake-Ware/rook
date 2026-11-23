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
CAPT = 4                         # extras: a boss come down to hover and beam

# -- extras (the Deck's own, not the firmware's): capture and the bonus stage --------------
BEAM_Y = 66                      # where a capturing boss hovers (its top, pixels)
BEAM_STEPS, BEAM_GROW = 75, 15   # how long it beams, and how long the beam takes to reach down
BONUS_WAVES, BONUS_PER = 5, 8    # a bonus stage: 5 waves of 8 = 40 flyers
BONUS_INTRO, BONUS_GAP, BONUS_SPACING, BONUS_TALLY = 60, 150, 7, 100     # steps
BONUS_HIT, BONUS_PERFECT = 100, 10000
FLYER = [[0x10, 0x38, 0x7C, 0xD6, 0x82, 0x00], [0x10, 0x38, 0xFE, 0x54, 0x28, 0x00]]


def bonus_due(stage: int) -> bool:
    """After clearing this stage, a bonus stage: after stage 2, then every 4th (6, 10, 14...)."""
    return stage == 2 or (stage > 2 and (stage - 2) % 4 == 0)


def flyer_pos(pattern: int, a: int) -> tuple[float, float]:
    """Where a bonus-stage flyer is, a steps after it came in. Five paths; 1 and 3 mirror 0 and 2."""
    import math
    m = pattern in (1, 3)
    p = {1: 0, 3: 2}.get(pattern, pattern)
    if p == 0:                                   # a sweep down and across, wiggling
        x, y = -6 + 1.1 * a, 14 + 0.55 * a + 18 * math.sin(a / 9)
    elif p == 2:                                 # down the middle, a loop, out the side
        if a < 40:
            x, y = 22.0, -8 + 1.5 * a
        elif a < 90:
            t = (a - 40) / 8
            x, y = 22 + 16 * math.sin(t), 52 + 16 * (1 - math.cos(t))
        else:
            x, y = 22 + 16 * math.sin(50 / 8) + (a - 90) * 1.4, 52 + 16 * (1 - math.cos(50 / 8)) - (a - 90) * 0.5
    else:                                        # up from the lower left, rising across
        x, y = -6 + 1.3 * a, 100 - 0.9 * a + 6 * math.sin(a / 5)
    return ((W - FOE_W) - x if m else x), y

BUG = [[0x44, 0x38, 0xFE, 0xBA, 0x82, 0x00], [0x82, 0x7C, 0xFE, 0x38, 0x44, 0x00]]
BOSS = [[0x38, 0x7C, 0xD6, 0xFE, 0x54, 0x82], [0x38, 0x7C, 0xD6, 0xFE, 0x54, 0x44]]
SHIP = [0x08, 0x00, 0x08, 0x00, 0x1C, 0x00, 0x9C, 0x80, 0xFF, 0x80, 0xDD, 0x80]


class Foe:
    __slots__ = ("state", "hp", "boss", "x", "y", "vx", "cap", "beam")

    def __init__(self, state=GONE, hp=0, boss=0):
        self.state, self.hp, self.boss, self.x, self.y, self.vx = state, hp, boss, 0, 0, 0
        self.cap = False          # extras: holds a captured ship
        self.beam = 0             # extras: CAPT: steps of beam left (0: still coming down)


class Rules:
    def __init__(self, rand=mini.esp_random, extras=False):
        self.rand = rand
        self.extras = extras      # the Deck's additions; off: exactly the firmware's game
        self.foes = [Foe() for _ in range(N)]
        self.shots = [[False, 0, 0] for _ in range(4 if extras else 2)]
        self.dual = False         # two ships side by side
        self.carry = None         # [x, y, boss]: the ship being pulled up a beam
        self.rescue = None        # [x, y]: a freed ship on its way down to join
        self.bonus = None         # a bonus stage: {t, spawned, hits, total, tally, award}
        self.flyers = []          # [pattern, age, x, y]
        self.msg = ("", 0)        # (what happened, the frame it shows until)
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
        self.now = ms
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
            self.ship_x = max(0, min(W - SHIP_W - (SHIP_W + 1 if self.dual else 0), self.ship_x))
            if keys & (DROP | ROTATE | ROTATE_BACK | DOWN):
                if self.fire_wait > 0:
                    self.fire_wait -= 1
                elif self.dual:                       # two at once, side by side
                    free = [s for s in self.shots if not s[0]]
                    if len(free) >= 2:
                        free[0][:] = [True, self.ship_x + SHIP_W // 2, SHIP_Y - 3]
                        free[1][:] = [True, self.ship_x + SHIP_W + 1 + SHIP_W // 2, SHIP_Y - 3]
                        self.fire_wait = 7
                elif sum(1 for s in self.shots if s[0]) < 2:
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
        if self.extras:
            self.carry_rescue(ms)
            if self.bonus is not None:
                self.bonus_step(ms)
                for b in self.booms:
                    if b[2]:
                        b[2] -= 1
                return
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
                if (self.extras and foes[i].boss and not foes[i].cap and not self.dual and self.carry is None
                        and self.rescue is None and not any(f.cap or f.state == CAPT for f in foes)
                        and self.rnd(2) == 0):
                    foes[i].state, foes[i].beam = CAPT, 0       # comes down to beam the ship up
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
            elif f.state == CAPT:
                self.capt_step(ms, i, f, alive)
            elif f.state == BACK:
                tx, ty = self.slot_x(i) * 16, self.slot_y(i) * 16
                f.x += 24 if f.x < tx - 24 else -24 if f.x > tx + 24 else tx - f.x
                f.y += 30
                if f.y >= ty:
                    f.state = FORM
        for s in self.shots:
            if not s[0]:
                continue
            if self.extras and self.shoot_captive(s):
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
                if self.extras:
                    self.boss_down(i, f, x, y, diving)
                break
        if self.extras and alive and due(ms, self.safe_until):
            self.ship_hits(ms)
        elif alive and due(ms, self.safe_until):
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
                if self.extras and bonus_due(self.stage):
                    self.start_bonus()
                else:
                    self.stage += 1
                    self.new_stage(ms)
        for b in self.booms:
            if b[2]:
                b[2] -= 1

    # -- extras ---------------------------------------------------------------------------
    def say(self, text: str, frames: int = 45) -> None:
        self.msg = (text, self.frame + frames)

    def captive_at(self, i: int) -> tuple[int, int]:
        """Where a boss's captive is drawn and hit: just above it."""
        return self.foe_x(i) - 1, self.foe_y(i) - SHIP_H - 1

    def capt_step(self, ms, i, f, alive) -> None:
        """A capturing boss: over the ship, down to BEAM_Y, then the beam, then back up to its slot."""
        if f.beam < 0:
            tx, ty = self.slot_x(i) * 16, self.slot_y(i) * 16
            f.x += 24 if f.x < tx - 24 else -24 if f.x > tx + 24 else tx - f.x
            f.y -= 30
            if f.y <= ty:
                f.state = FORM
            return
        if f.beam == 0:
            tx = (self.ship_x + SHIP_W // 2 - FOE_W // 2) * 16
            f.x += 24 if f.x < tx - 24 else -24 if f.x > tx + 24 else tx - f.x
            f.y = min(BEAM_Y * 16, f.y + 26)
            if f.y >= BEAM_Y * 16:
                f.beam = BEAM_STEPS
            return
        if self.carry is not None and self.carry[2] == i:
            return                                   # holding still while it pulls the ship up
        f.beam -= 1
        bx = cdiv(f.x, 16) + FOE_W // 2
        if (alive and due(ms, self.safe_until) and BEAM_STEPS - f.beam >= BEAM_GROW
                and self.ship_x - 1 <= bx <= self.ship_x + SHIP_W):          # caught in it
            self.carry = [self.ship_x, SHIP_Y, i]
            self.dead_until = (ms + 60000) & 0xFFFFFFFF                     # gone until the carry ends
            for b in self.bombs:
                b[0] = False
            self.say("CAUGHT!", 60)
            return
        if f.beam <= 0:
            f.beam = -1                              # done: back up to its place

    def carry_rescue(self, ms) -> None:
        c = self.carry
        if c is not None:
            f = self.foes[c[2]]
            tx, ty = cdiv(f.x, 16) - 1, cdiv(f.y, 16) - SHIP_H - 1
            c[0] += 1 if c[0] < tx else -1 if c[0] > tx else 0
            c[1] -= 2
            if c[1] <= ty:                           # up beside the boss: it holds it now
                self.carry = None
                f.cap, f.beam = True, -1
                self.lives -= 1
                self.say("SHIP TAKEN", 60)
                if self.lives <= 0:
                    self.lives = 0
                    self.over = True
                else:
                    self.ship_x = (W - SHIP_W) // 2
                    self.dead_until = (ms + 900) & 0xFFFFFFFF
                    self.safe_until = (ms + 2700) & 0xFFFFFFFF
        r = self.rescue
        if r is not None:
            alive = due(ms, self.dead_until)
            tx = min(self.ship_x + SHIP_W + 1, W - SHIP_W)
            r[0] += max(-2, min(2, tx - r[0]))
            r[1] += max(-3, min(3, SHIP_Y - r[1]))
            if alive and r[1] == SHIP_Y and abs(r[0] - tx) <= 2:
                self.rescue = None
                if self.ship_x > W - 2 * SHIP_W - 1:
                    self.ship_x = W - 2 * SHIP_W - 1
                self.dual = True
                self.say("DOUBLE SHIP!", 60)

    def shoot_captive(self, s) -> bool:
        """A shot that hits a held captive: it is lost."""
        for i, f in enumerate(self.foes):
            if f.state == GONE or not f.cap:
                continue
            x, y = self.captive_at(i)
            if s[1] < x or s[1] > x + SHIP_W or s[2] > y + SHIP_H or s[2] + 4 < y:
                continue
            s[0] = False
            f.cap = False
            self.boom(x + 4, y + 3)
            self.say("CAPTIVE LOST")
            return True
        return False

    def boss_down(self, i, f, x, y, diving) -> None:
        """A boss shot down: a captive it held goes free only if it was out diving."""
        if self.carry is not None and self.carry[2] == i:        # mid-carry: the ship drops back, single
            c, self.carry = self.carry, None
            self.rescue = None
            self.ship_x = max(0, min(W - SHIP_W, c[0]))
            self.dead_until = 0
            self.safe_until = (self.now + 1500) & 0xFFFFFFFF
            self.say("SAVED!")
            return
        if not f.cap:
            return
        f.cap = False
        cx, cy = x - 1, y - SHIP_H - 1
        if diving:
            self.rescue = [cx, cy]
            self.say("RESCUED!", 60)
        else:                                    # in formation: the captive goes down with it
            self.boom(cx + 4, cy + 3)
            self.say("CAPTIVE LOST")

    def ship_hits(self, ms) -> None:
        """The firmware's bombs and divers against the ship, which may be two."""
        sx = self.ship_x
        halves = [sx] + ([sx + SHIP_W + 1] if self.dual else [])
        hit = [False] * len(halves)
        for b in self.bombs:
            if not b[0] or not (b[2] + 2 >= SHIP_Y + 1 and b[2] <= SHIP_Y + SHIP_H):
                continue
            for k, hx in enumerate(halves):
                if hx <= b[1] < hx + SHIP_W:
                    b[0] = False
                    hit[k] = True
        for f in self.foes:
            if f.state != DIVE:
                continue
            x, y = cdiv(f.x, 16), cdiv(f.y, 16)
            if y + FOE_H <= SHIP_Y + 1 or y >= SHIP_Y + SHIP_H:
                continue
            got = [k for k, hx in enumerate(halves) if not (x + FOE_W <= hx or x >= hx + SHIP_W)]
            if not got:
                continue
            if f.cap:                                # it took its captive down with it
                f.cap = False
            f.state = GONE
            self.boom(x + 3, y + 3)
            for k in got:
                hit[k] = True
        if not any(hit):
            return
        for k, h in enumerate(hit):
            if h:
                self.boom(halves[k] + 4, SHIP_Y + 3)
        if self.dual and not all(hit):              # one half gone: the other flies on
            if hit[0]:
                self.ship_x = halves[1]
            self.dual = False
            self.safe_until = (ms + 1500) & 0xFFFFFFFF
            self.say("HALF LOST")
            return
        self.dual = False
        self.boom(sx + 1, SHIP_Y + 1)
        for b in self.bombs:
            b[0] = False
        self.dead_until = (ms + 1100) & 0xFFFFFFFF
        self.safe_until = (ms + 2900) & 0xFFFFFFFF
        self.lives -= 1
        if self.lives <= 0:
            self.over = True

    def start_bonus(self) -> None:
        self.bonus = {"t": 0, "spawned": 0, "hits": 0, "total": BONUS_WAVES * BONUS_PER, "tally": 0, "award": 0}
        self.flyers = []
        for b in self.bombs:
            b[0] = False
        self.foes = [Foe() for _ in range(N)]

    def bonus_step(self, ms) -> None:
        """Waves fly through on their paths; nothing fires, dives or hurts; hits are counted."""
        b = self.bonus
        b["t"] += 1
        if b["tally"]:
            b["tally"] -= 1
            if not b["tally"]:
                self.bonus = None
                self.stage += 1
                self.new_stage(ms)
            return
        for w in range(BONUS_WAVES):
            for k in range(BONUS_PER):
                if b["t"] == BONUS_INTRO + w * BONUS_GAP + k * BONUS_SPACING:
                    x, y = flyer_pos(w, 0)
                    self.flyers.append([w, 0, x, y])
                    b["spawned"] += 1
        live = []
        for f in self.flyers:
            f[1] += 1
            f[2], f[3] = flyer_pos(f[0], f[1])
            x, y = int(f[2]), int(f[3])
            if f[1] > 5 and (x < -12 or x > W + 5 or y > H + 5 or y < -12):
                continue
            shot = None
            for s in self.shots:
                if s[0] and not (s[1] < x - 1 or s[1] > x + FOE_W or s[2] > y + FOE_H or s[2] + 4 < y):
                    shot = s
                    break
            if shot:
                shot[0] = False
                b["hits"] += 1
                self.boom(x + 3, y + 3)
                continue
            live.append(f)
        self.flyers = live
        if b["spawned"] == b["total"] and not self.flyers:
            perfect = b["hits"] == b["total"]
            b["award"] = BONUS_PERFECT if perfect else b["hits"] * BONUS_HIT
            self.points += b["award"]
            b["tally"] = BONUS_TALLY

    def start(self, ms):
        self.points, self.lives, self.stage = 0, 3, 1
        self.ship_x = (W - SHIP_W) // 2
        self.over = self.paused = self.quit_asked = False
        self.dead_until = self.safe_until = 0
        self.dual, self.carry, self.rescue, self.bonus, self.flyers, self.msg = False, None, None, None, [], ("", 0)
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
CAPTIVE = (255, 80, 80)
FLYC = [(120, 255, 200), (255, 160, 220), (255, 230, 120), (160, 200, 255), (200, 255, 120)]
HURT, SHIPC, SHOT, HOT = (255, 150, 60), (130, 225, 255), (255, 255, 130), (255, 90, 50)
BG, FIELD, WHITE, DIM, GOLD, RED = (14, 17, 23), (4, 5, 14), (240, 240, 240), (140, 150, 168), (255, 215, 0), (230, 92, 92)


class SwarmGame(mini.Screen):
    NAME = "Swarm"

    def __init__(self, size=(1280, 800), bests=None, rumble=None, rand=mini.esp_random, extras=True):
        super().__init__(size, bests, rumble)
        self.g = Rules(rand, extras)
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
            mid = self.g.ship_x + (SHIP_W + (SHIP_W + 1 if self.g.dual else 0)) / 2
            self.pulse |= DROP | (LEFT if fx < mid - 1.5 else RIGHT if fx > mid + 1.5 else 0)
        super().update(dt)
        if self.g.lives < self._lives:
            self.rumble(1.0, 0.8, 250)
        self._lives = self.g.lives
        m = self.g.msg[0] if self.g.frame < self.g.msg[1] else ""
        if m and m != getattr(self, "_said", ""):
            self.rumble(*{"CAUGHT!": (0.6, 1.0, 400), "DOUBLE SHIP!": (0.4, 0.8, 200),
                          "HALF LOST": (1.0, 0.6, 200)}.get(m, (0.3, 0.5, 80)))
        self._said = m

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

    def draw_beam(self, s, cx, top, grown) -> None:
        bottom = top + (SHIP_Y + SHIP_H - top) * grown
        ov = pygame.Surface((W * S, H * S), pygame.SRCALPHA)
        a = 90 + 40 * ((self.g.frame // 2) & 1)
        steps = 8
        for k in range(steps):                             # bands that widen as they go down
            y0 = top + (bottom - top) * k / steps
            y1 = top + (bottom - top) * (k + 1) / steps
            w0, w1 = 1 + 5 * k / steps, 1 + 5 * (k + 1) / steps
            col = (120, 170, 255, a) if (k + self.g.frame // 3) % 2 else (190, 140, 255, a)
            pygame.draw.polygon(ov, col, [((cx - w0) * S, y0 * S), ((cx + w0) * S, y0 * S),
                                          ((cx + w1) * S, y1 * S), ((cx - w1) * S, y1 * S)])
        s.blit(ov, (self.ox, self.oy))

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
            if f.cap:                                       # its captive, held above it, in the enemy's colour
                s.blit(self.bitmap(SHIP, SHIP_W, CAPTIVE, True), self.p(*g.captive_at(i)))
            if f.state == CAPT and f.beam > 0:              # the beam: widening stripes down to the ship
                self.draw_beam(s, x + FOE_W / 2, y + FOE_H, min(1.0, (BEAM_STEPS - f.beam) / BEAM_GROW))
        for w, a, fx, fy in g.flyers:
            col = FLYC[w % len(FLYC)]
            s.blit(self.bitmap(FLYER[(g.frame // 4) & 1], FOE_W, col), self.p(int(fx), int(fy)))
        if g.carry is not None:                            # pulled up the beam, turning to the enemy's colour
            s.blit(self.bitmap(SHIP, SHIP_W, CAPTIVE if g.frame & 4 else SHIPC, True), self.p(g.carry[0], g.carry[1]))
        if g.rescue is not None:                           # on its way down to join, flashing
            s.blit(self.bitmap(SHIP, SHIP_W, WHITE if g.frame & 2 else SHIPC, True), self.p(*g.rescue))
        alive = due(self.ms, g.dead_until)
        safe = not due(self.ms, g.safe_until)
        if not g.over and alive and not (safe and (g.frame & 2)):
            s.blit(self.bitmap(SHIP, SHIP_W, SHIPC, True), self.p(g.ship_x, SHIP_Y))
            if g.dual:
                s.blit(self.bitmap(SHIP, SHIP_W, SHIPC, True), self.p(g.ship_x + SHIP_W + 1, SHIP_Y))
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
        mini.text(s, "BONUS" if g.bonus is not None else f"STAGE {g.stage}", (lx, self.oy + 360), 48, GOLD, "topright")
        for k in range(max(0, min(3, g.lives - 1))):           # the ships in reserve
            s.blit(self.bitmap(SHIP, SHIP_W, SHIPC, True), (lx - 70 - k * 70, self.oy + 430))
        rx = self.ox + W * S + 40
        for i, line in enumerate(["d-pad / stick: move", "A, up, Y: fire (hold)", "START or X: pause", "B: back",
                                  "touch: hold a finger", "", "a boss's beam takes", "your ship: shoot that",
                                  "boss as it dives to", "win it back, double", f"bonus stage after 2,",
                                  "then every 4th"]):
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
        elif g.bonus is not None:
            b = g.bonus
            if b["tally"]:
                r = pygame.Rect(0, 0, W * S - 40, 300)
                r.center = (cx, self.oy + 64 * S)
                pygame.draw.rect(s, BG, r, border_radius=18)
                pygame.draw.rect(s, GOLD, r, 3, border_radius=18)
                mini.text(s, "BONUS STAGE", (cx, r.y + 50), 52, GOLD)
                mini.text(s, f"HITS {b['hits']} / {b['total']}", (cx, r.y + 120), 56, WHITE)
                perfect = b["hits"] == b["total"]
                mini.text(s, "PERFECT!" if perfect else "BONUS", (cx, r.y + 190), 52, ROWC[0] if perfect else DIM)
                mini.text(s, f"+{b['award']:,}", (cx, r.y + 250), 60, GOLD)
            elif b["t"] < BONUS_INTRO:
                mini.text(s, "BONUS STAGE", (cx, self.oy + 58 * S), 72, GOLD)
                mini.text(s, "shoot all you can", (cx, self.oy + 70 * S), 40, WHITE)
            else:
                mini.text(s, f"HITS {b['hits']}", (cx, self.oy + 60), 44, GOLD)
        elif g.enter_y < 0:
            mini.text(s, f"STAGE {g.stage}", (cx, self.oy + 76 * S), 64, GOLD)
        if not g.over and g.frame < g.msg[1] and g.msg[0]:
            col = {"CAUGHT!": RED, "SHIP TAKEN": RED, "CAPTIVE LOST": RED, "HALF LOST": RED}.get(g.msg[0], GOLD)
            mini.text(s, g.msg[0], (cx, self.oy + 92 * S), 64, col)
        self.close = mini.close_button(s, self.vw)
