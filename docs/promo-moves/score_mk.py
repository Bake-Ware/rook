"""Clip 4's score: our own remix of "In the Hall of the Mountain King" (Grieg, 1875, public domain),
cut to the picture. score.py calls bed(T) when the clip's mood says theme: 'mountainking'.

  2.0-3.9   the title: one low note, a held breath
  3.9-6.6   the intruder creeps: pizzicato theme, low and slow
  6.6-12.4  it lands: the theme on a pulse lead, alarm pulse underneath
  12.4-27   the call (under the voice): the tune plucked low, bass ostinato, ticking
  27-36     the fix: full chiptune, accelerating 120 -> 176 bpm, lands on a hit at 36
  36-43.6   the report: a quiet tick and the bass
  43.6-51   morning: the theme turned to B major, soft bells
  51.2-56   end card: last cadence, and a plink as the full stop drops
"""
import numpy as np

SR = 48000
rng = np.random.default_rng(11)
mtof = lambda m: 440.0 * 2 ** ((m - 69) / 12)

# the tune, B minor, in beats (eighths = .5)
MEL = [(59, .5), (61, .5), (62, .5), (64, .5), (66, .5), (62, .5), (66, 1),
       (65, .5), (61, .5), (65, 1), (64, .5), (60, .5), (64, 1),
       (59, .5), (61, .5), (62, .5), (64, .5), (66, .5), (62, .5), (66, .5), (71, .5),
       (69, .5), (66, .5), (62, .5), (66, .5), (69, 2)]
BASS = [47, 47, 49, 49, 47, 47, 45, 45]          # B B C# C# B B A A, two per bar
MAJ = {62: 63, 64: 64, 66: 66, 65: 66, 60: 61, 69: 68, 71: 71, 59: 59, 61: 61}   # minor -> B major colour


def env(n, a, d, s, r):
    e = np.full(n, s, float); ia = min(int(a * SR), n); e[:ia] = np.linspace(0, 1, ia, endpoint=False)
    k = min(int(d * SR), n - ia); e[ia:ia + k] = np.linspace(1, s, k, endpoint=False)
    ir = min(int(r * SR), n)
    if ir: e[-ir:] *= np.linspace(1, 0, ir)
    return e
def crush(x, bits): q = 2 ** (bits - 1); return np.round(x * q) / q
def pulse(m, dur, duty=.25):
    n = max(1, int(dur * SR)); ph = np.cumsum(np.full(n, mtof(m) / SR)) % 1
    return crush(np.where(ph < duty, 1., -1.) * env(n, .002, .06, .65, .02), 5)
def tri(m, dur):
    n = max(1, int(dur * SR)); ph = np.cumsum(np.full(n, mtof(m) / SR)) % 1
    return crush((4 * np.abs(ph - .5) - 1) * env(n, .002, .04, .9, .015), 4)
def pizz(m, dur=.35):
    n = int(dur * SR); t = np.arange(n) / SR; f = mtof(m)
    return (np.sin(2 * np.pi * f * t) + .4 * np.sin(4 * np.pi * f * t)) * np.exp(-t / .09)
def bell(m, dur=1.4):
    n = int(dur * SR); t = np.arange(n) / SR; f = mtof(m)
    return (np.sin(2 * np.pi * f * t) + .3 * np.sin(2 * np.pi * 2.01 * f * t) * np.exp(-t / .2)) * np.exp(-t / .6)
def hiss(dur, decay):
    n = int(dur * SR); t = np.arange(n) / SR
    return rng.choice([-1., 1.], n) * np.exp(-t / decay)
def kick():
    n = int(.14 * SR); t = np.arange(n) / SR; f = 46 + 160 * np.exp(-t / .02)
    return crush(np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR)) * np.exp(-t / .05), 4)
def pad(ms, dur):
    n = int(dur * SR); t = np.arange(n) / SR; x = np.zeros(n)
    for m in ms:
        for dv in (-.1, .1):
            ph = np.cumsum(np.full(n, mtof(m) * 2 ** (dv / 12) / SR)) % 1; x += 2 * ph - 1
    k = np.ones(40) / 40; x = np.convolve(x, k, 'same')                        # soft
    return x / (2 * len(ms)) * env(n, .8, .5, .8, 1.2)


class Bus:
    def __init__(self, T): self.x = np.zeros(int(T * SR) + SR)
    def add(self, t, s, g=1.0):
        i = int(t * SR); k = min(len(s), len(self.x) - i)
        if k > 0 and i >= 0: self.x[i:i + k] += s[:k] * g


def beats(t0, t1, bpm0, bpm1=None):
    """times of successive half-beats from t0 to t1, tempo ramping bpm0 -> bpm1"""
    bpm1 = bpm1 or bpm0; out, t = [], t0
    while t < t1:
        out.append(t); u = (t - t0) / max(1e-6, t1 - t0)
        t += 30 / (bpm0 + (bpm1 - bpm0) * u)
    return out


def play_mel(bus, grid, inst, g, transpose=0, maj=False):
    """lay the tune on a half-beat grid (looping), each note lasting its half-beats"""
    seq = []
    for m, d in MEL:
        seq.append((m, int(round(d * 2))))
    i = k = 0
    while i < len(grid):
        m, n = seq[k % len(seq)]
        if maj: m = MAJ.get(m, m)
        end = grid[min(i + n, len(grid) - 1)] if i + n < len(grid) else grid[i] + .3
        bus.add(grid[i], inst(m + transpose, max(.08, (end - grid[i]) * .92)), g)
        i += n; k += 1


def bed(T):
    lead, bass, perc, air = Bus(T), Bus(T), Bus(T), Bus(T)
    # 2.0-3.9: a held breath
    bass.add(2.0, tri(35, 1.9) * env(int(1.9 * SR), .3, .2, .8, .6), .5)
    # 3.9-6.6: the intruder creeps, pizzicato, low
    g = beats(3.9, 6.6, 88)
    play_mel(lead, g, pizz, .55, -12)
    for j, t in enumerate(g[::2]): bass.add(t, tri(BASS[j % 8] - 12, .25), .5)
    # 6.6-12.4: it lands; theme on pulse, the alarm pulse under it
    g = beats(6.6, 12.4, 100)
    play_mel(lead, g, lambda m, d: pulse(m, d, .125), .28, 0)
    for j, t in enumerate(g[::2]):
        bass.add(t, tri(BASS[j % 8] - 12, .3), .55)
        if j % 2 == 0: perc.add(t, kick(), .35)
    air.add(6.6, pad([59, 62, 66], 5.8), .18)
    # 12.4-27: under the voice - bass ostinato, ticking
    g = beats(12.4, 26.6, 100)
    for j, t in enumerate(g[::2]): bass.add(t, tri(BASS[j % 8] - 12, .28), .45)
    for t in g[1::2]: perc.add(t, hiss(.03, .008), .1)
    for j, t in enumerate(g[::8]): perc.add(t, kick(), .25)                   # a slow pulse
    play_mel(lead, g, pizz, .4, -12)                                          # the tune, plucked low, under the voice
    air.add(12.4, pad([47, 54, 59], 14.6), .22)
    # 26.6-36: the fix, accelerating, full
    g = beats(26.6, 36.0, 120, 176)
    play_mel(lead, g, lambda m, d: pulse(m, d, .25), .3, 12)
    play_mel(lead, g, lambda m, d: pulse(m, d, .5), .1, 0)
    for j, t in enumerate(g[::2]):
        bass.add(t, tri(BASS[j % 8] - 12 + (12 if j % 2 else 0), .2), .55)
        perc.add(t, kick() if j % 2 == 0 else hiss(.12, .045), .4 if j % 2 == 0 else .2)
    for t in g[1::2]: perc.add(t, hiss(.04, .01), .1)
    # the hit at 36
    perc.add(36.0, kick(), .7); bass.add(36.0, tri(35, 1.6), .7); air.add(36.0, pad([47, 59, 62, 66], 2.4), .3)
    lead.add(36.0, pulse(71, 1.2, .25), .3)
    # 36-43.6: the report, quiet
    g = beats(37.2, 43.6, 92)
    for j, t in enumerate(g[::4]): bass.add(t, tri(BASS[(j * 2) % 8] - 12, .3), .35)
    for t in g[::2]: perc.add(t, hiss(.02, .006), .05)
    # 43.6-51: morning, the theme in B major, bells
    g = beats(43.6, 51.0, 84)
    play_mel(lead, g, lambda m, d: bell(m + 12, 1.3), .2, 0, maj=True)
    air.add(43.6, pad([59, 63, 66], 7.6), .16)
    # 51.2-56: last cadence; the full stop drops at ~54.5
    air.add(51.2, pad([54, 58, 61], 1.6), .2); air.add(52.6, pad([59, 63, 66, 71], 3.4), .22)
    lead.add(52.6, bell(83, 2.4), .22)
    for k, (m, t) in enumerate([(95, 54.5), (88, 54.7), (83, 54.95)]):        # the plink as it falls
        lead.add(t, bell(m, .5), .12 / (k + 1))
    mix = lead.x + bass.x * .8 + perc.x + air.x
    return np.tanh(mix * 1.1) * .5
