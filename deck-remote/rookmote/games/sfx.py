"""Baketris's sounds, synthesized as Sfx.java does it, sample for sample: each skin
its own set of gameplay sounds (MOVE ... KABOOM), the menu/meta sounds shared.
Wave: 0 sine, 1 square, 2 triangle, 3 noise, 4 low-passed noise, 5 high-passed noise.

Pure Python and slow (seconds per set): audio.py runs it once, in another
process, and caches the result; nothing here runs in the frame loop."""

from __future__ import annotations

import math
from array import array

from .rules import JavaRandom

RATE = 44100
MOVE, ROT, HOLD, LOCK, HARD, C1, C2, C3, C4, KABOOM, N = range(11)
SETS = 7
SHARED = ("spin", "pc", "over", "win", "tick", "go", "level")

_M, _A, _K = 0x5DEECE66D, 0xB, (1 << 48) - 1
_D = 1.0 / (1 << 53)


def _clamp_short(x: float) -> int:
    """(short) Math.max(-32767, Math.min(32767, x)): clamp, then truncate toward zero."""
    if x > 32767:
        return 32767
    if x < -32767:
        return -32767
    return int(x)


def tone(dur, f0, f1, wave, vol, decay) -> array:
    n = max(1, int(RATE * dur))
    out = array("h", bytes(2 * n))
    ph = lp = 0.0
    seed = (7 ^ _M) & _K                    # new Random(7), its nextDouble inlined
    atk, rel = int(RATE * 0.0015), int(RATE * 0.006)
    exp, floor, sin, tau = math.exp, math.floor, math.sin, 2 * math.pi
    sweep = f1 < f0 and wave == 0
    for i in range(n):
        t = i / n
        f = f0 + (f1 - f0) * t
        if sweep:
            f = f1 + (f0 - f1) * exp(-t * 5)
        ph += f / RATE
        fr = ph - floor(ph)
        if wave == 0:
            v = sin(tau * fr)
        elif wave == 1:
            v = 1 if fr < 0.5 else -1
        elif wave == 2:
            v = 4 * abs(fr - 0.5) - 1
        else:
            seed = (seed * _M + _A) & _K
            a = seed >> 22
            seed = (seed * _M + _A) & _K
            x = ((a << 27) + (seed >> 21)) * _D * 2 - 1
            if wave == 3:
                v = x
            elif wave == 4:
                lp += 0.07 * (x - lp)
                v = lp * 4
            else:
                lp += 0.2 * (x - lp)
                v = (x - lp) * 1.4
        env = exp(-decay * t)
        if i < atk:
            env *= i / atk
        if n - i < rel:
            env *= (n - i) / rel
        x = v * env * vol * 32767
        out[i] = 32767 if x > 32767 else -32767 if x < -32767 else int(x)
    return out


def mix_at(a: array, b: array, off: int) -> array:
    n = max(len(a), len(b) + off)
    out = array("h", bytes(2 * n))
    la, lb = len(a), len(b)
    for i in range(n):
        j = i - off
        v = (a[i] if i < la else 0) + (b[j] if 0 <= j < lb else 0)
        out[i] = 32767 if v > 32767 else -32767 if v < -32767 else v
    return out


def mix(a: array, b: array) -> array:
    return mix_at(a, b, 0)


def cat(a: array, b: array) -> array:
    return a + b


def bell(dur, f, vol) -> array:
    return mix(mix(tone(dur, f, f, 0, vol, 6), tone(dur * 0.7, f * 2.76, f * 2.76, 0, vol * 0.5, 9)),
               tone(dur * 0.5, f * 5.4, f * 5.4, 0, vol * 0.25, 14))


def arp(fs, each, wave, vol) -> array:
    s = array("h")
    for x in fs:
        s = cat(s, tone(each, x, x, wave, vol, 1.5))
    return s


def crackle(dur, count, vol, seed) -> array:
    r = JavaRandom(seed)
    s = array("h", bytes(2 * int(RATE * dur)))
    n = len(s)
    for _ in range(count):
        at = r.next_int(max(1, n - RATE // 200))
        a = vol * (0.3 + r.next_double()) * (1 - at / n)
        j = 0
        while j < RATE // 400 and at + j < n:
            s[at + j] = _clamp_short(s[at + j] + (r.next_double() * 2 - 1) * a * 32767)
            j += 1
    return s


def glitch(dur, vol, seed) -> array:
    r = JavaRandom(seed)
    s = array("h")
    seg = 0.012
    t = 0.0
    while t < dur:
        f = 300 + r.next_int(3200)
        s = cat(s, tone(seg, f, f, 1 if r.next_boolean() else 3, vol, 0))
        t += seg
    return s


def crush(s: array, bits: int, hold: int) -> array:
    out = array("h", bytes(2 * len(s)))
    q = 1 << (16 - bits)
    last = 0
    for i in range(len(s)):
        if i % hold == 0:
            v = s[i]
            last = (v // q if v >= 0 else -((-v) // q)) * q      # Java's division truncates
        out[i] = last
    return out


def echo(s: array, delay, fb, taps) -> array:
    d = int(RATE * delay)
    out = array("h", bytes(2 * (len(s) + d * taps)))
    g = 1.0
    n = len(out)
    for t in range(taps + 1):
        for i in range(len(s)):
            j = i + t * d
            if j < n:
                out[j] = _clamp_short(out[j] + s[i] * g)
        g *= fb
    return out


def reverse(s: array) -> array:
    out = array("h", s)
    out.reverse()
    return out


def clip(s: array, drive) -> array:
    norm = math.tanh(drive)
    tanh = math.tanh
    out = array("h", bytes(2 * len(s)))
    for i in range(len(s)):
        out[i] = int(tanh(s[i] / 32767.0 * drive) / norm * 32000)
    return out


def build(skin: int) -> list:
    """Sfx.build: one skin's ten sounds."""
    w: list = [None] * N
    R = RATE
    up = [523, 659, 784, 1047, 1319, 1568, 2093]
    if skin == 1:                                       # RETRO: crunchy 8-bit squares
        w[MOVE] = crush(tone(0.018, 1046, 1046, 1, 0.12, 0), 4, 2)
        w[ROT] = crush(cat(tone(0.02, 784, 784, 1, 0.14, 0), tone(0.02, 1175, 1175, 1, 0.14, 0)), 4, 2)
        w[HOLD] = crush(arp([523, 784, 1046], 0.03, 1, 0.14), 4, 2)
        w[LOCK] = crush(mix(tone(0.04, 0, 0, 3, 0.25, 6), tone(0.05, 110, 110, 1, 0.2, 4)), 3, 4)
        w[HARD] = clip(crush(mix(tone(0.09, 0, 0, 3, 0.5, 5), tone(0.15, 95, 35, 1, 0.55, 4)), 3, 4), 1.4)
        for k in range(1, 5):
            notes = up[:k + 2]
            w[C1 + k - 1] = clip(crush(mix(arp(notes, 0.035, 1, 0.16), tone(0.12 + 0.05 * k, 0, 0, 3, 0.35, 6)), 4, 2), 1.3)
        w[KABOOM] = clip(crush(mix(mix(tone(1.3, 0, 0, 3, 0.75, 2.4), tone(0.6, 70, 25, 1, 0.5, 3)),
                                   tone(1.0, 55, 20, 0, 0.9, 2.2)), 3, 6), 1.8)
    elif skin == 2:                                     # GLASS: bells, tinks, shatter
        w[MOVE] = bell(0.06, 2637, 0.08)
        w[ROT] = mix(bell(0.12, 1760, 0.12), bell(0.1, 2637, 0.06))
        w[HOLD] = cat(bell(0.08, 1319, 0.12), bell(0.15, 1976, 0.12))
        w[LOCK] = mix(bell(0.1, 3136, 0.1), tone(0.06, 140, 60, 0, 0.4, 8))
        w[HARD] = clip(mix(mix(tone(0.25, 110, 35, 0, 0.9, 6), tone(0.012, 0, 0, 5, 0.4, 2)), bell(0.35, 2349, 0.18)), 1.5)
        for k in range(1, 5):
            x = mix(tone(0.35 + 0.1 * k, 0, 0, 5, 0.45, 5), crackle(0.3 + 0.1 * k, 25 + 15 * k, 0.35, k))
            for i in range(k + 2):
                x = mix_at(x, bell(0.4, 1568 * math.pow(2, i * 4 / 12.0), 0.12), int(R * 0.03 * i))
            w[C1 + k - 1] = clip(mix(x, tone(0.4 + 0.1 * k, 85, 28, 0, 0.9, 4.5)), 1.6)
        kb = mix(mix(tone(1.2, 0, 0, 5, 0.6, 2.5), crackle(1.0, 140, 0.4, 9)), tone(1.6, 70, 16, 0, 1.0, 2.0))
        r = JavaRandom(4)
        for i in range(8):
            kb = mix_at(kb, bell(0.6, 1500 + r.next_int(3000), 0.1), int(R * 0.06 * i))
        w[KABOOM] = clip(kb, 2.2)
    elif skin == 3:                                     # CANDY: pops, boings, slide whistles
        w[MOVE] = tone(0.025, 600, 950, 0, 0.15, 3)
        w[ROT] = mix(tone(0.09, 280, 720, 0, 0.22, 3), tone(0.09, 560, 1440, 0, 0.06, 4))
        w[HOLD] = tone(0.14, 500, 1500, 0, 0.2, 1.5)
        w[LOCK] = tone(0.035, 380, 1300, 0, 0.3, 4)
        w[HARD] = clip(mix_at(tone(0.2, 220, 55, 0, 0.85, 5), tone(0.05, 500, 1600, 0, 0.25, 3), int(R * 0.05)), 1.4)
        for k in range(1, 5):
            x = tone(0.4, 100, 40, 0, 0.7, 5)
            for i in range(k + 3):
                x = mix_at(x, tone(0.05, 400 + 150 * i, 1200 + 300 * i, 0, 0.25, 3), int(R * 0.045 * i))
            w[C1 + k - 1] = clip(x, 1.4)
        kb = mix(tone(0.7, 90, 300, 0, 0.5, 2), tone(1.4, 70, 18, 0, 1.0, 2.2))
        r = JavaRandom(5)
        for i in range(20):
            f = 400 + r.next_int(900)
            kb = mix_at(kb, tone(0.05, f, f * 3, 0, 0.22, 3), int(R * (0.05 + r.next_double() * 0.9)))
        w[KABOOM] = clip(kb, 2.0)
    elif skin == 4:                                     # LAVA: hiss, rock slams, sizzle, crackle
        w[MOVE] = tone(0.03, 0, 0, 4, 0.12, 6)
        w[ROT] = mix(tone(0.07, 0, 0, 4, 0.25, 4), tone(0.05, 0, 0, 5, 0.06, 8))
        w[HOLD] = mix(tone(0.2, 0, 0, 4, 0.3, 1), tone(0.2, 0, 0, 5, 0.08, 3))
        w[LOCK] = mix(tone(0.25, 0, 0, 5, 0.16, 5), tone(0.08, 130, 50, 0, 0.5, 7))
        w[HARD] = clip(mix(mix(tone(0.4, 85, 22, 0, 1.0, 4), tone(0.15, 0, 0, 4, 1.0, 6)),
                           mix_at(tone(0.001, 0, 0, 0, 0, 1), tone(0.5, 0, 0, 5, 0.2, 4), int(R * 0.05))), 1.7)
        for k in range(1, 5):
            w[C1 + k - 1] = clip(mix(mix(tone(0.6 + 0.15 * k, 70, 20, 0, 1.0, 3.5), tone(0.6 + 0.2 * k, 0, 0, 5, 0.25, 3)),
                                     crackle(0.6 + 0.2 * k, 40 + 20 * k, 0.3, k)), 1.7)
        w[KABOOM] = clip(mix(mix(tone(2.2, 60, 14, 0, 1.0, 1.6), tone(2.4, 0, 0, 4, 1.0, 1.4)),
                             mix(crackle(2.0, 250, 0.4, 7), tone(1.5, 0, 0, 5, 0.25, 2))), 2.4)
    elif skin == 5:                                     # COSMOS: zaps, warps, echoes
        w[MOVE] = echo(tone(0.02, 1400, 1400, 0, 0.12, 4), 0.08, 0.4, 3)
        w[ROT] = tone(0.06, 2200, 400, 1, 0.1, 3)
        w[HOLD] = echo(tone(0.18, 200, 2000, 0, 0.18, 1), 0.09, 0.35, 3)
        w[LOCK] = echo(tone(0.1, 180, 60, 0, 0.5, 6), 0.11, 0.35, 3)
        w[HARD] = echo(clip(mix(tone(0.08, 1800, 200, 1, 0.14, 3), tone(0.3, 120, 30, 0, 0.9, 5)), 1.4), 0.13, 0.4, 4)
        for k in range(1, 5):
            w[C1 + k - 1] = clip(echo(mix(tone(0.3 + 0.1 * k, 300, 2400 + 300 * k, 2, 0.18, 2),
                                          tone(0.5, 80, 25, 0, 0.8, 4)), 0.15, 0.4, 4), 1.5)
        swell = reverse(tone(0.6, 0, 0, 5, 0.5, 4))
        w[KABOOM] = clip(echo(cat(swell, mix(tone(1.6, 70, 15, 0, 1.0, 2), tone(1.2, 0, 0, 4, 0.8, 2))), 0.2, 0.35, 4), 2.0)
    elif skin == 6:                                     # CODE: digital ticks, chirps, glitches
        w[MOVE] = tone(0.008, 3000, 3000, 1, 0.08, 0)
        w[ROT] = cat(cat(tone(0.012, 1200, 1200, 1, 0.1, 0), tone(0.012, 2400, 2400, 1, 0.1, 0)),
                     tone(0.012, 1800, 1800, 1, 0.1, 0))
        w[HOLD] = glitch(0.07, 0.12, 3)
        w[LOCK] = crush(tone(0.03, 800, 800, 1, 0.15, 4), 5, 3)
        w[HARD] = clip(crush(mix(glitch(0.06, 0.15, 5), tone(0.18, 110, 35, 1, 0.5, 5)), 4, 6), 1.5)
        for k in range(1, 5):
            w[C1 + k - 1] = clip(mix(glitch(0.12 + 0.08 * k, 0.12, 10 + k), crush(tone(0.25 + 0.05 * k, 90, 30, 0, 0.8, 5), 4, 4)), 1.5)
        chunk, st = glitch(0.04, 0.3, 17), array("h")
        for _ in range(8):
            st = cat(st, chunk)
        w[KABOOM] = clip(crush(mix(mix(st, tone(1.5, 70, 15, 0, 1.0, 2)), tone(1.2, 0, 0, 3, 0.4, 2.5)), 3, 8), 2.0)
    else:                                               # NEON
        w[MOVE] = tone(0.02, 900, 800, 1, 0.06, 5)
        w[ROT] = mix(tone(0.03, 1400, 1700, 2, 0.16, 4), tone(0.04, 220, 160, 0, 0.25, 6))
        w[HOLD] = cat(tone(0.04, 440, 660, 2, 0.2, 2), tone(0.07, 880, 990, 2, 0.18, 3))
        w[LOCK] = clip(mix_at(tone(0.008, 0, 0, 3, 0.25, 3), mix(tone(0.13, 150, 50, 0, 0.7, 7), tone(0.05, 0, 0, 4, 0.7, 9)),
                              int(R * 0.012)), 1.3)
        ka = mix(tone(0.012, 0, 0, 3, 0.5, 3), tone(0.01, 2600, 1800, 1, 0.12, 2))
        chunk = mix(mix(tone(0.30, 125, 34, 0, 1.0, 6), tone(0.10, 0, 0, 4, 1.0, 9)), tone(0.11, 80, 45, 1, 0.2, 7))
        w[HARD] = clip(mix_at(ka, chunk, int(R * 0.028)), 1.6)
        for k in range(1, 5):
            boom = mix(tone(0.45 + 0.12 * k, 85, 28, 0, 1.0, 4.2), tone(0.22 + 0.06 * k, 0, 0, 4, 0.9, 6))
            x = mix(boom, arp(up[:k + 1], 0.04, 2, 0.2))
            if k == 4:
                x = mix(x, tone(0.9, 52, 24, 0, 0.9, 3))
                x = mix(x, tone(0.8, 0, 0, 3, 0.16, 4))
                x = mix_at(x, tone(0.25, 2093, 2093, 0, 0.14, 5), int(R * 0.2))
            w[C1 + k - 1] = clip(x, 1.7)
        kb = mix(tone(0.035, 0, 0, 3, 1.0, 4), tone(1.7, 75, 16, 0, 1.0, 2.0))
        kb = mix(kb, tone(1.9, 0, 0, 4, 1.0, 1.8))
        kb = mix(kb, tone(0.45, 58, 28, 1, 0.45, 4))
        kb = mix_at(kb, tone(0.9, 0, 0, 3, 0.18, 3.5), int(R * 0.08))
        kb = mix_at(kb, tone(0.6, 40, 20, 0, 0.8, 3), int(R * 0.12))
        w[KABOOM] = clip(kb, 2.4)
    return w


def shared() -> dict:
    """The constructor's menu/meta sounds: spin, pc, over, win, tick, go, level."""
    n = [523, 659, 784, 1047, 1319, 1568, 2093]
    out = {}
    out["spin"] = clip(mix(tone(0.16, 200, 900, 1, 0.08, 1), mix_at(tone(0.001, 0, 0, 0, 0, 1),
                                                                    tone(0.3, 95, 35, 0, 0.8, 5), int(RATE * 0.12))), 1.4)
    p = array("h")
    for i in range(7):
        p = cat(p, tone(0.05, n[i], n[i], 2, 0.24, 1.5))
    p = cat(p, mix(tone(0.45, 1047, 1047, 0, 0.18, 3), tone(0.45, 1568, 1568, 0, 0.14, 3)))
    p = mix(p, tone(1.0, 60, 25, 0, 0.9, 2.5))
    out["pc"] = clip(p, 1.5)
    out["over"] = clip(mix(tone(0.8, 440, 90, 2, 0.3, 2.5), tone(1.1, 70, 22, 0, 0.9, 2.2)), 1.4)
    wn = array("h")
    for i in range(4):
        wn = cat(wn, tone(0.08, n[i] / 2.0, n[i] / 2.0, 2, 0.28, 1.5))
    wn = cat(wn, mix(tone(0.5, 523, 523, 0, 0.2, 2.5), tone(0.5, 784, 784, 0, 0.16, 2.5)))
    out["win"] = mix(wn, tone(0.9, 65, 30, 0, 0.7, 2.5))
    out["tick"] = tone(0.08, 660, 660, 0, 0.3, 5)
    out["go"] = mix(tone(0.18, 990, 990, 0, 0.35, 3), tone(0.3, 90, 40, 0, 0.7, 5))
    out["level"] = cat(tone(0.06, 784, 784, 1, 0.08, 2), tone(0.1, 1175, 1175, 1, 0.08, 2))
    return out
