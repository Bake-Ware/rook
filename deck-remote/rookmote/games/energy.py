"""The player's energy (the Deck's own, for Baketris): how hard they are going.

Pieces placed a second drive it: each lock is a push at once (PUSH) and feeds an
average over a couple of seconds whose level the energy climbs to fast; about one
piece a second holds CHARGED, 1.5 SURGING, 2 or more OVERDRIVE, and three or four
quick drops reach CHARGED from nothing. Other inputs (moves, turns) add a little
on top past a busy pace; clears lift it at once by their kind. It falls back
slowly once they ease off. Four steps (0-3) that the music (a rate step and a
hotter arrangement each: audio.heat_up) and the effects follow. Pure arithmetic:
tested on its own."""

from __future__ import annotations

import math

TAU = 2.0                 # s: the pieces-a-second average's memory
INPUT_TAU = 1.2           # s: the other inputs' average
FLOOR, SPAN = 0.5, 2.0    # pieces a second for nothing, and over how many more it reaches the top
INPUT_FLOOR, INPUT_GAIN = 2.5, 0.04   # past this many other inputs a second, each more adds this much
PUSH = 0.075              # what a lock adds at once
ATTACK = 2.0              # how fast it climbs to where the pace says (per s)
DECAY = 0.17              # how fast it falls back (per s): from the top to nothing in ~6 s
STEPS = (0.25, 0.5, 0.75)
NAMES = ["", "CHARGED", "SURGING", "OVERDRIVE"]


class Energy:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.e = 0.0           # 0..1
        self.pps = 0.0         # pieces a second, averaged
        self.aps = 0.0         # other inputs a second, averaged
        self.pending = 0.0
        self.pieces = 0

    def act(self, weight: float = 1.0) -> None:
        """A move, a turn, a hold: a little."""
        self.pending += weight

    def piece(self) -> None:
        """A piece placed (locked: dropped or let down): the main thing."""
        self.pieces += 1
        self.boost(PUSH)

    def boost(self, x: float) -> None:
        self.e = min(1.0, self.e + x)

    def clear(self, cleared: int, spin: int, b2b: bool, combo: int, pc: bool) -> None:
        """A lock that cleared: its kind lifts the energy at once."""
        x = (0.0, 0.04, 0.07, 0.10, 0.22)[min(cleared, 4)]
        if spin:
            x += 0.08
        if b2b:
            x += 0.06
        if combo > 0:
            x += 0.025 * min(combo, 6)
        if pc:
            x += 0.25
        self.boost(x)

    def update(self, dt_ms: float) -> None:
        s = max(0.0, dt_ms) / 1000.0
        self.pps = self.pps * math.exp(-s / TAU) + self.pieces / TAU
        self.aps = self.aps * math.exp(-s / INPUT_TAU) + self.pending / INPUT_TAU
        self.pieces, self.pending = 0, 0.0
        target = (self.pps - FLOOR) / SPAN + max(0.0, self.aps - INPUT_FLOOR) * INPUT_GAIN
        target = min(1.0, max(0.0, target))
        if target > self.e:
            self.e = min(target, self.e + ATTACK * s)
        else:
            self.e = max(target, self.e - DECAY * s)

    @property
    def step(self) -> int:
        return sum(1 for t in STEPS if self.e >= t)
