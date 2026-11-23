"""Baketris's music, rendered as Music.java does it: grunge loops of public-domain
melodies (Korobeiniki, folk, 19th c.; Grieg's "In the Hall of the Mountain King",
1875; Petzold's Minuet in G, c. 1725; Beethoven's "Ode to Joy", 1824) with the
original distorted guitar, bass and drum arrangement. 24 kHz mono.

The voice buffers are float32 (array 'f'), as Java's float[] are. Pure Python and
slow (tens of seconds a song): audio.py renders in another process and caches."""

from __future__ import annotations

import math
from array import array

from .rules import JavaRandom

RATE = 24000
NAMES = ["KOROBEINIKI", "MOUNTAIN KING", "MINUET IN G", "ODE TO JOY"]


class Song:
    def __init__(self, bpm, bar_len, mel, roots, chug, kicks, snares):
        self.bpm, self.bar_len, self.mel, self.roots = bpm, bar_len, mel, roots
        self.chug, self.kicks, self.snares = chug, kicks, snares


SONGS = [
    Song(138, 8, [
        (76, 2), (71, 1), (72, 1), (74, 2), (72, 1), (71, 1),
        (69, 2), (69, 1), (72, 1), (76, 2), (74, 1), (72, 1),
        (71, 3), (72, 1), (74, 2), (76, 2),
        (72, 2), (69, 2), (69, 4),
        (0, 1), (74, 2), (77, 1), (81, 2), (79, 1), (77, 1),
        (76, 3), (72, 1), (76, 2), (74, 1), (72, 1),
        (71, 2), (71, 1), (72, 1), (74, 2), (76, 2),
        (72, 2), (69, 2), (69, 4)],
        [40, 45, 40, 45, 38, 36, 40, 45],
        [1, 0, 1, 1, 0, 1, 1, 1], [0, 3, 4], [2, 6]),
    Song(132, 8, [
        (71, 1), (73, 1), (74, 1), (76, 1), (78, 1), (74, 1), (78, 2),
        (77, 1), (73, 1), (77, 2), (76, 1), (72, 1), (76, 2),
        (71, 1), (73, 1), (74, 1), (76, 1), (78, 1), (74, 1), (78, 1), (83, 1),
        (81, 1), (78, 1), (74, 1), (78, 1), (81, 4),
        (66, 1), (68, 1), (69, 1), (71, 1), (73, 1), (69, 1), (73, 2),
        (72, 1), (68, 1), (72, 2), (71, 1), (67, 1), (71, 2),
        (66, 1), (68, 1), (69, 1), (71, 1), (73, 1), (69, 1), (73, 1), (78, 1),
        (76, 1), (73, 1), (69, 1), (73, 1), (76, 4)],
        [35, 42, 35, 38, 42, 37, 42, 45],
        [1, 1, 1, 1, 1, 1, 1, 1], [0, 2, 4, 6], [2, 6]),
    Song(138, 6, [
        (74, 2), (67, 1), (69, 1), (71, 1), (72, 1),
        (74, 2), (67, 2), (67, 2),
        (76, 2), (72, 1), (74, 1), (76, 1), (78, 1),
        (79, 2), (67, 2), (67, 2),
        (72, 2), (74, 1), (72, 1), (71, 1), (69, 1),
        (71, 2), (72, 1), (71, 1), (69, 1), (67, 1),
        (66, 2), (67, 1), (69, 1), (71, 1), (67, 1),
        (69, 6),
        (74, 2), (67, 1), (69, 1), (71, 1), (72, 1),
        (74, 2), (67, 2), (67, 2),
        (76, 2), (72, 1), (74, 1), (76, 1), (78, 1),
        (79, 2), (67, 2), (67, 2),
        (72, 2), (74, 1), (72, 1), (71, 1), (69, 1),
        (71, 2), (72, 1), (71, 1), (69, 1), (67, 1),
        (69, 2), (71, 1), (69, 1), (67, 1), (66, 1),
        (67, 6)],
        [43, 43, 48, 43, 45, 43, 38, 38, 43, 43, 48, 43, 45, 43, 38, 43],
        [1, 0, 1, 1, 1, 1], [0, 3], [2, 4]),
    Song(126, 8, [
        (76, 2), (76, 2), (77, 2), (79, 2), (79, 2), (77, 2), (76, 2), (74, 2),
        (72, 2), (72, 2), (74, 2), (76, 2), (76, 3), (74, 1), (74, 4),
        (76, 2), (76, 2), (77, 2), (79, 2), (79, 2), (77, 2), (76, 2), (74, 2),
        (72, 2), (72, 2), (74, 2), (76, 2), (74, 3), (72, 1), (72, 4),
        (74, 2), (74, 2), (76, 2), (72, 2), (74, 2), (76, 1), (77, 1), (76, 2), (72, 2),
        (74, 2), (76, 1), (77, 1), (76, 2), (74, 2), (72, 2), (74, 2), (67, 4),
        (76, 2), (76, 2), (77, 2), (79, 2), (79, 2), (77, 2), (76, 2), (74, 2),
        (72, 2), (72, 2), (74, 2), (76, 2), (74, 3), (72, 1), (72, 4)],
        [48, 43, 48, 43, 48, 43, 48, 48, 43, 48, 43, 43, 48, 43, 48, 48],
        [1, 0, 1, 1, 0, 1, 1, 1], [0, 3, 4], [2, 6]),
]


def mtof(m: int) -> float:
    return 440.0 * math.pow(2, (m - 69) / 12.0)


def _round(x: float) -> int:
    """Math.round: floor(x + 0.5)."""
    return math.floor(x + 0.5)


def render(s: Song) -> array:
    """One song's loop, int16 at RATE."""
    beat = 60.0 / s.bpm
    e8 = _round(RATE * beat / 2)
    bars, bl = len(s.roots), s.bar_len
    total = 2 * bars * bl * e8
    L = total + RATE * 2
    zero = bytes(4 * L)
    lead, gtr, bass, drums = array("f", zero), array("f", zero), array("f", zero), array("f", zero)
    r = JavaRandom(1861)
    intro_bars = min(4, bars // 2)
    for pass_ in range(2):
        base = pass_ * bars * bl * e8
        t8 = 0
        for note, eighths in s.mel:
            if note > 0:
                st, ln = base + t8 * e8, eighths * e8
                saw(lead, st, ln, mtof(note), 0.5, eighths >= 3, 0.9, 1.5)
                if pass_ == 1:
                    saw(lead, st, ln, mtof(note - 12), 0.3, False, 0.9, 1.5)
            t8 += eighths
        for bar in range(bars):
            root = s.roots[bar]
            intro = pass_ == 0 and bar < intro_bars
            bs = base + bar * bl * e8
            if (pass_ == 0 and bar == intro_bars) or (pass_ == 1 and (bar == 0 or bar == bars // 2)):
                crash(drums, bs, r)
            for k in range(bl):
                st = bs + k * e8
                if k == 0:
                    chord(gtr, st, e8 * 2, root, 0.55, 0.92, 1.2)
                elif s.chug[k] == 1:
                    chord(gtr, st, e8, root, 0.4, 0.5, 6)
                if not intro:
                    saw(bass, st, e8, mtof(root), 0.6, False, 0.82, 2)
                kick_ = k in s.kicks
                snare_ = k in s.snares
                if intro:
                    kick_ = k == 0 or k == bl // 2
                    snare_ = bar == intro_bars - 1 and k >= bl // 2
                if kick_:
                    kick(drums, st)
                if snare_:
                    snare(drums, st, r)
                if not intro and k % 2 == 1:
                    noise(drums, st, 0.05, 0.05, 30, True, r)
    bus(gtr, 9, 0.5)
    bus(lead, 4, 0.55)
    bus(bass, 3, 0.16)
    d = int(RATE * 0.17)
    mixed = array("f", zero)
    f32 = array("f", [0.0])

    def f(x):                                   # a float op, as Java's: rounded to float32
        f32[0] = x
        return f32[0]
    for i in range(L):
        echo_ = f(lead[i - d] * f(0.2)) if i >= d else 0.0
        mixed[i] = f(f(f(f(gtr[i] * f(0.26)) + f(f(lead[i] + echo_) * f(0.3))) + f(bass[i] * f(0.42))) + drums[i])
    for i in range(total, L):
        mixed[i - total] += mixed[i]
    out = array("h", bytes(2 * total))
    norm = math.tanh(1.4)
    tanh = math.tanh
    for i in range(total):
        out[i] = int(tanh(mixed[i] * 1.4) / norm * 0.9 * 32767)
    return out


def saw(b: array, start, ln, f, amp, vib, gate_frac, decay) -> None:
    """Two slightly detuned saws."""
    p1, p2 = 0.0, 0.37
    gate, rel, atk = int(ln * gate_frac), int(RATE * 0.012), int(RATE * 0.003)
    n = min(gate + rel, len(b) - start)
    sin, exp, floor, tau = math.sin, math.exp, math.floor, 2 * math.pi
    for i in range(n):
        t = i / RATE
        ff = f
        if vib and t > 0.18:
            ff *= 1 + 0.008 * sin(tau * 5.2 * t)
        p1 += ff * 1.003 / RATE
        p1 -= floor(p1)
        p2 += ff * 0.997 / RATE
        p2 -= floor(p2)
        v = (2 * p1 - 1) + (2 * p2 - 1)
        if i < atk:
            env = i / atk
        else:
            env = exp(-decay * t)
            if env < 0.55:
                env = 0.55
        if i >= gate:
            env *= 1 - (i - gate) / rel
        b[start + i] += _f32(v * 0.5 * env * amp)
    return None


def chord(b, start, ln, root, amp, gate_frac, decay) -> None:
    """Power chord: root, fifth, octave."""
    saw(b, start, ln, mtof(root), amp * 0.5, False, gate_frac, decay)
    saw(b, start, ln, mtof(root + 7), amp * 0.4, False, gate_frac, decay)
    saw(b, start, ln, mtof(root + 12), amp * 0.3, False, gate_frac, decay)


def bus(b: array, drive, lp_a) -> None:
    """DC/rumble high-pass, tanh overdrive, two-pole low-pass "cabinet"."""
    hp = y1 = y2 = 0.0
    norm = math.tanh(drive)
    tanh = math.tanh
    for i in range(len(b)):
        x = b[i]
        hp += 0.008 * (x - hp)
        x = tanh((x - hp) * drive) / norm
        y1 += lp_a * (x - y1)
        y2 += lp_a * (y1 - y2)
        b[i] = y2


def kick(b: array, start) -> None:
    n = int(RATE * 0.18)
    ph = 0.0
    r = JavaRandom(start)
    sin, exp, tanh, tau = math.sin, math.exp, math.tanh, 2 * math.pi
    for i in range(min(n, len(b) - start)):
        t = i / n
        f = 42 + 120 * exp(-t * 9)
        ph += f / RATE
        v = sin(tau * ph) * exp(-t * 3.5) * 0.85
        if i < RATE * 0.004:
            v += (r.next_double() * 2 - 1) * 0.25
        b[start + i] += _f32(tanh(v * 1.6))


def snare(b: array, start, r: JavaRandom) -> None:
    noise(b, start, 0.28, 0.4, 11, False, r)
    n = int(RATE * 0.07)
    ph = 0.0
    sin, exp, tau = math.sin, math.exp, 2 * math.pi
    for i in range(min(n, len(b) - start)):
        ph += 185.0 / RATE
        b[start + i] += _f32(sin(tau * ph) * exp(-i / n * 4) * 0.25)


def crash(b: array, start, r: JavaRandom) -> None:
    noise(b, start, 1.6, 0.16, 2.2, True, r)


def noise(b: array, start, dur, amp, decay, hp, r: JavaRandom) -> None:
    n = int(RATE * dur)
    lp = 0.0
    exp = math.exp
    nd = r.next_double
    for i in range(min(n, len(b) - start)):
        x = nd() * 2 - 1
        lp += 0.25 * (x - lp)
        v = x - lp if hp else x
        b[start + i] += _f32(v * exp(-decay * i / RATE) * amp)


_F = array("f", [0.0])


def _f32(x: float) -> float:
    _F[0] = x
    return _F[0]
