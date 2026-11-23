"""Baketris's sound on the Deck: synthesized once, cached, played from memory.

Synthesis (sfx.py, music.py) is pure Python and takes seconds a sound set, tens
of seconds a song, so it never runs in the app: a job queue (one thread, which
only waits) starts `python -m rookmote.games.audio <job>` niced, one job at a
time, and the results land under ~/.cache/rookmote/baketris-audio-N/. The frame
loop only plays Sounds already in memory.

What Android does at play time that SDL's mixer can't (SoundPool's playback
rate, AudioTrack.setPlaybackRate) is done ahead: each sound is cached at the
rates the game plays it at (hard drop and the quad's random pitch in five steps,
a clear's combo pitch at each combo), each song at each playback rate the game
asks for, the first time it asks (and the likely next ones right after).

Cache files: <name>.pcm (int16 little-endian mono, 44.1 kHz; the songs' own
render, songN.pcm, at 24 kHz) and, for packs of many sounds, <name>.json (the
index, written last: no index, no pack)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from array import array
from pathlib import Path

from ..paths import cache_dir

VERSION = 1
OUT_RATE = 44100
ROOT = Path(__file__).resolve().parents[2]       # the folder rookmote/ is in (for -m)

MOVE, ROT, HOLD, LOCK, HARD, C1, C2, C3, C4, KABOOM = range(10)
HARD_RATES = (0.95, 0.975, 1.0, 1.025, 1.05)
KABOOM_RATES = (0.92, 0.945, 0.97, 0.995, 1.02)
CLEAR_RATES = tuple(round(1 + 0.05 * c, 2) for c in range(21))   # 1 + combo * 0.05, to SoundPool's 2.0


def rates_for(k: int) -> tuple:
    if k == HARD:
        return HARD_RATES
    if k == KABOOM:
        return KABOOM_RATES
    if C1 <= k <= C4:
        return CLEAR_RATES
    if k == ROT:
        return (1.0, 0.8)                      # 0.8: a menu item picked
    return (1.0,)


def store() -> Path:
    d = cache_dir() / f"baketris-audio-{VERSION}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _le(a: array) -> bytes:
    if sys.byteorder != "little":
        a = array("h", a)
        a.byteswap()
    return a.tobytes()


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def resample(a: array, step: float) -> array:
    """Linear interpolation, `step` source samples per output sample (a playback rate)."""
    n = len(a)
    if n < 2:
        return array("h", a)
    m = int((n - 1) / step) + 1
    out = array("h", bytes(2 * m))
    for k in range(m):
        p = k * step
        i = int(p)
        f = p - i
        x = a[i]
        out[k] = int(x + (a[i + 1] - x) * f) if i + 1 < n else x
    return out


def write_pack(name: str, sounds: dict) -> None:
    d = store()
    index, chunks, off = {}, [], 0
    for key, a in sounds.items():
        index[key] = [off, len(a)]
        chunks.append(_le(a))
        off += len(a)
    _write(d / f"{name}.pcm", b"".join(chunks))
    _write(d / f"{name}.json", json.dumps(index).encode())


def pack_ready(name: str) -> bool:
    return (store() / f"{name}.json").exists()


HEAT = 10000        # a variant number: the playback rate in thousandths + HEAT * the energy step (0-3)


def song_rate_name(i: int, v: int) -> str:
    heat, milli = divmod(v, HEAT)
    return f"song{i}_r{milli}" + (f"_h{heat}" if heat else "")


# -- the jobs (in the child process) ----------------------------------------------------

def job_sfx(skin: int) -> None:
    from . import sfx
    w = sfx.build(skin)
    out = {}
    for k, a in enumerate(w):
        for r in rates_for(k):
            out[f"{k}@{r}"] = a if r == 1.0 else resample(a, r)
    write_pack(f"sfx{skin}", out)


def job_shared() -> None:
    from . import sfx
    write_pack("shared", sfx.shared())


def _song_source(i: int) -> array:
    from . import music
    p = store() / f"song{i}.pcm"
    if p.exists():
        a = array("h")
        a.frombytes(p.read_bytes())
        if sys.byteorder != "little":
            a.byteswap()
        return a
    a = music.render(music.SONGS[i])
    _write(p, _le(a))
    return a


def heat_up(src: array, song: int, heat: int) -> array:
    """The energy steps' music: 1 more drive, 2 and sixteenth-note hats, 3 and a kick on
    every beat with more drive still. On the 24 kHz render, before the rate."""
    import math
    import random
    from . import music
    if heat <= 0:
        return src
    rate = music.RATE
    e8 = int(math.floor(rate * 60.0 / music.SONGS[song].bpm / 2 + 0.5))
    x = [v / 32767.0 for v in src]
    n = len(x)
    if heat >= 2:                                           # hats: short high noise, every sixteenth
        r = random.Random(song)
        hat = [(r.random() * 2 - 1) * math.exp(-k / (rate * 0.006)) for k in range(int(rate * 0.025))]
        lp = 0.0
        for k in range(len(hat)):                           # a high-pass: only the hiss
            lp += 0.3 * (hat[k] - lp)
            hat[k] -= lp
        amp = 0.10 if heat == 2 else 0.13
        for t in range(0, n, e8 // 2):
            acc = amp if (t // (e8 // 2)) % 2 else amp * 0.6
            for k, h in enumerate(hat):
                if t + k >= n:
                    break
                x[t + k] += h * acc
    if heat >= 3:                                           # a kick on every beat
        kick = []
        ph = 0.0
        for k in range(int(rate * 0.13)):
            f = 45 + 70 * math.exp(-k / (rate * 0.025))
            ph += f / rate
            kick.append(math.sin(2 * math.pi * ph) * math.exp(-k / (rate * 0.05)) * 0.38)
        for t in range(0, n, 2 * e8):
            for k, v in enumerate(kick):
                if t + k >= n:
                    break
                x[t + k] += v
    drive = 1.0 + 0.45 * heat
    norm = math.tanh(drive)
    tanh = math.tanh
    return array("h", (int(tanh(v * drive) / norm * 31000) for v in x))


def job_song(i: int, v: int) -> None:
    from . import music
    heat, milli = divmod(v, HEAT)
    src = heat_up(_song_source(i), i, heat)
    out = resample(src, music.RATE * (milli / 1000) / OUT_RATE)
    _write(store() / f"{song_rate_name(i, v)}.pcm", _le(out))


def run_job(job: tuple) -> None:
    kind = job[0]
    if kind == "sfx":
        job_sfx(int(job[1]))
    elif kind == "shared":
        job_shared()
    elif kind == "song":
        job_song(int(job[1]), int(job[2]))


def job_done(job: tuple) -> bool:
    kind = job[0]
    if kind == "sfx":
        return pack_ready(f"sfx{job[1]}")
    if kind == "shared":
        return pack_ready("shared")
    if kind == "song":
        return (store() / f"{song_rate_name(int(job[1]), int(job[2]))}.pcm").exists()
    return True


def main(argv: list) -> int:
    try:
        os.nice(10)
    except OSError:
        pass
    run_job(tuple(argv))
    return 0


# -- the app's end ----------------------------------------------------------------------

class Jobs:
    """Runs jobs one at a time in a child process; a thread waits on it (no GIL taken)."""

    def __init__(self, on_done=None):
        self.q: list = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.on_done = on_done
        self.busy = None
        self.failed: set = set()
        self.stopped = False
        self.thread = threading.Thread(target=self._run, name="baketris-audio", daemon=True)
        self.thread.start()

    def want(self, job: tuple, first: bool = False) -> None:
        job = tuple(str(x) for x in job)
        with self.lock:
            if job == self.busy or job in self.failed:
                return
            if job in self.q:
                if not first:
                    return
                self.q.remove(job)
            self.q.insert(0, job) if first else self.q.append(job)
        self.wake.set()

    def pending(self) -> int:
        with self.lock:
            return len(self.q) + (1 if self.busy else 0)

    def stop(self) -> None:
        self.stopped = True
        self.wake.set()

    def _run(self) -> None:
        while not self.stopped:
            with self.lock:
                job = self.q.pop(0) if self.q else None
                self.busy = job
            if job is None:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            ok = job_done(job)
            if not ok:
                try:
                    r = subprocess.run([sys.executable, "-m", "rookmote.games.audio", *job], cwd=str(ROOT),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
                    ok = r.returncode == 0 and job_done(job)
                    if not ok:
                        sys.stderr.write(f"baketris audio job {job} failed: {r.stderr.decode()[-400:]}\n")
                except (OSError, subprocess.SubprocessError) as e:
                    sys.stderr.write(f"baketris audio job {job}: {e}\n")
            with self.lock:
                self.busy = None
                if not ok:
                    self.failed.add(job)
            if ok and self.on_done:
                try:
                    self.on_done(job)
                except Exception as e:          # a bad cache file: say so, keep going
                    sys.stderr.write(f"baketris audio load {job}: {e}\n")


def _read_pcm(path: Path) -> bytes:
    data = path.read_bytes()
    if sys.byteorder != "little":
        a = array("h")
        a.frombytes(data)
        a.byteswap()
        data = a.tobytes()
    return data


class Audio:
    """Sfx.java's and Music.java's interface, over pygame.mixer and the cache."""

    def __init__(self, enabled: bool = True):
        self.ok = False
        self.pg = None
        if enabled:
            try:
                import pygame
                if pygame.mixer.get_init() != (OUT_RATE, -16, 1):
                    pygame.mixer.quit()                     # pygame.init() opened it stereo: ours is mono
                    pygame.mixer.init(OUT_RATE, -16, 1, 512)
                freq, size, ch = pygame.mixer.get_init()
                if (freq, size, ch) == (OUT_RATE, -16, 1):
                    pygame.mixer.set_num_channels(16)
                    pygame.mixer.set_reserved(2)               # 0: music, 1: the announcer
                    self.pg = pygame
                    self.ok = True
                else:
                    sys.stderr.write(f"baketris: mixer is {freq}/{size}/{ch}, no sound\n")
            except Exception as e:
                sys.stderr.write(f"baketris: no sound ({e})\n")
        self.sets: dict = {}            # skin -> {key: Sound}
        self.shared: dict = {}
        self.skin = 0
        self.music = Music(self)
        self.jobs = Jobs(self._loaded) if self.ok else None

    # loading (on the jobs thread, after a job; or now, if it was cached)
    def _loaded(self, job: tuple) -> None:
        if job[0] == "sfx":
            self.sets[int(job[1])] = self._pack(f"sfx{job[1]}")
        elif job[0] == "shared":
            self.shared = self._pack("shared")
        elif job[0] == "song":
            self.music._loaded(int(job[1]), int(job[2]))

    def _pack(self, name: str) -> dict:
        d = store()
        index = json.loads((d / f"{name}.json").read_text())
        data = _read_pcm(d / f"{name}.pcm")
        snd = self.pg.mixer.Sound
        return {k: snd(buffer=data[o * 2:(o + n) * 2]) for k, (o, n) in index.items()}

    def warm(self, skin: int, song: int) -> None:
        """What a game needs first: this skin's set, the shared sounds, the song at rate 1; then the rest."""
        if not self.ok:
            return
        self.load_announcer()
        self.use(skin)
        self.jobs.want(("shared",))
        for m in (1000, 1028, 1070):                # the start, the first level up, danger
            self.music.want(song, m)
        for i in range(4):
            self.music.want(i, 1000)
        for s in range(7):
            if s != skin:
                self.jobs.want(("sfx", s))

    def use(self, skin: int) -> None:
        self.skin = skin
        if self.ok and skin not in self.sets:
            self.jobs.want(("sfx", skin), first=True)

    def ready(self) -> bool:
        return not self.ok or (self.skin in self.sets and bool(self.shared))

    def play(self, k: int, vol: float, rate: float = 1.0) -> None:
        """A sound of the skin's set (Sfx.play(sfx.id(k), vol, rate)): the nearest cached rate."""
        s = self.sets.get(self.skin)
        if not s:
            return
        rate = max(0.5, min(2.0, rate))
        best = min(rates_for(k), key=lambda r: abs(r - rate))
        self._play(s.get(f"{k}@{best}"), vol)

    # the announcer: committed clips (rookmote/games/announcer, made by tools/make_announcer.py)
    def load_announcer(self) -> None:
        """Loads the clips on a thread (small OGGs; decoding still takes a few ms each)."""
        if not self.ok or getattr(self, "voice", None) is not None:
            return
        self.voice, self.voice_q, self.voice_vol = {}, [], 1.0

        def run():
            d = Path(__file__).resolve().parent / "announcer"
            for f in sorted(d.glob("*.ogg")):
                try:
                    self.voice[f.stem] = self.pg.mixer.Sound(str(f))
                except Exception as e:
                    sys.stderr.write(f"baketris announcer {f.name}: {e}\n")
        threading.Thread(target=run, name="baketris-voice", daemon=True).start()

    def announce(self, names: list, vol: float) -> None:
        """Say these, one after the other (a new call cuts what was queued, not what is being said)."""
        if not self.ok or getattr(self, "voice", None) is None:
            return
        self.voice_vol = vol
        self.voice_q = [n for n in names if n][:3]

    def tick_announcer(self) -> None:
        if not self.ok or not getattr(self, "voice_q", None):
            return
        ch = self.pg.mixer.Channel(1)
        if ch.get_busy():
            return
        snd = self.voice.get(self.voice_q.pop(0))
        if snd is not None:
            ch.set_volume(self.voice_vol)
            ch.play(snd)

    def play_shared(self, name: str, vol: float) -> None:
        self._play(self.shared.get(name), vol)

    def _play(self, snd, vol: float) -> None:
        if snd is None:
            return
        ch = self.pg.mixer.find_channel(True)
        if ch is not None:
            ch.set_volume(min(1.0, vol))
            ch.play(snd)

    def close(self) -> None:
        self.music.pause()
        if self.jobs:
            self.jobs.stop()


class Music:
    """Music.java's controls. Plays on the mixer's reserved channel 0, a song's variant at
    the asked rate once it is cached; cycle mode queues the next song at the loop's end."""

    CACHE = 6

    def __init__(self, audio: Audio):
        self.a = audio
        self.variants: dict = {}        # (song, milli) -> (bytes, Sound)
        self.order: list = []
        self.lock = threading.Lock()
        self.want_song = 0
        self.milli = 1000
        self.vol = 0.55
        self.cycle = False
        self.playing = False
        self.restart_req = True
        self.now = -1
        self.cur = None                 # (song, milli) playing
        self.off = 0                    # sample the current Sound began at, in its variant
        self.t0 = 0.0                   # when it began
        self.paused_pos = None
        self.queued = None              # (song, milli) queued after the current

    def _ch(self):
        return self.a.pg.mixer.Channel(0) if self.a.ok else None

    def want(self, song: int, milli: int, first: bool = False) -> None:
        """Cached and loaded, on the jobs thread (a cached one is only read: no job runs)."""
        if self.a.ok and self._get(song, milli) is None:
            self.a.jobs.want(("song", song, milli), first=first)

    def _loaded(self, song: int, milli: int) -> None:
        data = _read_pcm(store() / f"{song_rate_name(song, milli)}.pcm")
        snd = self.a.pg.mixer.Sound(buffer=data)
        with self.lock:
            self.variants[(song, milli)] = (data, snd)
            if (song, milli) in self.order:
                self.order.remove((song, milli))
            self.order.append((song, milli))
            while len(self.order) > self.CACHE:
                old = self.order.pop(0)
                if old not in (self.cur, self.queued, (self.want_song, self.milli)):
                    self.variants.pop(old, None)
                else:
                    self.order.append(old)
                    break

    def _get(self, song: int, milli: int):
        with self.lock:
            return self.variants.get((song, milli))

    # Music.java's interface
    def set_cycle(self, c: bool) -> None:
        self.cycle = c

    def current(self) -> int:
        return self.now

    def select(self, song: int) -> None:
        song = max(0, min(3, song))
        if song != self.want_song:
            self.want_song = song
            self.restart_req = True
        self.want(song, self.milli, first=True)

    def play(self) -> None:
        self.playing = True
        ch = self._ch()
        if ch is not None and self.paused_pos is not None and self.cur is not None:
            ch.unpause()
            self.t0 = time.monotonic() - (self.paused_pos - self.off) / OUT_RATE
            self.paused_pos = None

    def pause(self) -> None:
        if self.playing and self.cur is not None and self.paused_pos is None:
            self.paused_pos = self._pos()
        self.playing = False
        ch = self._ch()
        if ch is not None:
            ch.pause()

    def restart(self) -> None:
        self.pause()
        self.restart_req = True

    def set_rate(self, r: float, heat: int = 0) -> None:
        """The playback rate, and the energy step's arrangement (heat 0-3; see heat_up)."""
        milli = int(round(r * 1000))
        self.milli = milli + HEAT * heat
        self.want(self.want_song, self.milli, first=True)
        if self.a.ok:                               # the likely next ones: a level up, danger on/off, a step more
            for m, h in ((milli + 28, heat), (milli + 70, heat), (milli - 70, heat), (milli + 35, heat + 1),
                         (milli - 35, heat - 1)):
                if 1000 <= m <= 1600 and 0 <= h <= 3:
                    self.want(self.want_song, m + HEAT * h)

    def set_volume(self, v: float) -> None:
        self.vol = v
        ch = self._ch()
        if ch is not None:
            ch.set_volume(v)

    def _pos(self) -> int:
        return self.off + int((time.monotonic() - self.t0) * OUT_RATE)

    def _start(self, key, at: int, ch) -> None:
        data, snd = self._get(*key)
        n = len(data) // 2
        at = max(0, min(n - 1, at))
        s = snd if at == 0 else self.a.pg.mixer.Sound(buffer=data[at * 2:])
        ch.stop()
        ch.set_volume(self.vol)
        ch.play(s)
        self.cur, self.off, self.t0, self.paused_pos = key, at, time.monotonic(), None
        self.now = key[0]
        self.queued = None
        self._queue_next(ch)

    def _queue_next(self, ch) -> None:
        song = (self.cur[0] + 1) % 4 if self.cycle else self.cur[0]
        nxt = (song, self.cur[1])
        v = self._get(*nxt)
        if v is None:
            self.want(*nxt)
            return
        ch.queue(v[1])
        self.queued = nxt

    def tick(self) -> None:
        """Each frame: start what is asked once it is cached, follow the loop, change rate."""
        ch = self._ch()
        if ch is None:
            return
        if self.restart_req:
            if self.cur is not None:
                ch.stop()
                self.cur = self.queued = None
                self.paused_pos = None
            if not self.playing:
                return
        if not self.playing:
            return
        want = (self.want_song, self.milli)
        if self.cur is None or self.restart_req:
            if self._get(*want):
                self.restart_req = False
                self._start(want, 0, ch)
            return
        if self.queued is not None and ch.get_queue() is None and ch.get_busy():
            self.cur, self.off, self.t0 = self.queued, 0, time.monotonic()      # the queued one began
            self.now = self.cur[0]
            self.queued = None
            if self.cycle:
                self.want_song = self.cur[0]
            self._queue_next(ch)
        elif self.queued is None:
            if not ch.get_busy():                   # ended with nothing queued (the next wasn't cached)
                nxt = ((self.cur[0] + 1) % 4 if self.cycle else self.cur[0], self.cur[1])
                if self._get(*nxt):
                    if self.cycle:
                        self.want_song = nxt[0]
                    self._start(nxt, 0, ch)
                return
            self._queue_next(ch)
        if self.cur[1] != self.milli and self.cur[0] == self.want_song and self._get(*want):
            old = self._get(*self.cur)
            frac = self._pos() / max(1, len(old[0]) // 2) if old else 0.0
            n = len(self._get(*want)[0]) // 2
            self._start(want, int(frac * n), ch)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
