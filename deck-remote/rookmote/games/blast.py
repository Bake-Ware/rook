"""The BAKETRIS mode's blast (the Deck's own, not the Android build's): after a 4-line
clear every block left in the well flies off the stack as a body (an impulse
out from the cleared rows, gravity, spin, bouncing off the walls, the floor,
the blocks already landed and each other) and comes to rest wherever it lands,
snapped to a grid cell as the new stack (same colours; a cell already taken
pushes it up). Then any complete rows clear as usual (settle_clear), and a
4-line one blasts again: chains.

Pure Python, no pygame: deterministic for a seed (its own random.Random), a
fixed 1/60 s step (one a frame), and a hash of nearby bodies so a full well's
worth stays well inside a frame. Quick by design: heavy gravity, little
bounce, a block lands after 3 slow steps, and all of it by 1.4 s (a chained
blast 1.1 s): about a second from the blast to the next piece."""

from __future__ import annotations

import math
import random

from . import rules as G

DT = 1 / 60                       # one step a frame
GRAV = -150.0                     # cells / s^2 (y is up, row 0 the floor): heavy, so it comes down decisively
REST, WALL_REST = 0.15, 0.35      # bounce kept off the floor/blocks, off the walls: little
CEIL = G.H - 2                    # the well goes up to H rows; nothing leaves it
SLOW, STILL_STEPS = 3.0, 3        # under this speed for this many steps: it has landed
MAX_STEPS = 84                    # everything snaps by 1.4 s whatever it is doing
CHAIN_STEPS = 66                  # a chained blast: 1.1 s


class Body:
    __slots__ = ("x", "y", "vx", "vy", "ang", "spin", "col", "still")

    def __init__(self, x, y, vx, vy, spin, col):
        self.x, self.y, self.vx, self.vy, self.ang, self.spin, self.col = x, y, vx, vy, 0.0, spin, col
        self.still = 0


class Blast:
    """grid: rows of colour ids (Game.grid, row 0 at the bottom), after the clear.
    rows: the rows that were cleared (where it blew from). Takes every block out
    of grid; step() until done, grid is then the new stack."""

    def __init__(self, grid, rows, seed, max_steps=MAX_STEPS):
        self.max_steps = max_steps
        self.grid = grid
        self.rng = random.Random(repr(seed))          # a tuple seeds it too, the same every run
        r = self.rng.random
        cy = sum(rows) / len(rows) + 0.5 if rows else 2.0
        cx = G.W / 2
        self.bodies: list[Body] = []
        for row in range(G.H):
            for col in range(G.W):
                v = grid[row][col]
                if not v:
                    continue
                grid[row][col] = 0
                dx, dy = col + 0.5 - cx, row + 0.5 - cy
                d = math.hypot(dx, dy) or 1.0
                power = 14 + r() * 14
                vx = dx / d * power + (r() - 0.5) * 8
                vy = abs(dy) / d * power * 0.6 + 20 + r() * 22          # out, and mostly up
                self.bodies.append(Body(float(col), float(row), vx, vy, (r() - 0.5) * 30, v))
        self.count = len(self.bodies)
        self.steps = 0
        self.done = not self.bodies

    # -- the grid as solid cells -----------------------------------------------------
    def solid(self, col, row) -> bool:
        if col < 0 or col >= G.W or row < 0:
            return True
        return row < G.H and self.grid[row][col] != 0

    def snap(self, b: Body) -> None:
        col = min(G.W - 1, max(0, int(round(b.x))))
        row = min(G.H - 1, max(0, int(round(b.y))))
        while row < G.H - 1 and self.grid[row][col]:
            row += 1                                  # taken: settle upward
        if self.grid[row][col]:                       # the column is full to the top: next free one
            for c in sorted(range(G.W), key=lambda c: abs(c - col)):
                for rr in range(G.H):
                    if not self.grid[rr][c]:
                        col, row = c, rr
                        break
                else:
                    continue
                break
        self.grid[row][col] = b.col

    def step(self) -> None:
        if self.done:
            return
        self.steps += 1
        bodies = self.bodies
        for b in bodies:                              # flight
            b.vy += GRAV * DT
            b.x += b.vx * DT
            b.y += b.vy * DT
            b.ang += b.spin * DT
            self._bounds(b)
            self._cells(b)
        buckets: dict = {}                            # each other, nearby only
        for b in bodies:
            buckets.setdefault((int(b.x), int(b.y)), []).append(b)
        for (bx, by), group in buckets.items():
            for ox in (0, 1):
                for oy in (-1, 0, 1):
                    if ox == 0 and oy < 0:
                        continue
                    other = group if (ox, oy) == (0, 0) else buckets.get((bx + ox, by + oy))
                    if not other:
                        continue
                    for i, a in enumerate(group):
                        for b in (other[i + 1:] if other is group else other):
                            self._pair(a, b)
        for b in bodies:                              # and the walls, floor and landed cells have the last word
            self._bounds(b)
            self._cells(b)
        keep = []
        for b in bodies:                              # landed: into the grid
            supported = b.y <= 0.02 or self.solid(int(round(b.x)), int(math.floor(b.y + 0.5)) - 1)
            if abs(b.vx) + abs(b.vy) < SLOW and supported:
                b.still += 1
            else:
                b.still = 0
            if b.still >= STILL_STEPS or self.steps >= self.max_steps:
                self.snap(b)
            else:
                keep.append(b)
        self.bodies = keep
        self.done = not keep

    @staticmethod
    def _bounds(b: Body) -> None:
        if b.x < 0:
            b.x, b.vx = 0.0, -b.vx * WALL_REST
        elif b.x > G.W - 1:
            b.x, b.vx = float(G.W - 1), -b.vx * WALL_REST
        if b.y > CEIL:
            b.y, b.vy = float(CEIL), -abs(b.vy) * WALL_REST
        if b.y < 0:
            b.y = 0.0
            if b.vy < 0:
                b.vy = -b.vy * REST
            b.vx *= 0.85
            b.spin *= 0.7

    def _cells(self, b: Body) -> None:
        """Out of any landed cell it overlaps, along the shallower side."""
        x0, y0 = int(math.floor(b.x)), int(math.floor(b.y))
        for col in (x0, x0 + 1):
            for row in (y0, y0 + 1):
                if col < 0 or col >= G.W or row < 0 or row >= G.H or not self.grid[row][col]:
                    continue
                px = 1 - abs(b.x - col)
                py = 1 - abs(b.y - row)
                if px <= 0 or py <= 0:
                    continue
                if py <= px:
                    if b.y > row:
                        b.y = row + 1.0
                        if b.vy < 0:
                            b.vy = -b.vy * REST
                        b.vx *= 0.85
                        b.spin *= 0.7
                    else:
                        b.y = row - 1.0
                        if b.vy > 0:
                            b.vy = -b.vy * REST
                else:
                    b.x = col + 1.0 if b.x > col else col - 1.0
                    b.vx = -b.vx * WALL_REST

    @staticmethod
    def _pair(a: Body, b: Body) -> None:
        dx, dy = b.x - a.x, b.y - a.y
        px, py = 1 - abs(dx), 1 - abs(dy)
        if px <= 0 or py <= 0:
            return
        if py <= px:                                  # one on the other: stack
            s = 1 if dy >= 0 else -1
            a.y -= s * py / 2
            b.y += s * py / 2
            if (b.vy - a.vy) * s < 0:
                m = (a.vy + b.vy) / 2
                d = (b.vy - a.vy) * REST / 2
                a.vy, b.vy = m + d, m - d
                a.vx, b.vx = a.vx * 0.9, b.vx * 0.9
        else:
            s = 1 if dx >= 0 else -1
            a.x -= s * px / 2
            b.x += s * px / 2
            if (b.vx - a.vx) * s < 0:
                m = (a.vx + b.vx) / 2
                d = (b.vx - a.vx) * REST / 2
                a.vx, b.vx = m + d, m - d
        a.spin, b.spin = -b.spin * 0.6, -a.spin * 0.6


def settle_clear(game: G.Game) -> int:
    """After a blast has settled: complete rows clear and score as a lock with no spin
    would (Game._lock's table, back-to-back, combo, all clear; past 4 rows, 400 a row
    more), and the listener hears it (a 4-row one: another blast). Answers the rows."""
    full = [r for r in range(G.H) if all(game.grid[r])]
    n = len(full)
    if not n:
        game.combo = -1
        return 0
    for k, r in enumerate(full[:4]):
        game.clear_colors[k] = list(game.grid[r])
    keep = [row for r, row in enumerate(game.grid) if r not in full]
    game.grid = keep + [[0] * G.W for _ in range(n)]
    base = [0, 100, 300, 500, 800][min(n, 4)] + 400 * max(0, n - 4)
    b2b_bonus = False
    if n >= 4:
        if game.b2b:
            base = base * 3 // 2
            b2b_bonus = True
        game.b2b = True
    else:
        game.b2b = False
    game.combo += 1
    if game.combo > 0:
        game.score += 50 * game.combo * game.level
    game.score += base * game.level
    pc = not any(any(row) for row in game.grid)
    if pc:
        game.score += (3200 if n >= 4 and b2b_bonus else [0, 800, 1200, 1800, 2000][min(n, 4)]) * game.level
    game.lines += n
    if game.mode in (G.MARATHON, G.BAKETRIS):
        game.level = min(game.lines // 10 + 1, 20)
    if game.listener:
        game.listener.on_lock(min(n, 4), full[:4], 0, b2b_bonus, game.combo, pc, True)
    return n
