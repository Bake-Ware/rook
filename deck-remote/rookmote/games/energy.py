"""The player's energy (the Deck's own, for Baketris): how much they are doing in a row.

Every input counts (act), clears and their kind give a boost; the level follows
what the last second or so has been like: up fast, down slowly once they ease
off. Four steps (0-3) that the music (a rate step and a hotter arrangement each:
audio.heat_up) and the effects follow. Pure arithmetic: tested on its own."""

from __future__ import annotations

import math

TAU = 1.2                 # s: the actions-per-second average's memory
FLOOR, SPAN = 1.4, 4.6    # actions a second for nothing, and over how many more it reaches the top: with the
                          # pace's peaks, the steps come at about 2, 3.3 and 4.4 inputs a second
ATTACK = 1.6              # how fast it climbs to where the pace says (per s)
DECAY = 0.17              # how fast it falls back (per s): from the top to nothing in ~6 s
STEPS = (0.25, 0.5, 0.75)
NAMES = ["", "CHARGED", "SURGING", "OVERDRIVE"]


class Energy:
    def __init__(self):
        self.e = 0.0          # 0..1
        self.rate = 0.0       # actions a second, averaged
        self.pending = 0.0

    def act(self, weight: float = 1.0) -> None:
        self.pending += weight

    def boost(self, x: float) -> None:
        self.e = min(1.0, self.e + x)

    def clear(self, cleared: int, spin: int, b2b: bool, combo: int, pc: bool) -> None:
        """A lock that cleared: its kind lifts the energy at once."""
        x = (0.0, 0.05, 0.09, 0.13, 0.28)[min(cleared, 4)]
        if spin:
            x += 0.10
        if b2b:
            x += 0.08
        if combo > 0:
            x += 0.03 * min(combo, 6)
        if pc:
            x += 0.30
        self.boost(x)

    def update(self, dt_ms: float) -> None:
        s = max(0.0, dt_ms) / 1000.0
        self.rate = self.rate * math.exp(-s / TAU) + self.pending / TAU
        self.pending = 0.0
        target = min(1.0, max(0.0, (self.rate - FLOOR) / SPAN))
        if target > self.e:
            self.e = min(target, self.e + ATTACK * s)
        else:
            self.e = max(target, self.e - DECAY * s)

    @property
    def step(self) -> int:
        return sum(1 for t in STEPS if self.e >= t)

    def reset(self) -> None:
        self.e = self.rate = self.pending = 0.0
