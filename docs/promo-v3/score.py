"""Promo v3 score: an original arrangement of "Korobeiniki" (Russian folk song, 1861,
public domain; the melody the Tetris theme is built on). 128 BPM, 142 beats, same beat
map as the picture.

  0-16    the box: 8-bit chiptune (square lead, triangle bass, noise drums, fast arps)
  16      shatter: bit-crushed glitch + glass
  16-32   the door: lighter, major key: music box over warm pad, strings swell, timpani roll
  32-40   title: full orchestra (brass, strings, choir, timpani)
  40-72   first person: string ostinato, horns carry the theme, build 64-72
  72-88   one layer: the theme, tutti
  88-100  limit reached: power-down, lonely music box in minor, handoff chime
  100-104 rebuild
  104-128 tools, agents talking: second phrase, tutti
  128-142 outro: A major (Picardy) ending
"""
import sys, wave
import numpy as np
from synth import *
from scipy.signal import fftconvolve, butter, sosfilt

TOTAL_BEATS = 142
N = int(TOTAL_BEATS * B * SR) + SR

# ---------------------------------------------------------------- the tune
A4, B4, C5, D5, E5, F5, G5, A5 = 69, 71, 72, 74, 76, 77, 79, 81
R = None
P1 = [(E5, 1), (B4, .5), (C5, .5), (D5, 1), (C5, .5), (B4, .5),
      (A4, 1), (A4, .5), (C5, .5), (E5, 1), (D5, .5), (C5, .5),
      (B4, 1.5), (C5, .5), (D5, 1), (E5, 1),
      (C5, 1), (A4, 1), (A4, 1), (R, 1)]
P2 = [(R, .5), (D5, 1), (F5, .5), (A5, 1), (G5, .5), (F5, .5),
      (E5, 1.5), (C5, .5), (E5, 1), (D5, .5), (C5, .5),
      (B4, 1), (B4, .5), (C5, .5), (D5, 1), (E5, 1),
      (C5, 1), (A4, 1), (A4, 1), (R, 1)]
CH1 = ['Am', 'Am', 'E', 'Am']; CH2 = ['Dm', 'Am', 'E', 'Am']
CHORDS = {'Am': [57, 60, 64], 'E': [56, 59, 64], 'Dm': [57, 62, 65], 'C': [55, 60, 64], 'G': [55, 59, 62], 'F': [57, 60, 65], 'A': [57, 61, 64]}
ROOT = {'Am': 45, 'E': 40, 'Dm': 38, 'C': 36, 'G': 43, 'F': 41, 'A': 45}
# minor -> relative-major degree map (A minor degree n -> C major degree n), for the "lighter" door section
MAJ = {69: 72, 71: 74, 72: 76, 74: 77, 76: 79, 77: 81, 79: 83, 81: 84, 68: 71}
def major(mel): return [((MAJ[m] if m in MAJ else m + 3) if m else m, d) for m, d in mel]
CHMAJ = {'Am': 'C', 'E': 'G', 'Dm': 'F'}

def seq(mel, start, until=None):
    """yield (beat, midi, dur) for a melody starting at a beat, optionally cut at a beat"""
    b = start
    for m, d in mel:
        if until is not None and b >= until: break
        if m is not None: yield b, m, d
        b += d

# ---------------------------------------------------------------- instruments
def square(f, n, duty=.25):
    ph = (np.cumsum(np.full(n, f / SR)) if np.isscalar(f) else np.cumsum(f / SR)) % 1.0
    return np.where(ph < duty, 1.0, -1.0)
def triangle(f, n):
    ph = np.cumsum(np.full(n, f / SR)) % 1.0
    return 4 * np.abs(ph - .5) - 1
def crush(x, bits=5):
    q = 2 ** (bits - 1); return np.round(x * q) / q
def chip_lead(m, dur, duty=.25):
    n = int(dur * SR); t = t_of(n)
    vib = mtof(m) * (1 + .006 * np.sin(2 * np.pi * 6 * t) * np.clip((t - .12) / .1, 0, 1))
    x = square(vib, n, duty) * env_adsr(n, .002, .08, .7, .02)
    return crush(x * .9, 5)
def chip_bass(m, dur):
    n = int(dur * SR); return crush(triangle(mtof(m), n) * env_adsr(n, .002, .05, .9, .01), 4)
def chip_noise(dur, decay, hp_f=0, lvl=1.0, period=1):
    n = int(dur * SR); t = t_of(n)
    x = np.repeat(rng.choice([-1.0, 1.0], n // period + 1), period)[:n]
    if hp_f: x = hp(x, hp_f)
    return x * np.exp(-t / decay) * lvl
def chip_kick(lvl=1.0):
    n = int(.12 * SR); t = t_of(n); f = 50 + 180 * np.exp(-t / .02)
    return crush(triangle(1, 1)[0] * 0 + np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR)) * np.exp(-t / .05), 4) * lvl
def music_box(m, dur=1.6, lvl=1.0):
    n = int(dur * SR); t = t_of(n); f = mtof(m)
    x = np.sin(2 * np.pi * f * t) + .35 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t / .25) + .18 * np.sin(2 * np.pi * 4.16 * f * t) * np.exp(-t / .08)
    return x * np.exp(-t / .55) * lvl
def piano(m, dur=2.0, lvl=1.0):
    n = int(dur * SR); t = t_of(n); f = mtof(m)
    x = sum(a * np.sin(2 * np.pi * f * k * (1 + .0004 * k * k) * t) * np.exp(-t * (1.2 + k * .8)) for k, a in zip(range(1, 7), [1, .55, .3, .18, .1, .06]))
    hammer = lp(rng.standard_normal(n), 3000) * np.exp(-t / .006) * .08
    return (x + hammer) * lvl
def strings(midis, dur, attack=.25, release=.35, cutoff=3200, voices=5, detune=.12, trem=0):
    n = int(dur * SR); t = t_of(n); out = np.zeros(n)
    for m in midis:
        for v in range(voices):
            d = (v - (voices - 1) / 2) / ((voices - 1) / 2) * detune
            f = mtof(m) * 2 ** (d / 12) * (1 + .0035 * np.sin(2 * np.pi * (5.3 + v * .3) * t + v))
            out += saw(f, n, rng.random())
    out = lp(out / (len(midis) * voices ** .5), cutoff)
    if trem: out *= .6 + .4 * np.sin(2 * np.pi * trem * t) ** 2
    return out * env_adsr(n, attack, .5, .9, release)
def brass(midis, dur, cutoff=2600, attack=.035):
    n = int(dur * SR); t = t_of(n); out = np.zeros(n)
    for m in midis:
        for v in range(3):
            f = mtof(m) * 2 ** ((v - 1) * .06 / 12) * (1 - .01 * np.exp(-t / .04))
            out += saw(f, n, rng.random())
    out /= len(midis) * 3 ** .5
    env_c = 700 + (cutoff - 700) * (1 - np.exp(-t / .06))
    blocks = 256; y = np.zeros(n); zi = None
    for k in range(0, n, blocks):
        sos = butter(2, min(env_c[k], SR / 2 * .9), 'low', fs=SR, output='sos')
        if zi is None: zi = np.zeros((sos.shape[0], 2))
        seg, zi = sosfilt(sos, out[k:k + blocks], zi=zi); y[k:k + blocks] = seg
    return np.tanh(y * 1.6) * env_adsr(n, attack, .3, .85, .12)
def choir(midis, dur, attack=.5, release=.8):
    n = int(dur * SR); src = np.zeros(n); t = t_of(n)
    for m in midis:
        for v in range(3): src += saw(mtof(m) * 2 ** ((v - 1) * .08 / 12) * (1 + .004 * np.sin(2 * np.pi * 4.8 * t + v)), n, rng.random())
    x = bp(src, 650, 950) * 1.0 + bp(src, 1050, 1350) * .6 + bp(src, 2500, 3000) * .15
    return x / len(midis) * env_adsr(n, attack, .5, .9, release)
def timpani(m=45, lvl=1.0, dur=1.6):
    n = int(dur * SR); t = t_of(n); f = mtof(m) * (1 + .08 * np.exp(-t / .03))
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / .55) + .5 * np.sin(2 * np.pi * np.cumsum(f * 1.5) / SR) * np.exp(-t / .3)
    x += lp(rng.standard_normal(n), 1200) * np.exp(-t / .03) * .4
    return x * lvl
def taiko(lvl=1.0):
    n = int(.8 * SR); t = t_of(n); f = 45 + 70 * np.exp(-t / .04)
    return np.tanh((np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / .25) + lp(rng.standard_normal(n), 900) * np.exp(-t / .05) * .6) * 1.4) * lvl

def roll(b0, b1):
    span = b1 - b0
    q = [b0 + span * f for f in (0, .4, .7, .87)]
    pos = list(np.arange(q[0], q[1], 1)) + list(np.arange(q[1], q[2], .5)) + list(np.arange(q[2], q[3], .25)) + list(np.arange(q[3], b1, .125))
    for p in pos: add(drums, p, snare(.12 + .5 * ((p - b0) / span) ** 1.5))

# ---------------------------------------------------------------- buses
chip = np.zeros(N); drums = np.zeros(N); orch = np.zeros(N); bassb = np.zeros(N); keys = np.zeros(N); fx = np.zeros(N); pads = np.zeros(N)
def at(buf, b, x, g=1.0): add(buf, b, x, g)

# ---------------------------------------------------------------- 0-16 the box: chiptune
for b, m, d in seq(P1, 0, until=12):
    at(chip, b, chip_lead(m, d * B * .92, .25 if b < 8 else .125), .32)
for bar, name in enumerate(CH1[:3]):
    for k in range(8):
        r = ROOT[name] - 12 + (12 if k % 2 else 0)
        at(bassb, bar * 4 + k * .5, chip_bass(r, B * .45), .5)
    tones = CHORDS[name]
    for k in range(16):  # classic fast arp on a second pulse channel
        at(chip, bar * 4 + k * .25, chip_lead(tones[k % 3] + 12, B * .22, .5), .09)
for b in range(0, 12):
    at(drums, b, chip_kick(.5) if b % 2 == 0 else chip_noise(.14, .05, 1500, .3, 2))
    at(drums, b + .5, chip_noise(.05, .012, 6000, .16))
# 12-16: the walls close in - a rising chip run that speeds up, noise roll, the bass pumps
run = [57, 59, 60, 62, 64, 65, 67, 68, 69, 71, 72, 74, 76, 77, 79, 80, 81, 83, 84, 86, 88]
pos, k = 12.0, 0
while pos < 16 and k < 200:
    step = .5 if pos < 13 else .25 if pos < 14.5 else .125
    at(chip, pos, chip_lead(run[min(k, len(run) - 1)] - (12 if pos < 14 else 0) + (k // len(run)) * 0, B * step * .9, .125), .26)
    pos += step; k += 1
for b8 in np.arange(12, 16, .25): at(bassb, b8, chip_bass(33 + (12 if int(b8 * 4) % 2 else 0), B * .22), .5)
pos = 12
while pos < 16:
    at(drums, pos, chip_noise(.08, .03, 2500, .18 + .3 * (pos - 12) / 4, 2)); pos += .5 if pos < 14 else .25 if pos < 15 else .125

# ---------------------------------------------------------------- 16 shatter
at(fx, 16, impact(1.0)); at(drums, 16, crash(.6, 3.0))
gl = chip_noise(1.2, .35, 0, 1.0, 6); at(fx, 16, crush(lp(gl, 6000), 3), .35)
n = s(1.0); t = t_of(n); at(fx, 16, crush(square(880 * 2 ** (-5 * t / t[-1]), n, .5) * (1 - t / t[-1]), 3), .18)
for k in range(28):
    nn = int(.5 * SR); tt = t_of(nn); f = 2500 + rng.random() * 5000
    at(fx, 16 + rng.random() * 1.6, np.sin(2 * np.pi * f * tt) * np.exp(-tt / (.04 + .12 * rng.random())), .05 + .08 * rng.random())

# ---------------------------------------------------------------- 16-32 the door: lighter, major
for bar in range(4, 8):
    name = CHMAJ.get(CH1[(bar - 4) % 4], 'C')
    at(pads, bar * 4, strings([m + 12 for m in CHORDS[name]], 4 * B, attack=1.0 if bar == 4 else .4, release=.6, cutoff=1800), .32 + .06 * (bar - 4))
for b, m, d in seq(major(P1), 18, until=30):
    at(keys, b, music_box(m + 12, 1.8), .26)
    at(keys, b, music_box(m, 1.2), .12)
for b in np.arange(26, 31.5, .125):  # timpani roll, crescendo
    at(drums, b, timpani(40, .08 + .35 * ((b - 26) / 5.5) ** 2, .5))
swell = strings([55, 60, 64, 67, 72], s(5.5) / SR, attack=4.5, release=.3, cutoff=4200, trem=12)
at(orch, 26, swell, .5)
n = s(5.5); t = t_of(n); u = t / t[-1]
at(fx, 26, sweep_lp(rng.standard_normal(n), 400, 11000, curve=2.0) * u ** 2.5, .3)
at(fx, 30.75, whoosh(.75, .35))
for buf in (chip, drums, orch, bassb, keys, fx, pads): buf[s(31.5):s(32)] = 0

# ---------------------------------------------------------------- orchestral helpers
def tutti_theme(mel, start, until, lvl=1.0, octave=0):
    for b, m, d in seq(mel, start, until):
        dur = d * B * .98
        at(orch, b, brass([m - 12 + octave, m + octave], dur), .34 * lvl)
        at(orch, b, strings([m + octave, m + 12 + octave], dur, attack=.03, release=.12, cutoff=5200), .3 * lvl)
def chord_bed(names, start, lvl=1.0, choir_on=True):
    for k, name in enumerate(names):
        b = start + k * 4; tones = CHORDS[name]
        at(pads, b, strings([m - 12 for m in tones] + tones, 4 * B, attack=.08, release=.3, cutoff=2600), .3 * lvl)
        if choir_on: at(pads, b, choir(tones + [tones[0] + 12], 4 * B, attack=.25, release=.4), .5 * lvl)
        at(bassb, b, strings([ROOT[name] - 12, ROOT[name]], 4 * B, attack=.02, release=.2, cutoff=900), .45 * lvl)
def ostinato(names, start, lvl=1.0, sub=.5):
    for k, name in enumerate(names):
        tones = CHORDS[name]; pat = [0, 1, 2, 1] if sub == .5 else [0, 1, 2, 1, 2, 0, 1, 2]
        steps = int(4 / sub)
        for j in range(steps):
            at(orch, start + k * 4 + j * sub, strings([tones[pat[j % len(pat)]] + 12], sub * B * .55, attack=.005, release=.05, cutoff=4500, voices=3), .2 * lvl)
def hybrid_drums(b0, b1, lvl=1.0, taiko_on=True):
    for b in range(b0, b1):
        at(drums, b, kick(.7 * lvl)); side.append(b)
        if b % 2 == 1: at(drums, b, clap(.45 * lvl))
        at(drums, b + .5, hat(.25 * lvl, open_=True))
        if taiko_on and b % 4 in (0, 2): at(drums, b, taiko(.55 * lvl))
side = []

# ---------------------------------------------------------------- 32-40 title
at(fx, 32, impact(1.0)); at(drums, 32, crash(.6)); at(drums, 32, timpani(45, 1.0))
tutti_theme(P1, 32, 40, 1.1); chord_bed(CH1[:2], 32, 1.0); hybrid_drums(32, 40, .9)
at(drums, 36, timpani(40, .8))

# ---------------------------------------------------------------- 40-72 first person
chord_bed(CH1 + CH1 + CH2[:2], 40, .55, choir_on=False)
ostinato(CH1 + CH1 + CH2[:2], 40, .9)
for b, m, d in seq(P1, 40, 56): at(orch, b, brass([m - 12], d * B * .95, cutoff=1800), .3)   # horns
for b, m, d in seq(P2, 56, 64): at(orch, b, strings([m, m + 12], d * B * .95, attack=.05, release=.15, cutoff=5000), .28)
for b in range(40, 64):
    at(drums, b, kick(.38)); side.append(b)
    if b % 2 == 1: pass
    if b % 4 == 0: at(drums, b, taiko(.4))
for b in range(40, 64, 6): at(fx, b - .5, whoosh(.5, .12))
# build 64-72: 16th ostinato, rising strings, timpani + snare roll, riser
ostinato(['E', 'E'], 64, 1.1, sub=.25)
at(orch, 64, strings([64, 68, 71, 76], 8 * B, attack=6.5 * B, release=.2, cutoff=5000, trem=16), .75)
for b in np.arange(64, 72, .125): at(drums, b, timpani(40, .05 + .3 * ((b - 64) / 8) ** 2, .4))
roll(66, 72)
n = s(8); t = t_of(n); u = t / t[-1]
at(fx, 64, sweep_lp(rng.standard_normal(n), 400, 12000, curve=2.0) * u ** 2.5, .3)
at(fx, 71.25, whoosh(.75, .25))

# ---------------------------------------------------------------- 72-88 one layer: the theme, tutti
at(fx, 72, impact(1.0)); at(fx, 72, subdrop(.8)); at(drums, 72, crash(.6)); at(drums, 72, timpani(45, 1.0)); at(drums, 80, crash(.45))
tutti_theme(P1, 72, 88, 1.15); chord_bed(CH1, 72, 1.0); ostinato(CH1, 72, .8); hybrid_drums(72, 88)
for k in range(64): at(chip, 72 + k * .25, chip_lead(CHORDS[CH1[k // 16]][k % 3] + 24, B * .2, .5), .05)  # 8-bit callback

# ---------------------------------------------------------------- 88-100 limit reached
nb = s(4); brk = np.zeros(nb)
for b in range(88, 92):
    k_ = kick(.7); i = s(b - 88); brk[i:i + len(k_)] += k_[:max(0, nb - i)][:len(k_)]
at(drums, 88, sweep_lp(brk, 8000, 300, curve=.8))
at(pads, 88, sweep_lp(strings(CHORDS['Am'] + [69], 3.5 * B, attack=.05, release=.2, cutoff=4000), 4000, 400, curve=.8), .4)
n = s(2); t = t_of(n); u = t / t[-1]
at(fx, 91.5, lp(saw(440 * 2 ** (-4 * u), n), 3000) * (1 - u) ** 1.5, .5)
at(pads, 92, strings([45, 52, 57, 60], 8 * B, attack=1.2, release=1.2, cutoff=1200), .38)
for b, m, d in seq(P1, 92, 100): at(keys, 92 + (b - 92) * 1.0, piano(m, d * B * 1.8 + .6), .3)
for k, m in enumerate([76, 81, 88]): at(keys, 98 + k * .25, music_box(m, 1.6), .25)   # handoff chime

# ---------------------------------------------------------------- 100-104 rebuild
for b in np.arange(100, 104, .125): at(drums, b, timpani(40, .06 + .32 * ((b - 100) / 4) ** 2, .4))
at(orch, 100, strings([64, 68, 71, 76], 4 * B, attack=3.5 * B, release=.2, cutoff=5000, trem=16), .42)
roll(100, 104)
n = s(4); t = t_of(n); u = t / t[-1]
at(fx, 100, sweep_lp(rng.standard_normal(n), 400, 12000, curve=2.0) * u ** 2.5, .28)
at(fx, 103.25, whoosh(.75, .25))

# ---------------------------------------------------------------- 104-128 second phrase, tutti
at(fx, 104, impact(1.0)); at(fx, 104, subdrop(.7)); at(drums, 104, crash(.6)); at(drums, 104, timpani(45, 1.0)); at(drums, 120, crash(.45))
tutti_theme(P2, 104, 120, 1.15); chord_bed(CH2, 104, 1.0); ostinato(CH2, 104, .8)
cad = [(B4, 1), (B4, .5), (C5, .5), (D5, 1), (E5, 1), (C5, 1), (A4, 1), (A4, 2)]
tutti_theme(cad, 120, 128, 1.2, octave=0); chord_bed(['E', 'Am'], 120, 1.0)
hybrid_drums(104, 128)
for b in (106, 110, 114, 118, 122):
    for k, m in enumerate((88, 93)): at(keys, b + k * .25, music_box(m, 1.0), .12)
for b in np.arange(126, 128, .125): at(drums, b, timpani(45, .1 + .3 * ((b - 126) / 2), .4))

# ---------------------------------------------------------------- 128-142 outro: A major
at(drums, 128, crash(.7, 4.0)); at(fx, 128, impact(.9)); at(drums, 128, timpani(45, 1.0))
at(orch, 128, brass([45, 57, 61, 64, 69], 7 * B, cutoff=2400, attack=.05), .4)
at(pads, 128, strings([45, 52, 57, 61, 64, 69, 73, 76], 10 * B, attack=.05, release=2.5, cutoff=4000), .42)
at(pads, 128, choir([57, 61, 64, 69], 10 * B, attack=.3, release=2.5), .55)
at(bassb, 128, strings([33, 45], 9 * B, attack=.02, release=2, cutoff=700), .5)
for k, (m, d) in enumerate([(E5, 1), (B4, .5), (C5 + 1, .5), (D5, 1), (E5 + 0, 2)]):  # the opening notes, now in A major
    pass
for b, m in [(134, 88), (135, 83), (135.5, 85), (136, 86), (137, 88)]: at(keys, b, music_box(m, 2.2), .22)
at(drums, 136, timpani(45, .7))


# ---------------------------------------------------------------- body percussion (Barbatuques-style sounds, our own groove)
body = np.zeros(N)
def bp_chest(lvl=1.0):   # palm on chest: low, round thump
    n = int(.25 * SR); t = t_of(n); f = 75 + 60 * np.exp(-t / .015)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / .07) + lp(rng.standard_normal(n), 700) * np.exp(-t / .012) * .35
    return x * lvl
def bp_thigh(lvl=1.0):   # slap on the thigh: mid thud with skin noise
    n = int(.18 * SR); t = t_of(n)
    x = np.sin(2 * np.pi * 190 * t) * np.exp(-t / .03) * .6 + bp(rng.standard_normal(n), 900, 3500) * np.exp(-t / .018)
    return x * lvl
def bp_clap(lvl=1.0):    # several people clapping, slightly loose
    n = int(.3 * SR); out = np.zeros(n)
    for k in range(4):
        off = int(abs(rng.normal(0, .006)) * SR); m = n - off; t = t_of(m)
        out[off:] += bp(rng.standard_normal(m), 1000 + 300 * k, 5500) * (np.exp(-t / .011) + .25 * np.exp(-t / .06))
    return out * lvl * .5
def bp_snap(lvl=1.0):    # finger snap
    n = int(.12 * SR); t = t_of(n)
    return (bp(rng.standard_normal(n), 2200, 7000) * np.exp(-t / .006) + np.sin(2 * np.pi * 2300 * t) * np.exp(-t / .012) * .3) * lvl
def bp_tch(lvl=1.0):     # mouth "tch", like a shaker
    n = int(.08 * SR); t = t_of(n)
    return hp(rng.standard_normal(n), 4500) * np.exp(-t / .02) * (1 - np.exp(-t / .002)) * lvl
def bp_click(pitch=1000, lvl=1.0):   # tongue click
    n = int(.06 * SR); t = t_of(n)
    return np.sin(2 * np.pi * pitch * t) * np.exp(-t / .009) * lvl
def bp_dum(m=45, lvl=1.0):          # a low voice "dum"
    n = int(.35 * SR); t = t_of(n); src = saw(mtof(m), n) + .5 * square(mtof(m), n, .5)
    x = bp(src, 250, 600) * 1.0 + bp(src, 700, 1000) * .35
    return x * env_adsr(n, .01, .08, .5, .1) * lvl

# one bar = 16 sixteenth steps; a baião-flavoured pattern
GROOVE = {'chest': [0, 3, 8, 11], 'thigh': [6, 14], 'ghost': [4, 12], 'clap': [4, 12], 'snap': [2, 10], 'click': [5, 13, 15]}
def groove(b0, b1, lvl=1.0, full=True, dum=False, names=None):
    bar = 0
    for b in np.arange(b0, b1, 4):
        fill = full and (bar % 4 == 3)
        for st in GROOVE['chest']: at(body, b + st / 4, bp_chest(.9 * lvl))
        for st in GROOVE['thigh']: at(body, b + st / 4, bp_thigh(.6 * lvl))
        for st in GROOVE['ghost']: at(body, b + st / 4 + .5, bp_thigh(.22 * lvl))
        for st in GROOVE['clap']: at(body, b + st / 4, bp_clap(.75 * lvl))
        if full:
            for st in GROOVE['snap']: at(body, b + st / 4, bp_snap(.35 * lvl))
            for st in GROOVE['click']: at(body, b + st / 4, bp_click(1100 if st != 15 else 800, .25 * lvl))
            for st in range(16): at(body, b + st / 4, bp_tch((.22 if st % 4 == 2 else .1) * lvl))
        if fill:  # last bar of four: chest triplets into the next phrase
            for k in range(6): at(body, b + 2 + k / 3, bp_chest(.6 * lvl + .08 * k * lvl))
        if dum and names:
            name = names[bar % len(names)]; r = ROOT[name]
            for st in (0, 3, 8, 11): at(body, b + st / 4, bp_dum(r, .35 * lvl))
        bar += 1

# title: big unison hits on the theme's accents
for b in (32, 33.5, 34, 36, 37.5, 38): at(body, b, bp_chest(1.0)); at(body, b, bp_clap(.9))
# first person: the groove carries the tour, with a sung bass
groove(40, 64, .95, full=True, dum=True, names=CH1 + CH1 + CH2[:2])
# build: claps tighten from 8ths to 16ths to 32nds
for b in np.arange(64, 72, .5 if True else .25):
    sub = .5 if b < 67 else .25 if b < 70 else .125
    for k in np.arange(0, .5, sub): at(body, b + k, bp_clap(.35 + .5 * (b - 64) / 8))
for b in range(64, 72): at(body, b, bp_chest(.8))
# one layer: lighter groove under the tutti
groove(72, 88, .6, full=True)
# breakdown: snaps on 2 and 4 come back in as the new agent arrives, then claps build
for b in np.arange(96, 100, 1): at(body, b + 1, bp_snap(.45))
for b in np.arange(100, 104, .25): at(body, b, bp_clap(.25 + .55 * (b - 100) / 4))
# tools and agents talking: full groove with the second phrase
groove(104, 128, .75, full=True, dum=True, names=CH2 + ['E', 'Am'])
# outro: three last unison hits
for b in (128, 129.5, 130): at(body, b, bp_chest(1.0)); at(body, b, bp_clap(.9))

# ---------------------------------------------------------------- mix
sidechain = np.ones(N)
for b in side:
    i = s(b); n = min(N - i, int(.4 * SR)); tl = np.arange(n) / SR
    sidechain[i:i + n] = np.minimum(sidechain[i:i + n], 1 - .55 * np.exp(-tl / .12))
pads *= np.maximum(sidechain, .55); bassb *= sidechain

def reverb(x, secs=2.0, mix=.3):
    n = int(secs * SR); t = t_of(n)
    ir = lp(rng.standard_normal(n) * np.exp(-t / (secs / 5)), 7000); ir /= np.sqrt(np.sum(ir ** 2))
    return fftconvolve(x, ir)[:len(x)] * mix
hall = reverb(orch * .8 + pads * .7 + keys * 1.0 + fx * .2, 2.8, .42)
room = reverb(chip * .5 + drums * .12 + body * .35, .9, .25)
dly = np.zeros(N); d = s(.75); dly[d:] = keys[:-d] * .3 + chip[:-d] * .12
mid = chip * .8 + body * .85 + drums * .9 + orch * .85 + bassb * .9 + keys * .8 + fx * .9 + pads * .7 + hall + room
hz = int(.011 * SR); wide = orch * .4 + pads * .5 + hall * .8
L = mid + np.concatenate([np.zeros(hz), wide[:-hz]]) * .3 + dly * .7
R = mid + wide * .3 + np.concatenate([np.zeros(int(.006 * SR)), dly[:-int(.006 * SR)]]) * .9
L = hp(L, 25); R = hp(R, 25)
for ch in (L, R): ch[s(31.55):s(31.95)] *= .15
pts = [(0, -7), (11.9, -6), (12, -5), (15.9, -2.5), (16, -1.5), (31.5, -1), (32, 0), (40, -3), (63.9, -3), (64, -1), (71.9, 0), (72, 0), (88, 0), (92, -4), (99.9, -4), (100, -1), (103.9, 0), (104, 0), (128, 0), (136, -2), (142, -2)]
bx = np.arange(N) / SR / B
g = 10 ** (np.interp(bx, [p[0] for p in pts], [p[1] for p in pts]) / 20)
fade = np.clip((142 - bx) / 5, 0, 1) ** 1.5
L *= g * fade; R *= g * fade
st = np.stack([L, R], 1)
st = np.tanh(st / np.max(np.abs(st)) * 1.4); st = st / np.max(np.abs(st)) * .89
st = st[:int(TOTAL_BEATS * B * SR)]
out = sys.argv[1] if len(sys.argv) > 1 else 'score.wav'
with wave.open(out, 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((st * 32767).astype(np.int16).tobytes())
print(out, len(st) / SR)
