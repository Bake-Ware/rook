"""Lo-fi hip hop pass on "In the Hall of the Mountain King" (Grieg, 1875, public domain), our own arrangement.
Swung boom-bap at 80 bpm, Rhodes 9th chords, a felt-piano melody that keeps Grieg's tune, tape wobble,
vinyl crackle, a tape stop. bed(T) is cut to clip 4's picture; loop(secs) is a free-running sample.

    python score_lofi.py      -> out/music/lofi/{c4,loop}.mp3 + index.json
"""
import json, subprocess, wave
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
SR = 48000
BPM = 80
rng = np.random.default_rng(23)
mtof = lambda m: 440.0 * 2 ** ((m - 69) / 12)

MEL = [(59, .5), (61, .5), (62, .5), (64, .5), (66, .5), (62, .5), (66, 1),
       (65, .5), (61, .5), (65, 1), (64, .5), (60, .5), (64, 1),
       (59, .5), (61, .5), (62, .5), (64, .5), (66, .5), (62, .5), (66, .5), (71, .5),
       (69, .5), (66, .5), (62, .5), (66, .5), (69, 2)]
MAJ = {62: 63, 65: 66, 60: 61, 69: 68}                       # B minor -> B major colour for the morning
# one chord per half bar (8 per pass of the tune), voiced for Rhodes; bass roots alongside
CH_MIN = [('Bm9', [47, 57, 61, 62, 66]), ('Bm9', [47, 57, 61, 62, 66]), ('F#7', [42, 52, 58, 61, 64]), ('Cmaj7', [48, 52, 55, 59, 64]),
          ('Bm9', [47, 57, 61, 62, 66]), ('Bm9', [47, 57, 61, 62, 66]), ('Dmaj7', [50, 54, 57, 61, 66]), ('F#7', [42, 52, 58, 61, 64])]
CH_MAJ = [('Bmaj9', [47, 58, 61, 63, 66]), ('Bmaj9', [47, 58, 61, 63, 66]), ('F#9', [42, 52, 56, 58, 61]), ('Emaj7', [40, 51, 56, 59, 63]),
          ('Bmaj9', [47, 58, 61, 63, 66]), ('G#m7', [44, 54, 59, 63, 66]), ('Emaj7', [40, 51, 56, 59, 63]), ('F#9', [42, 52, 56, 58, 61])]


# ---------------------------------------------------------------- instruments
def env(n, a, d, s, r):
    e = np.full(n, s, float); ia = min(int(a * SR), n); e[:ia] = np.linspace(0, 1, ia, endpoint=False)
    k = min(int(d * SR), n - ia); e[ia:ia + k] = np.linspace(1, s, k, endpoint=False)
    ir = min(int(r * SR), n)
    if ir: e[-ir:] *= np.linspace(1, 0, ir)
    return e
def lp_fft(x, fc, order=2):
    X = np.fft.rfft(x); f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(X / np.sqrt(1 + (f / fc) ** (2 * order)), len(x))
def rhodes(m, dur, vel=1.0):
    n = int((dur + .6) * SR); t = np.arange(n) / SR; f = mtof(m)
    bell = np.sin(2 * np.pi * f * t + 1.2 * np.exp(-t / .25) * np.sin(2 * np.pi * f * 14 * t) * .15)
    body = np.sin(2 * np.pi * f * t) + .25 * np.sin(4 * np.pi * f * t) * np.exp(-t / .6)
    trem = 1 + .18 * np.sin(2 * np.pi * 4.2 * t)
    e = np.exp(-t / 2.2) * env(n, .006, .1, 1, .35)
    return (body * .8 + bell * .25 * np.exp(-t / .4)) * trem * e * vel
def felt(m, dur, vel=1.0):
    """muted felt piano: soft hammer, few partials, fast decay of the top"""
    n = int((dur + .9) * SR); t = np.arange(n) / SR; f = mtof(m)
    wob = 1 + .0025 * np.sin(2 * np.pi * .7 * t + m)
    x = sum(a * np.sin(2 * np.pi * f * k * wob * t) * np.exp(-t * (1.6 + k * 1.4)) for k, a in zip(range(1, 6), [1, .45, .2, .1, .05]))
    hammer = np.convolve(rng.standard_normal(n), np.ones(30) / 30, 'same') * np.exp(-t / .01) * .05
    return (x + hammer) * env(n, .008, .2, 1, .25) * vel
def sub(m, dur):
    n = int(dur * SR); t = np.arange(n) / SR
    return np.sin(2 * np.pi * mtof(m) * t) * env(n, .01, .1, .85, .08)
def kick():
    n = int(.35 * SR); t = np.arange(n) / SR; f = 48 + 75 * np.exp(-t / .035)
    return np.tanh(np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / .16) * 1.6)
def snare():
    n = int(.3 * SR); t = np.arange(n) / SR
    noise = rng.standard_normal(n); noise = noise - np.convolve(noise, np.ones(8) / 8, 'same')
    body = np.sin(2 * np.pi * 185 * t) * np.exp(-t / .05)
    return (np.convolve(noise, np.ones(3) / 3, 'same') * np.exp(-t / .09) * .55 + body * .5)
def hat(open_=False):
    n = int((.18 if open_ else .05) * SR); t = np.arange(n) / SR
    x = rng.standard_normal(n); x = x - np.convolve(x, np.ones(4) / 4, 'same')
    return x * np.exp(-t / (.06 if open_ else .014)) * .5
def crackle(T):
    n = int(T * SR); x = np.convolve(rng.standard_normal(n), np.ones(12) / 12, 'same') * .012
    pops = rng.random(n) < 7 / SR
    x[pops] += rng.choice([-1, 1], pops.sum()) * rng.uniform(.15, .5, pops.sum())
    return lp_fft(x, 5000)


class Bus:
    def __init__(self, T): self.x = np.zeros(int(T * SR) + SR)
    def add(self, t, s, g=1.0):
        i = int(t * SR); k = min(len(s), len(self.x) - i)
        if k > 0 and i >= 0: self.x[i:i + k] += s[:k] * g


B = 60 / BPM
def swing(t0, h):
    """time of half-beat h from t0, with lazy swing on the off-beats"""
    return t0 + (h // 2) * B + (B * .6 if h % 2 else 0)


def tune(bus, t0, t1, inst, g, maj=False, transpose=0):
    h = 0; k = 0
    while True:
        m, d = MEL[k % len(MEL)]; n = int(round(d * 2))
        t = swing(t0, h)
        if t >= t1: break
        if maj: m = MAJ.get(m, m)
        vel = .85 + .15 * ((k * 7) % 5) / 4                    # human-ish dynamics
        bus.add(t + rng.uniform(-.008, .012), inst(m + transpose, n * B / 2 * .95, vel), g)
        h += n; k += 1


def comp(keys, bass, t0, t1, chords, g=1.0, bassline=True):
    """Rhodes chords, one per half bar, with a lazy roll; sub bass on the root"""
    i = 0
    while t0 + i * 2 * B < t1 - .05:
        name, v = chords[i % len(chords)]; t = t0 + i * 2 * B
        for j, m in enumerate(v[1:]):
            keys.add(t + j * .018, rhodes(m, 2 * B * .95, .5), .22 * g)
        if bassline:
            bass.add(t, sub(v[0] - 12 + 12 * (v[0] < 40), B * 1.3), .5 * g)
            bass.add(t + B * 1.5, sub(v[0] - 12 + 12 * (v[0] < 40), B * .45), .35 * g)
        i += 1


def beat(drums, t0, t1, g=1.0, sparse=False):
    i = 0
    while t0 + i * B < t1 - .05:
        t = t0 + i * B; s = i % 4
        if s == 0 or (s == 2 and not sparse): drums.add(t, kick(), .9 * g)
        if s == 2 and not sparse: drums.add(t + B * .6, kick(), .5 * g)
        if s in (1, 3) and not sparse: drums.add(t + .012, snare(), .55 * g)
        if not sparse:
            drums.add(t, hat(), .22 * g); drums.add(t + B * .6, hat(s == 3), .16 * g)
        i += 1


def tape_stop(x, t0, t1):
    """slow the tape to a halt between t0 and t1 (pitch and speed fall together), silence after"""
    i0, i1 = int(t0 * SR), int(t1 * SR)
    rate = np.linspace(1, 0, i1 - i0) ** 1.4
    pos = i0 + np.cumsum(rate)
    seg = np.interp(pos, np.arange(len(x)), x)
    x = x.copy(); x[i0:i1] = seg * np.linspace(1, .6, i1 - i0)
    return x


def master(mix, T, tape=None):
    mix = lp_fft(mix, 7000, 1) * .9 + mix * .1                 # dusty top end
    n = len(mix); t = np.arange(n) / SR
    wow = (.0035 * np.sin(2 * np.pi * .55 * t) + .0015 * np.sin(2 * np.pi * 3.1 * t))   # tape wow and flutter
    mix = np.interp(np.arange(n) + wow * SR * .02, np.arange(n), mix)
    if tape: mix = tape_stop(mix, *tape)
    mix += crackle(n / SR)[:n]
    return np.tanh(mix * 1.4) * .55


def bed(T):
    keys, bass, drums, lead = Bus(T), Bus(T), Bus(T), Bus(T)
    # 2.0-3.9: the record drops in - crackle and a filtered Bm9
    keys.add(2.0, lp_fft(sum(rhodes(m, 1.8, .5) for m in [57, 61, 62, 66]), 900), .3)
    # 3.9-6.6: the tune alone on felt piano, no beat - the intruder creeps
    tune(lead, 3.9, 6.6, felt, .5, transpose=-12)
    # 6.6: the beat drops; chords, bass, the tune
    comp(keys, bass, 6.6, 26.6, CH_MIN)
    beat(drums, 6.6, 26.6)
    tune(lead, 6.6, 12.4, felt, .5)
    # 12.4-26.6: under the voice - the beat and chords carry it, a quiet low echo of the tune
    tune(lead, 12.4, 26.6, felt, .18, transpose=-12)
    # 26.6-36: the fix - the tune back up an octave with a Rhodes double, busier hats
    comp(keys, bass, 26.6, 36.0, CH_MIN, 1.1)
    beat(drums, 26.6, 36.0, 1.1)
    tune(lead, 26.6, 35.2, felt, .55, transpose=12)
    tune(lead, 26.6, 35.2, lambda m, d, v: rhodes(m, d, v), .18)
    # 36: after the tape stop, a soft chord lands
    keys.add(36.0, sum(rhodes(m, 3.0, .6) for m in [57, 61, 62, 66]), .3); bass.add(36.0, sub(35, 2.5), .55)
    # 36-43.6: the report - chords and a lazy kick only
    comp(keys, bass, 37.6, 43.6, CH_MIN, .7, bassline=False)
    beat(drums, 37.6, 43.6, .7, sparse=True)
    # 43.6-51: morning - the tune in B major on Rhodes, the beat back
    comp(keys, bass, 43.6, 51.2, CH_MAJ)
    beat(drums, 43.6, 51.2)
    tune(lead, 43.6, 51.2, felt, .45, maj=True)
    # 51.2-56: last chord rings out under the end card; a little plink as the full stop drops
    keys.add(51.2, sum(rhodes(m, 4.5, .6) for m in [58, 61, 63, 66, 70]), .3); bass.add(51.2, sub(35, 4), .5)
    for k, (m, t) in enumerate([(95, 54.5), (88, 54.7), (83, 54.95)]):
        lead.add(t, felt(m, .3, .6), .25 / (k + 1))
    mix = keys.x + bass.x * .9 + drums.x * .8 + lead.x
    return master(mix, T, tape=(35.2, 36.0))


def loop(secs=40.0):
    """a free-running version (intro, the tune twice, the major turn, outro) for the Music tab"""
    T = secs; keys, bass, drums, lead = Bus(T), Bus(T), Bus(T), Bus(T)
    bar = 4 * B; t0 = .4
    keys.add(t0, lp_fft(sum(rhodes(m, 3, .5) for m in [57, 61, 62, 66]), 900), .3)
    a = t0 + 2 * bar
    comp(keys, bass, a, a + 8 * bar, CH_MIN); beat(drums, a, a + 8 * bar)
    tune(lead, a, a + 8 * bar, felt, .5)
    b = a + 8 * bar
    comp(keys, bass, b, b + 4 * bar, CH_MAJ); beat(drums, b, b + 4 * bar)
    tune(lead, b, b + 4 * bar, felt, .45, maj=True)
    keys.add(b + 4 * bar, sum(rhodes(m, 4, .6) for m in [58, 61, 63, 66, 70]), .3)
    mix = keys.x + bass.x * .9 + drums.x * .8 + lead.x
    return master(mix, T)[:int(min(T, b + 4 * bar + 4) * SR)]


def save(x, path):
    pcm = np.int16(np.clip(np.repeat(x[:, None], 2, 1) / max(1e-9, np.abs(x).max()) * .9, -1, 1) * 32767)
    wav = path.with_suffix('.wav')
    with wave.open(str(wav), 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(wav), '-b:a', '192k', str(path)], check=True); wav.unlink()


if __name__ == '__main__':
    out = HERE / 'out' / 'music' / 'lofi'; out.mkdir(parents=True, exist_ok=True)
    save(bed(56.0)[:int(56 * SR)], out / 'c4.mp3')
    save(loop(), out / 'loop.mp3')
    (out / 'index.json').write_text(json.dumps([
        {'title': 'Lo-fi Mountain King, cut to clip 4', 'file': 'out/music/lofi/c4.mp3', 'note': 'timed to the picture; tape stop into the hit at 36 s'},
        {'title': 'Lo-fi Mountain King, loop', 'file': 'out/music/lofi/loop.mp3', 'note': 'free-running: intro, the tune twice, the major turn'},
    ], indent=1))
    print('wrote', out)
