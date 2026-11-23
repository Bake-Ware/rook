"""Baketris's rules, ported line for line from /home/bake/baketris Game.java:
SRS with its kick tables and a 180 kick table, the 7-bag (java.util.Random,
bit for bit, so a seed deals the same pieces as the Android build), hold,
lock delay with move reset (15), gravity by level, T-spins (3-corner, front
corners or the 5th kick for a full one), back-to-back, combos, all clears,
and the modes Marathon (150 lines), Sprint (40 lines), Ultra (2 minutes).
tests/test_baketris_rules.py holds it to traces made by Game.java itself."""

from __future__ import annotations

W, H, VIS = 10, 40, 20
I, O, T, S, Z, J, L = range(7)
MARATHON, SPRINT, ULTRA = 0, 1, 2
LOCK_MS = 500.0
LOCK_RESETS = 15
SPRINT_LINES, MARATHON_LINES = 40, 150
ULTRA_MS = 120000.0
BOX = [4, 2, 3, 3, 3, 3, 3]
NAMES = "IOTSZJL"


def _cells():
    base = [["....", "IIII", "....", "...."], ["OO", "OO"], [".T.", "TTT", "..."], [".SS", "SS.", "..."],
            ["ZZ.", ".ZZ", "..."], ["J..", "JJJ", "..."], ["..L", "LLL", "..."]]
    cells = []
    for t in range(7):
        n = BOX[t]
        m = [[base[t][r][c] != "." for c in range(n)] for r in range(n)]
        rots = []
        for _ in range(4):
            rots.append([(c, r) for r in range(n) for c in range(n) if m[r][c]])
            if t != O:
                m = [[m[n - 1 - c][r] for c in range(n)] for r in range(n)]
        cells.append(rots)
    return cells


CELLS = _cells()          # [type][rot] -> [(col, row)] in a y-down box

# SRS kicks, (x, y) with y up. Index: 0R, R0, R2, 2R, 2L, L2, L0, 0L
KICK_JLSTZ = [
    [(0, 0), (-1, 0), (-1, 1), (0, -2), (-1, -2)], [(0, 0), (1, 0), (1, -1), (0, 2), (1, 2)],
    [(0, 0), (1, 0), (1, -1), (0, 2), (1, 2)], [(0, 0), (-1, 0), (-1, 1), (0, -2), (-1, -2)],
    [(0, 0), (1, 0), (1, 1), (0, -2), (1, -2)], [(0, 0), (-1, 0), (-1, -1), (0, 2), (-1, 2)],
    [(0, 0), (-1, 0), (-1, -1), (0, 2), (-1, 2)], [(0, 0), (1, 0), (1, 1), (0, -2), (1, -2)]]
KICK_I = [
    [(0, 0), (-2, 0), (1, 0), (-2, -1), (1, 2)], [(0, 0), (2, 0), (-1, 0), (2, 1), (-1, -2)],
    [(0, 0), (-1, 0), (2, 0), (-1, 2), (2, -1)], [(0, 0), (1, 0), (-2, 0), (1, -2), (-2, 1)],
    [(0, 0), (2, 0), (-1, 0), (2, 1), (-1, -2)], [(0, 0), (-2, 0), (1, 0), (-2, -1), (1, 2)],
    [(0, 0), (1, 0), (-2, 0), (1, -2), (-2, 1)], [(0, 0), (-1, 0), (2, 0), (-1, 2), (2, -1)]]
KICK_180 = [(0, 0), (0, 1), (1, 0), (-1, 0), (1, 1), (-1, 1), (0, -1)]


class JavaRandom:
    """java.util.Random, exactly (the 48-bit LCG and nextInt's rejection loop)."""

    MULT, ADD, MASK = 0x5DEECE66D, 0xB, (1 << 48) - 1

    def __init__(self, seed: int):
        self.seed = (seed ^ self.MULT) & self.MASK

    def _next(self, bits: int) -> int:
        self.seed = (self.seed * self.MULT + self.ADD) & self.MASK
        v = self.seed >> (48 - bits)
        return v - (1 << bits) if v >= 1 << (bits - 1) and bits == 32 else v

    def next_int(self, bound: int) -> int:
        if bound & -bound == bound:                     # a power of two
            return (bound * self._next(31)) >> 31
        while True:
            bits = self._next(31)
            val = bits % bound
            if bits - val + (bound - 1) < (1 << 31):    # Java's int overflow test
                return val

    def next_double(self) -> float:
        return ((self._next(26) << 27) + self._next(27)) * (1.0 / (1 << 53))

    def next_boolean(self) -> bool:
        return self._next(1) != 0


class Listener:
    """What the view hears (Game.Listener)."""

    def on_move(self): pass
    def on_rotate(self): pass
    def on_hold(self): pass
    def on_hard_drop(self, type_, rot, x, from_y, to_y): pass
    def on_lock(self, cleared, rows, spin, b2b_bonus, combo, pc, hard): pass
    def on_end(self, win): pass


class Game:
    def __init__(self):
        self.listener: Listener | None = None
        self.grid = [[0] * W for _ in range(H)]
        self.clear_colors = [[0] * W for _ in range(4)]
        self.mode = MARATHON
        self.rng = JavaRandom(0)
        self.bag = [0] * 7
        self.bag_pos = 7
        self.queue: list[int] = []
        self.cur, self.rot, self.px, self.py = -1, 0, 0, 0
        self.hold = -1
        self.hold_used = False
        self.grav_acc = self.lock_timer = 0.0
        self.resets = self.lowest_y = 0
        self.last_rotate = self.hard_lock = False
        self.last_kick = -1
        self.score = 0
        self.lines, self.level, self.combo, self.pieces = 0, 1, -1, 0
        self.b2b = False
        self.time_ms = 0.0
        self.over = self.won = False

    def reset(self, mode: int, seed: int) -> None:
        self.mode = mode
        self.rng = JavaRandom(seed)
        for row in self.grid:
            row[:] = [0] * W
        self.bag_pos = 7
        self.queue = []
        self._fill_queue()
        self.cur, self.hold, self.hold_used = -1, -1, False
        self.score, self.lines, self.level, self.combo, self.pieces, self.b2b = 0, 0, 1, -1, 0, False
        self.time_ms, self.over, self.won = 0.0, False, False
        self.grav_acc = self.lock_timer = 0.0

    def fits(self, t: int, r: int, x: int, y: int) -> bool:
        g = self.grid
        for c, rr in CELLS[t][r]:
            col, row = x + c, y - rr
            if col < 0 or col >= W or row < 0:
                return False
            if row < H and g[row][col]:
                return False
        return True

    def _from_bag(self) -> int:
        if self.bag_pos >= 7:
            self.bag = list(range(7))
            for i in range(6, 0, -1):
                j = self.rng.next_int(i + 1)
                self.bag[i], self.bag[j] = self.bag[j], self.bag[i]
            self.bag_pos = 0
        v = self.bag[self.bag_pos]
        self.bag_pos += 1
        return v

    def _fill_queue(self) -> None:
        while len(self.queue) < 6:
            self.queue.append(self._from_bag())

    def _spawn(self, t: int) -> bool:
        self.cur, self.rot = t, 0
        self.px = 4 if t == O else 3
        self.py = 22 if t == I else 21
        self.grav_acc = self.lock_timer = 0.0
        self.resets, self.last_rotate, self.last_kick = 0, False, -1
        if not self.fits(self.cur, self.rot, self.px, self.py):
            self._finish(False)
            return False
        if self.fits(self.cur, self.rot, self.px, self.py - 1):
            self.py -= 1
        self.lowest_y = self.py
        return True

    def next_piece(self) -> None:
        self._fill_queue()
        t = self.queue.pop(0)
        self._fill_queue()
        self.hold_used = False
        self._spawn(t)

    def on_ground(self) -> bool:
        return self.cur >= 0 and not self.fits(self.cur, self.rot, self.px, self.py - 1)

    def ghost_y(self) -> int:
        y = self.py
        while self.fits(self.cur, self.rot, self.px, y - 1):
            y -= 1
        return y

    def _active(self) -> bool:
        return self.cur >= 0 and not self.over

    def _after_move(self) -> None:
        if self.lock_timer > 0 or self.on_ground():
            if self.resets < LOCK_RESETS:
                self.lock_timer = 0.0
                self.resets += 1

    def shift(self, dx: int) -> bool:
        if not self._active():
            return False
        if self.fits(self.cur, self.rot, self.px + dx, self.py):
            self.px += dx
            self.last_rotate = False
            self._after_move()
            if self.listener:
                self.listener.on_move()
            return True
        return False

    def rotate(self, d: int) -> bool:
        """d: 1 clockwise, -1 counter-clockwise, 2 half a turn."""
        if not self._active():
            return False
        if self.cur == O:
            if self.listener:
                self.listener.on_rotate()
            return True
        to = (self.rot + (2 if d == 2 else d) + 4) % 4
        if d == 2:
            kicks = KICK_180
        else:
            idx = self.rot * 2 if d == 1 else (7 if self.rot == 0 else self.rot * 2 - 1)
            kicks = KICK_I[idx] if self.cur == I else KICK_JLSTZ[idx]
        for k, (kx, ky) in enumerate(kicks):
            nx, ny = self.px + kx, self.py + ky
            if self.fits(self.cur, to, nx, ny):
                self.px, self.py, self.rot = nx, ny, to
                self.last_rotate = True
                self.last_kick = -1 if d == 2 else k
                self._after_move()
                if self.listener:
                    self.listener.on_rotate()
                return True
        return False

    def soft_step(self) -> bool:
        if not self._active():
            return False
        if self.fits(self.cur, self.rot, self.px, self.py - 1):
            self.py -= 1
            self.score += 1
            self.last_rotate = False
            if self.py < self.lowest_y:
                self.lowest_y, self.resets = self.py, 0
            return True
        return False

    def hard_drop(self) -> None:
        if not self._active():
            return
        frm, d = self.py, 0
        while self.fits(self.cur, self.rot, self.px, self.py - 1):
            self.py -= 1
            d += 1
        if d > 0:
            self.last_rotate = False
        self.score += 2 * d
        if self.listener:
            self.listener.on_hard_drop(self.cur, self.rot, self.px, frm, self.py)
        self.hard_lock = True
        self._lock()
        self.hard_lock = False

    def do_hold(self) -> None:
        if not self._active() or self.hold_used:
            return
        t = self.cur
        if self.hold < 0:
            self.hold = t
            self.next_piece()
        else:
            h, self.hold = self.hold, t
            self._spawn(h)
        self.hold_used = True
        if self.listener:
            self.listener.on_hold()

    def gravity_ms(self) -> float:
        lv = min(self.level, 20)
        return 1000.0 * (0.8 - (lv - 1) * 0.007) ** (lv - 1)

    def update(self, dt: float, soft: bool, sdf: int) -> None:
        if not self._active():
            return
        self.time_ms += dt
        if self.mode == ULTRA and self.time_ms >= ULTRA_MS:
            self.time_ms = ULTRA_MS
            self._finish(True)
            return
        if soft and sdf > 40:
            while self.fits(self.cur, self.rot, self.px, self.py - 1):
                self.py -= 1
                self.score += 1
                self.last_rotate = False
        else:
            interval = self.gravity_ms()
            if soft:
                interval = min(interval / sdf, interval)
            self.grav_acc += dt / interval
            while self.grav_acc >= 1:
                if self.fits(self.cur, self.rot, self.px, self.py - 1):
                    self.py -= 1
                    self.grav_acc -= 1
                    self.last_rotate = False
                    if soft:
                        self.score += 1
                else:
                    self.grav_acc = 0.0
                    break
        if self.py < self.lowest_y:
            self.lowest_y, self.resets = self.py, 0
        if self.on_ground():
            self.lock_timer += dt
            if self.lock_timer >= LOCK_MS or self.resets >= LOCK_RESETS:
                self._lock()
        else:
            self.lock_timer = 0.0

    def _filled(self, c: int, r: int) -> bool:
        return c < 0 or c >= W or r < 0 or self.grid[r][c] != 0

    def _lock(self) -> None:
        all_above = True
        for c, rr in CELLS[self.cur][self.rot]:
            col, row = self.px + c, self.py - rr
            if row < H:
                self.grid[row][col] = self.cur + 1
            if row < VIS:
                all_above = False
        self.pieces += 1
        spin = 0
        if self.cur == T and self.last_rotate:
            corners = [(0, 0), (2, 0), (0, 2), (2, 2)]
            occ = [self._filled(self.px + cx, self.py - cy) for cx, cy in corners]
            if sum(occ) >= 3:
                front = [(0, 1), (1, 3), (2, 3), (0, 2)]
                f = occ[front[self.rot][0]] and occ[front[self.rot][1]]
                spin = 2 if (f or self.last_kick == 4) else 1
        cleared = 0
        rows = [0] * 4
        for r in range(H):
            if cleared >= 4:
                break
            if self._full(r):
                self.clear_colors[cleared] = list(self.grid[r])
                rows[cleared] = r
                cleared += 1
        if cleared > 0:
            w = 0
            for r in range(H):
                if self._full(r):
                    continue
                if w != r:
                    self.grid[w] = list(self.grid[r])
                w += 1
            for r in range(w, H):
                self.grid[r] = [0] * W
        pc = cleared > 0 and self._empty()
        if spin == 2:
            base, difficult = [400, 800, 1200, 1600, 1600][cleared], cleared > 0
        elif spin == 1:
            base, difficult = [100, 200, 400, 400, 400][cleared], cleared > 0
        else:
            base, difficult = [0, 100, 300, 500, 800][cleared], cleared == 4
        b2b_bonus = False
        if cleared > 0:
            if difficult:
                if self.b2b:
                    base = base * 3 // 2
                    b2b_bonus = True
                self.b2b = True
            else:
                self.b2b = False
            self.combo += 1
            if self.combo > 0:
                self.score += 50 * self.combo * self.level
        else:
            self.combo = -1
        self.score += base * self.level
        if pc:
            p = [0, 800, 1200, 1800, 2000][cleared]
            if cleared == 4 and b2b_bonus:
                p = 3200
            self.score += p * self.level
        self.lines += cleared
        if self.mode == MARATHON:
            self.level = min(self.lines // 10 + 1, 20)
        if self.listener:
            self.listener.on_lock(cleared, rows[:cleared], spin, b2b_bonus, self.combo, pc, self.hard_lock)
        if self.mode == SPRINT and self.lines >= SPRINT_LINES:
            self._finish(True)
            return
        if self.mode == MARATHON and self.lines >= MARATHON_LINES:
            self._finish(True)
            return
        if all_above:
            self._finish(False)
            return
        self.next_piece()

    def _full(self, r: int) -> bool:
        return all(self.grid[r])

    def _empty(self) -> bool:
        return not any(any(row) for row in self.grid)

    def _finish(self, win: bool) -> None:
        if self.over:
            return
        self.over, self.won = True, win
        if self.listener:
            self.listener.on_end(win)
