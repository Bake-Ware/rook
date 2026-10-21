"""Music audition: our own chiptune arrangements of public-domain tunes, ~16 s each ->
out/music/<id>.wav + index.json (shown on the Music tab of the clip list).
    python tunes.py
Only the melodies are borrowed (all public domain); every arrangement here is synthesized from scratch."""
import json, wave
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
SR = 44100
rng = np.random.default_rng(7)
mtof = lambda m: 440.0 * 2 ** ((m - 69) / 12)
R = None


# ---------------------------------------------------------------- instruments
def env(n, a, d, s, r):
    e = np.full(n, s, float)
    ia, idd, ir = int(a * SR), int(d * SR), int(r * SR)
    ia = min(ia, n); e[:ia] = np.linspace(0, 1, ia, endpoint=False)
    k = min(idd, n - ia); e[ia:ia + k] = np.linspace(1, s, k, endpoint=False)
    if ir: e[-min(ir, n):] *= np.linspace(1, 0, min(ir, n))
    return e
def crush(x, bits): q = 2 ** (bits - 1); return np.round(x * q) / q
def pulse(m, dur, duty=.25, vib=True):
    n = max(1, int(dur * SR)); t = np.arange(n) / SR
    f = mtof(m) * (1 + (.006 * np.sin(2 * np.pi * 6 * t) * np.clip((t - .12) / .1, 0, 1) if vib else 0))
    ph = np.cumsum(f / SR) % 1
    return crush(np.where(ph < duty, 1., -1.) * env(n, .002, .07, .7, .02), 5)
def tri(m, dur):
    n = max(1, int(dur * SR)); ph = np.cumsum(np.full(n, mtof(m) / SR)) % 1
    return crush((4 * np.abs(ph - .5) - 1) * env(n, .002, .04, .9, .01), 4)
def noise(dur, decay, period=1):
    n = int(dur * SR); t = np.arange(n) / SR
    x = np.repeat(rng.choice([-1., 1.], n // period + 1), period)[:n]
    return x * np.exp(-t / decay)
def kick():
    n = int(.13 * SR); t = np.arange(n) / SR; f = 48 + 170 * np.exp(-t / .02)
    return crush(np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR)) * np.exp(-t / .05), 4)
def bell(m, dur):
    n = int(dur * SR); t = np.arange(n) / SR; f = mtof(m)
    return (np.sin(2 * np.pi * f * t) + .3 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t / .2)) * np.exp(-t / .5)


# ---------------------------------------------------------------- harmony: pick a diatonic triad per bar
MODES = {'major': [0, 2, 4, 5, 7, 9, 11], 'minor': [0, 2, 3, 5, 7, 8, 10], 'harm': [0, 2, 3, 5, 7, 8, 11], 'dorian': [0, 2, 3, 5, 7, 9, 10]}
def chords_for(mel, tonic, mode, bar):
    sc = [(tonic + s) % 12 for s in MODES[mode]]
    triads = [[sc[i], sc[(i + 2) % 7], sc[(i + 4) % 7]] for i in range(7)]
    total = sum(d for _, d in mel); out = []
    b0 = 0.0
    while b0 < total - 1e-6:
        score = np.zeros(7); pos = 0.0
        for m, d in mel:
            if m is not None and pos < b0 + bar and pos + d > b0:
                w = d * (1.6 if abs((pos - b0) % (bar / 2)) < 1e-6 else 1)
                for i, tr in enumerate(triads):
                    if m % 12 in tr: score[i] += w * (1.3 if m % 12 == tr[0] else 1)
            pos += d
        score[0] += .3; score[4] += .15                      # lean on the tonic and the dominant
        out.append(triads[int(np.argmax(score))]); b0 += bar
    return out


# ---------------------------------------------------------------- arranger
def arrange(tune):
    mel, bar, passes = tune['mel'], tune['bar'], tune['passes']
    length = sum(d for _, d in mel)
    ch = chords_for(mel, tune['tonic'], tune['mode'], bar)
    # time of each pass start, by its tempo
    starts, t = [], .15
    for bpm in passes: starts.append((t, 60 / bpm)); t += length * 60 / bpm
    N = int((t + 1.2) * SR)
    lead, bass, arp, drums = (np.zeros(N) for _ in range(4))
    def add(buf, sec, x, g):
        i = int(sec * SR); k = min(len(x), N - i)
        if k > 0: buf[i:i + k] += x[:k] * g
    style = tune.get('style', 'chip')
    for p, (t0, B) in enumerate(starts):
        pos = 0.0
        for m, d in mel:                                       # the tune
            if m is not None:
                if style == 'bell': add(lead, t0 + pos * B, bell(m + 12, max(d * B, .6)), .3)
                add(lead, t0 + pos * B, pulse(m + tune.get('oct', 0), d * B * .9, .125 if p % 2 else .25), .3 if style != 'bell' else .16)
            pos += d
        for i, tr in enumerate(ch):                            # bass, arps, drums per bar
            b0 = t0 + i * bar * B
            root = 36 + (tr[0] - 36) % 12
            steps = int(bar * 2)
            for k in range(steps):
                add(bass, b0 + k * B / 2, tri(root + (12 if k % 2 and style != 'bell' else 0), B * .45), .45)
            if style != 'bell' or p > 0:
                for k in range(int(bar * 4)):
                    add(arp, b0 + k * B / 4, pulse(60 + (tr[k % 3] - 60) % 12 + 12, B * .2, .5, vib=False), .07)
            if tune.get('drums', True) and not (style == 'bell' and p == 0):
                for k in range(int(bar)):
                    beat = b0 + k * B
                    add(drums, beat, kick() if k % 2 == 0 else noise(.14, .05, 2), .45 if k % 2 == 0 else .25)
                    add(drums, beat + B / 2, noise(.05, .012), .12)
    mix = lead + bass * .9 + arp + drums
    mix = np.tanh(mix * 1.3) * .8
    fade = int(.8 * SR); mix[-fade:] *= np.linspace(1, 0, fade)
    return mix


def wav(path, x):
    pcm = np.int16(np.clip(x, -1, 1) * 32000)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------- the tunes (melody, beats; R = rest)
E5, D5, C5, B4, A4, G4, F4, E4, D4, C4 = 76, 74, 72, 71, 69, 67, 65, 64, 62, 60
TUNES = [
    dict(id='morning', title='Morning Mood', origin='Grieg, Peer Gynt (1875)', clip='1 · Secretary', tonic=4, mode='major', bar=3,
         style='bell', drums=False, passes=[92, 100], oct=0,
         mel=[(80, 1), (78, .5), (76, 1), (75, .5), (76, 1), (78, .5), (80, 1), (78, .5), (80, 1), (78, .5), (80, 1), (83, .5),
              (80, 1), (78, .5), (76, 1.5), (R, 1.5)]),
    dict(id='sailor', title='Drunken Sailor', origin='Sea shanty (traditional)', clip='2 · Build it, don’t wait up', tonic=2, mode='dorian', bar=4,
         passes=[132],
         mel=[(A4, 1), (A4, .5), (A4, .5), (A4, 1), (A4, .5), (A4, .5), (A4, 1), (D4, 1), (F4, 1), (A4, 1),
              (G4, 1), (G4, .5), (G4, .5), (G4, 1), (G4, .5), (G4, .5), (G4, 1), (C4, 1), (E4, 1), (G4, 1),
              (A4, 1), (A4, .5), (A4, .5), (A4, 1), (A4, .5), (A4, .5), (A4, 1), (B4, 1), (C5, 1), (D5, 1),
              (C5, 1), (A4, 1), (G4, 1), (E4, 1), (D4, 2), (D4, 2)]),
    dict(id='washerwoman', title='The Irish Washerwoman', origin='Irish jig (traditional)', clip='3 · Team huddle', tonic=7, mode='major', bar=3,
         passes=[120, 126],
         mel=[(B4, .5), (G4, .5), (G4, .5), (D4, .5), (G4, .5), (G4, .5), (B4, .5), (G4, .5), (B4, .5), (D5, .5), (C5, .5), (B4, .5),
              (C5, .5), (A4, .5), (A4, .5), (E4, .5), (A4, .5), (A4, .5), (C5, .5), (A4, .5), (C5, .5), (E5, .5), (D5, .5), (C5, .5),
              (B4, .5), (G4, .5), (G4, .5), (D4, .5), (G4, .5), (G4, .5), (B4, .5), (G4, .5), (B4, .5), (D5, .5), (C5, .5), (B4, .5),
              (C5, .5), (B4, .5), (C5, .5), (A4, .5), (D5, .5), (C5, .5), (B4, .5), (G4, .5), (G4, .5), (G4, 1.5)]),
    dict(id='mountainking', title='In the Hall of the Mountain King', origin='Grieg, Peer Gynt (1875)', clip='4 · Security reporting', tonic=11, mode='harm', bar=4,
         passes=[100, 132, 176], oct=12,
         mel=[(59, .5), (61, .5), (62, .5), (64, .5), (66, .5), (62, .5), (66, 1),
              (65, .5), (61, .5), (65, 1), (64, .5), (60, .5), (64, 1),
              (59, .5), (61, .5), (62, .5), (64, .5), (66, .5), (62, .5), (66, .5), (71, .5),
              (69, .5), (66, .5), (62, .5), (66, .5), (69, 2)]),
    dict(id='scarborough', title='Scarborough Fair', origin='English ballad (traditional)', clip='5 · Research team', tonic=2, mode='dorian', bar=3,
         style='bell', passes=[96],
         mel=[(D4, 2), (D4, 1), (A4, 2), (A4, 1), (E4, 1.5), (F4, .5), (E4, 1), (D4, 3),
              (A4, 1), (C5, 2), (D5, 1), (C5, 2), (A4, 1), (B4, 1), (G4, 1), (A4, 3)]),
    dict(id='cancan', title='Galop infernal (the Can-can)', origin='Offenbach, Orpheus in the Underworld (1858)', clip='6 · Comic, song, shed', tonic=0, mode='major', bar=4,
         passes=[150, 165],
         mel=[(C4, 1), (D4, .5), (F4, .5), (E4, .5), (D4, .5), (G4, 1), (G4, 1), (G4, .5), (A4, .5), (E4, .5), (F4, .5), (D4, 1), (D4, 1),
              (D4, .5), (F4, .5), (E4, .5), (D4, .5), (C4, .5), (C5, .5), (B4, .5), (A4, .5), (G4, .5), (F4, .5), (E4, .5), (D4, .5), (C4, 2)]),
    dict(id='korobeiniki', title='Korobeiniki (the Tetris tune)', origin='Russian folk song (1861)', clip='7 · Make and track the campaign', tonic=9, mode='harm', bar=4,
         passes=[140],
         mel=[(E5, 1), (B4, .5), (C5, .5), (D5, 1), (C5, .5), (B4, .5), (A4, 1), (A4, .5), (C5, .5), (E5, 1), (D5, .5), (C5, .5),
              (B4, 1.5), (C5, .5), (D5, 1), (E5, 1), (C5, 1), (A4, 1), (A4, 1), (R, 1),
              (R, .5), (D5, 1), (77, .5), (81, 1), (79, .5), (77, .5), (E5, 1.5), (C5, .5), (E5, 1), (D5, .5), (C5, .5),
              (B4, 1), (B4, .5), (C5, .5), (D5, 1), (E5, 1), (C5, 1), (A4, 1), (A4, 1), (R, 1)]),
]

if __name__ == '__main__':
    out = HERE / 'out' / 'music'; out.mkdir(parents=True, exist_ok=True)
    idx = []
    for tu in TUNES:
        x = arrange(tu)
        wav(out / f"{tu['id']}.wav", x)
        idx.append({k: tu[k] for k in ('id', 'title', 'origin', 'clip')} | {'file': f"out/music/{tu['id']}.wav", 'secs': round(len(x) / SR, 1)})
        print(tu['id'], round(len(x) / SR, 1), 's')
    (out / 'index.json').write_text(json.dumps(idx, indent=1, ensure_ascii=False))
