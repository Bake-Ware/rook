"""Soundtrack for one Rook Moves clip: an underscore in the clip's key and tempo, sound
cues from the clip's events, and the voice lines, ducked under the voice.

    python score.py N            # reads out/cN.json and voices/*.wav -> out/cN_mix.wav (48 kHz stereo)
Only numpy is needed.
"""
import json, sys, wave
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
SR = 48000
NOTE = {'C': 0, 'C#': 1, 'D': 2, 'D#': 3, 'E': 4, 'F': 5, 'F#': 6, 'G': 7, 'G#': 8, 'A': 9, 'A#': 10, 'B': 11}
rng = np.random.default_rng(7)


def hz(m): return 440.0 * 2 ** ((m - 69) / 12)


def env(n, a, d, s, r, sus_len=None):
    a, d, r = int(a * SR), int(d * SR), int(r * SR)
    sus = max(0, n - a - d - r) if sus_len is None else int(sus_len * SR)
    e = np.concatenate([np.linspace(0, 1, max(a, 1)), np.linspace(1, s, max(d, 1)), np.full(sus, s), np.linspace(s, 0, max(r, 1))])
    return e[:n] if len(e) >= n else np.pad(e, (0, n - len(e)))


def lp_fast(x, fc):
    """One-pole low-pass, block-vectorised with numpy cumulative tricks (good enough for pads)."""
    a = np.exp(-2 * np.pi * fc / SR)
    n = len(x)
    out = np.empty(n)
    blk = max(16, min(4096, int(150 / max(1e-9, -np.log10(a))))); z = 0.0
    p = a ** np.arange(1, blk + 1)
    for s in range(0, n, blk):
        seg = x[s:s + blk]; m = len(seg)
        # y[k] = (1-a) * sum_{j<=k} a^(k-j) x[j] + a^(k+1) z
        w = (1 - a) * seg / (a ** np.arange(m))
        y = np.cumsum(w) * (a ** np.arange(m)) + p[:m] * z
        out[s:s + m] = y; z = y[-1]
    return out


class Mix:
    def __init__(self, T):
        self.n = int(T * SR) + SR
        self.L = np.zeros(self.n); self.R = np.zeros(self.n)

    def add(self, t, sig, gain=1.0, pan=0.0):
        i = int(t * SR)
        if i >= self.n or len(sig) == 0: return
        if i < 0: sig = sig[-i:]; i = 0
        sig = sig[:self.n - i] * gain
        self.L[i:i + len(sig)] += sig * np.sqrt(.5 * (1 - pan)) * 1.414
        self.R[i:i + len(sig)] += sig * np.sqrt(.5 * (1 + pan)) * 1.414


def tone(f, dur, kind='sine', a=.005, d=.1, s=.6, r=.2, detune=0.0):
    n = int(dur * SR); t = np.arange(n) / SR
    if kind == 'sine':
        x = np.sin(2 * np.pi * f * t)
    elif kind == 'pad':
        x = sum(np.sin(2 * np.pi * f * (1 + dt) * t + k) / (k + 1) for k, dt in enumerate((0, .004, -.003)))
        x += .25 * np.sin(2 * np.pi * 2 * f * t) + .12 * np.sin(2 * np.pi * 3 * f * t)
    elif kind == 'pluck':
        x = np.sin(2 * np.pi * f * t) + .5 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t * 6) + .25 * np.sin(2 * np.pi * 3 * f * t) * np.exp(-t * 9)
    elif kind == 'piano':
        x = sum(np.sin(2 * np.pi * f * h * (1 + .0004 * h * h) * t) * np.exp(-t * (1.6 + h * .9)) / h ** 1.1 for h in range(1, 7))
    elif kind == 'bell':
        x = sum(np.sin(2 * np.pi * f * h * t) * np.exp(-t * (1.2 + h * .8)) * w for h, w in ((1, 1), (2.76, .5), (5.4, .25), (8.9, .12)))
    return x * env(n, a, d, s, r)


def noise(dur):
    return rng.standard_normal(int(dur * SR))


def chords_for(key, minor):
    r = 48 + NOTE[key]
    prog = [(0, 3, 7), (8, 12, 15), (3, 7, 10), (10, 14, 17)] if minor else [(0, 4, 7), (9, 12, 16), (5, 9, 12), (7, 11, 14)]
    return [[r + k for k in c] for c in prog]


def underscore(m, T, mood):
    bpm = mood.get('bpm', 96); beat = 60 / bpm; bar = 4 * beat
    minor = mood.get('minor', False)
    ch = chords_for(mood.get('key', 'D'), minor)
    energy = mood.get('energy', .7)
    start, stop = 1.6, T - 4.6
    nb = int((stop - start) / bar) + 1
    for b in range(nb):
        t0 = start + b * bar
        c = ch[b % 4]
        for i, note in enumerate(c):
            m.add(t0, tone(hz(note), bar + .6, 'pad', a=.6, d=.4, s=.7, r=.7) * .05, pan=(i - 1) * .4)
        m.add(t0, tone(hz(c[0] - 12), bar, 'sine', a=.02, d=.3, s=.6, r=.3) * .09)
        if b >= 1:
            for k in range(8):                                   # arpeggio, 8ths
                note = c[(0, 1, 2, 1, 2, 0, 2, 1)[k]] + 12 + (12 if k in (4, 6) else 0)
                m.add(t0 + k * beat / 2, tone(hz(note), .5, 'pluck', a=.003, d=.2, s=.15, r=.2) * .045 * energy, pan=.35 * np.sin(k))
        if b >= 2 and energy > .5:                               # soft kick + shaker
            for k in range(4):
                tk = t0 + k * beat
                n = int(.25 * SR); tt = np.arange(n) / SR
                kick = np.sin(2 * np.pi * (50 + 90 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 14)
                m.add(tk, kick * .22 * energy)
                m.add(tk + beat / 2, lp_fast(noise(.06), 7000) * env(int(.06 * SR), .002, .02, .3, .03) * .05 * energy, pan=.3)


def cue(m, e):
    t, k = e['t'], e['type']
    if k == 'sting':
        for i, n in enumerate((62, 69, 74, 78)):
            m.add(t + .15 + i * .09, tone(hz(n), 2.6, 'bell', a=.002, d=.5, s=.3, r=1.2) * .09, pan=(i - 1.5) * .25)
        sw = lp_fast(noise(1.8), 1800) * env(int(1.8 * SR), 1.2, .3, .4, .3) * .05
        m.add(t, sw)
    elif k == 'end':
        for i, n in enumerate((50, 57, 62, 66, 69)):
            m.add(t + i * .05, tone(hz(n), 4.4, 'pad', a=.3, d=.6, s=.6, r=1.6) * .055, pan=(i - 2) * .2)
        m.add(t + .3, tone(hz(74), 3.0, 'bell', a=.002) * .07)
    elif k == 'pulse':
        n = int(.11 * SR); tt = np.arange(n) / SR
        f = 700 + 900 * tt / tt[-1]
        m.add(t, np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, .005, .03, .5, .06) * .06, pan=.2)
    elif k == 'land':
        m.add(t, tone(1568, .25, 'bell', a=.001) * .05, pan=-.2)
    elif k in ('whoosh', 'rush'):
        d = .55 if k == 'whoosh' else 1.1
        x = noise(d); n = len(x); tt = np.arange(n) / n
        sweep = lp_fast(x, 2500) * np.sin(np.pi * tt) ** 2
        m.add(t - (d * .5 if k == 'whoosh' else d * .8), sweep * (.09 if k == 'whoosh' else .12))
    elif k == 'shimmer':
        for i in range(6):
            m.add(t + i * .07, tone(hz(86 + (i % 3) * 3), 1.2, 'bell', a=.002) * .025, pan=(i % 2) * .6 - .3)
    elif k == 'alarm':
        n = int(1.6 * SR); tt = np.arange(n) / SR
        x = (np.sin(2 * np.pi * 110 * tt) + .4 * np.sin(2 * np.pi * 220 * tt)) * env(n, .02, .2, .8, .6)
        m.add(t, x * .2)
    elif k == 'ring':
        for r in range(int(e.get('n', 2))):
            n = int(1.0 * SR); tt = np.arange(n) / SR
            x = (np.sin(2 * np.pi * 440 * tt) + np.sin(2 * np.pi * 480 * tt)) * (np.sin(2 * np.pi * 20 * tt) > 0) * env(n, .01, .1, .8, .1)
            m.add(t + r * 1.4, x * .045, pan=.3)
    elif k == 'buzz':
        for r in range(2):
            n = int(.35 * SR); tt = np.arange(n) / SR
            m.add(t + r * .5, np.sin(2 * np.pi * 160 * tt) * (np.sin(2 * np.pi * 30 * tt) * .5 + .5) * env(n, .01, .05, .9, .05) * .12)
    elif k == 'tap':
        m.add(t, lp_fast(noise(.03), 3000) * env(int(.03 * SR), .001, .01, .2, .01) * .2)
    elif k == 'print':
        for i in range(int(e.get('dur', 4) * 6)):
            m.add(t + i / 6, tone(880 + 220 * (i % 3), .08, 'sine', a=.002, d=.03, s=.2, r=.03) * .012)
    elif k == 'melody':
        # Celtic-flavoured piano tune, D mixolydian-ish, 8 bars at e['bpm']
        bpm = e.get('bpm', 110); b = 60 / bpm
        mel = [(74, 1), (76, .5), (78, .5), (79, 1), (78, .5), (76, .5), (74, 1), (71, 1), (69, 2),
               (74, 1), (76, .5), (78, .5), (81, 1), (79, .5), (78, .5), (76, 1.5), (74, .5), (76, 2),
               (78, 1), (79, .5), (81, .5), (83, 1), (81, .5), (79, .5), (78, 1), (76, 1), (74, 2),
               (71, .5), (74, .5), (76, 1), (78, .5), (76, .5), (74, 1), (69, 1), (74, 2.5)]
        tt = t
        for note, d in mel:
            m.add(tt, tone(hz(note), d * b + 1.2, 'piano', a=.002, d=.2, s=.5, r=.6) * .16, pan=.15)
            tt += d * b
        for i, root in enumerate([50, 48, 55, 50, 50, 48, 55, 50]):        # left hand
            for j, n in enumerate((root, root + 7, root + 12)):
                m.add(t + i * 4 * b + j * b, tone(hz(n), 1.6, 'piano', a=.002) * .07, pan=-.2)


def read_wav(p):
    with wave.open(str(p)) as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64) / 32768
    if ch == 2: x = x.reshape(-1, 2).mean(axis=1)
    if sr != SR:
        x = np.interp(np.arange(int(len(x) * SR / sr)) / SR, np.arange(len(x)) / sr, x)
    return x


def main():
    n = sys.argv[1]
    meta = json.loads((HERE / 'out' / f'c{n}.json').read_text())
    T = meta['T']
    music, fx, vox = Mix(T), Mix(T), Mix(T)
    underscore(music, T, meta.get('mood', {}))
    for e in meta['events']:
        cue(fx, e)
    duck = np.ones(music.n)
    for v in meta['voice']:
        p = HERE / 'voices' / f"{v['id']}.wav"
        if p.exists():
            x = read_wav(p)
            x = x / (np.sqrt(np.mean(x ** 2)) + 1e-9) * .12                    # level every line the same
            vox.add(v['t'], x, 1.0, pan=0.0 if v['who'] == 'rook' else -.08)
            d = len(x) / SR
        else:
            d = v.get('dur', 2)
        a, b = int(v['t'] * SR), int((v['t'] + d) * SR)
        duck[max(0, a - int(.15 * SR)):b + int(.3 * SR)] = .38
    duck = lp_fast(duck, 3.0)
    L = music.L * duck + fx.L + vox.L
    R = music.R * duck + fx.R + vox.R
    # fade the tail, soft clip
    nT = int(T * SR)
    L, R = L[:nT], R[:nT]
    f = np.ones(nT); f[-int(.5 * SR):] = np.linspace(1, 0, int(.5 * SR))
    st = np.stack([L * f, R * f], axis=1)
    st = np.tanh(st * 1.2) / 1.2
    pcm = (st / max(1e-9, np.abs(st).max()) * .89 * 32767).astype(np.int16)
    out = HERE / 'out' / f'c{n}_mix.wav'
    with wave.open(str(out), 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
    print('wrote', out, f'{T:.1f}s')


if __name__ == '__main__':
    main()
