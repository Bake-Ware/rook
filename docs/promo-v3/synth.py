"""Original 128 BPM track for the Rook promo, synthesised from scratch (no samples).

Beat map (one beat = 60/128 s):
  0-12  intro: drone, heartbeat
  12-28 build A: bass 8ths, hats, kick
  28-46 build B: riser, accelerating snare roll, filter opens, stutter at the end
  46-48 silence
  48-80 drop: four on the floor, supersaw chords, offbeat bass, arp
  80-96 outro: crash, pad, pulse, fade
"""
import numpy as np
from scipy.signal import butter, sosfilt, fftconvolve
import wave, sys

SR = 44100
BPM = 128
B = 60 / BPM
rng = np.random.default_rng(7)

def s(beat):
    return int(round(beat * B * SR))

def t_of(n):
    return np.arange(n) / SR

def mtof(m):
    return 440.0 * 2 ** ((m - 69) / 12)

def add(buf, beat, sig, gain=1.0):
    i = s(beat)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(sig))
    buf[i:j] += sig[: j - i] * gain

def lp(x, f, order=2):
    return sosfilt(butter(order, min(f, SR / 2 * 0.95), "low", fs=SR, output="sos"), x)

def hp(x, f, order=2):
    return sosfilt(butter(order, f, "high", fs=SR, output="sos"), x)

def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, min(hi, SR / 2 * 0.95)], "band", fs=SR, output="sos"), x)

def sweep_lp(x, f0, f1, block=512, curve=2.0):
    """Low-pass whose cutoff moves from f0 to f1 (exponential), state carried across blocks."""
    out = np.zeros_like(x)
    zi = None
    nb = (len(x) + block - 1) // block
    for k in range(nb):
        p = (k / max(1, nb - 1)) ** curve
        f = f0 * (f1 / f0) ** p
        sos = butter(2, min(f, SR / 2 * 0.95), "low", fs=SR, output="sos")
        if zi is None:
            zi = np.zeros((sos.shape[0], 2))
        seg = x[k * block:(k + 1) * block]
        y, zi = sosfilt(sos, seg, zi=zi)
        out[k * block:k * block + len(seg)] = y
    return out

def saw(f, n, phase=0.0):
    ph = (phase + np.cumsum(np.full(n, f / SR))) % 1.0 if np.isscalar(f) else (phase + np.cumsum(f / SR)) % 1.0
    return 2 * ph - 1

def env_adsr(n, a, d, sus, r, hold=None):
    t = t_of(n)
    hold = hold if hold is not None else n / SR - r
    e = np.where(t < a, t / max(a, 1e-4), sus + (1 - sus) * np.exp(-(t - a) / max(d, 1e-4)))
    rel = np.clip((t - hold) / max(r, 1e-4), 0, 1)
    return e * (1 - rel)

# ---------------------------------------------------------------- instruments
def kick(level=1.0, dur=0.42, f_hi=160, f_lo=44):
    n = int(dur * SR); t = t_of(n)
    f = f_lo + (f_hi - f_lo) * np.exp(-t / 0.035)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.16)
    click = hp(rng.standard_normal(n), 3000) * np.exp(-t / 0.004) * 0.25
    return np.tanh((body + click) * 1.8) * level

def heartbeat():
    k = kick(0.9, 0.5, 90, 38)
    return lp(k, 400)

def clap(level=1.0):
    n = int(0.35 * SR); t = t_of(n)
    nz = bp(rng.standard_normal(n), 900, 6000)
    e = np.zeros(n)
    for off in (0, 0.011, 0.022):
        e += (t >= off) * np.exp(-np.clip(t - off, 0, None) / 0.012)
    e += (t >= 0.03) * np.exp(-np.clip(t - 0.03, 0, None) / 0.11)
    tone = np.sin(2 * np.pi * 190 * t) * np.exp(-t / 0.05) * 0.4
    return (nz * e * 0.6 + tone) * level

def snare(level=1.0):
    n = int(0.22 * SR); t = t_of(n)
    nz = bp(rng.standard_normal(n), 1500, 9000) * np.exp(-t / 0.06)
    tone = np.sin(2 * np.pi * 220 * t) * np.exp(-t / 0.04)
    return (nz * 0.7 + tone * 0.5) * level

def hat(level=1.0, open_=False):
    n = int((0.25 if open_ else 0.06) * SR); t = t_of(n)
    return hp(rng.standard_normal(n), 7500, 4) * np.exp(-t / (0.08 if open_ else 0.014)) * level

def crash(level=1.0, dur=2.6):
    n = int(dur * SR); t = t_of(n)
    nz = hp(rng.standard_normal(n), 4500, 2) + 0.4 * bp(rng.standard_normal(n), 2500, 6000)
    return nz * np.exp(-t / 0.7) * level

def impact(level=1.0):
    n = int(2.2 * SR); t = t_of(n)
    f = 30 + 60 * np.exp(-t / 0.12)
    sub = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.7)
    nz = lp(rng.standard_normal(n), 900) * np.exp(-t / 0.25) * 0.5
    return np.tanh((sub + nz) * 1.5) * level

def subdrop(level=1.0, dur=1.8):
    n = int(dur * SR); t = t_of(n)
    f = 28 + 40 * np.exp(-t / 0.5)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.9) * level

def whoosh(beats=0.75, level=1.0):
    n = s(beats); t = t_of(n)
    x = rng.standard_normal(n)
    x = sweep_lp(x, 300, 9000, curve=1.5)
    e = (t / t[-1]) ** 3
    return x * e * level

def supersaw(midis, dur, voices=7, detune=0.18, cutoff=5000, attack=0.005, release=0.08):
    n = int(dur * SR)
    out = np.zeros(n)
    for m in midis:
        f0 = mtof(m)
        for v in range(voices):
            d = (v - (voices - 1) / 2) / ((voices - 1) / 2) * detune
            out += saw(f0 * 2 ** (d / 12), n, rng.random())
    out /= len(midis) * voices ** 0.5
    out = lp(out, cutoff)
    return out * env_adsr(n, attack, 0.3, 0.85, release)

def pluck(m, dur, cutoff=3500):
    n = int(dur * SR); t = t_of(n)
    f = mtof(m)
    sq = np.sign(np.sin(2 * np.pi * f * t)) * 0.6 + saw(f, n) * 0.4
    return lp(sq, cutoff) * np.exp(-t / 0.09)

def bassnote(m, dur, cutoff=900, sub=1.0):
    n = int(dur * SR); t = t_of(n)
    f = mtof(m)
    x = saw(f, n) * 0.6 + np.sin(2 * np.pi * f * t) * 0.8 * sub + np.sin(2 * np.pi * f / 2 * t) * 0.3 * sub
    x = lp(x, cutoff)
    return x * env_adsr(n, 0.003, 0.12, 0.7, 0.03)

