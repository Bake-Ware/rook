"""Baketris's sound: the Python synthesis against Sfx.java's and Music.java's own
output (tests/fixtures/baketris_audio.json.gz, made by make_baketris_audio_fixtures.py
from Baketris's Java), sample for sample; the rate variants; the cache's packs; and
the music player's loop, cycle and rate changes on SDL's dummy audio driver.

Run (from deck-remote/): /tmp/deck/venv/bin/python -m unittest tests.test_baketris_audio
(the songs take ~20 s to render in pure Python)."""

import gzip
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from array import array
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from rookmote.games import audio as A  # noqa: E402
from rookmote.games import music, sfx  # noqa: E402

FX = json.loads(gzip.open(HERE / "fixtures" / "baketris_audio.json.gz").read())


def digest(a: array) -> str:
    b = array("h", a)
    if sys.byteorder != "little":
        b.byteswap()
    return hashlib.sha256(b.tobytes()).hexdigest()


class Synthesis(unittest.TestCase):
    def check(self, name, a):
        want = FX["sounds"][name]
        self.assertEqual(len(a), want["n"], name)
        self.assertEqual(list(a[::FX["step"]]), want["every"], name)
        self.assertEqual(digest(a), want["sha256"], name)

    def test_shared_sounds(self):
        for name, a in sfx.shared().items():
            self.check(name, a)

    def test_every_skin_set(self):
        for s in range(sfx.SETS):
            for k, a in enumerate(sfx.build(s)):
                self.check(f"s{s}_{k}", a)

    def test_songs(self):
        for i, song in enumerate(music.SONGS):
            self.check(f"song{i}", music.render(song))


class Variants(unittest.TestCase):
    def test_resample(self):
        a = array("h", [0, 100, 200, 300, 400])
        self.assertEqual(list(A.resample(a, 1.0)), [0, 100, 200, 300, 400])
        self.assertEqual(list(A.resample(a, 2.0)), [0, 200, 400])
        self.assertEqual(list(A.resample(a, 0.5)), [0, 50, 100, 150, 200, 250, 300, 350, 400])

    def test_rates_cover_what_the_game_plays(self):
        self.assertIn(1.0, A.rates_for(A.MOVE))
        self.assertEqual(A.rates_for(A.C1)[0], 1.0)
        self.assertEqual(A.rates_for(A.C4)[-1], 2.0)       # combo 20 and up: SoundPool's ceiling
        self.assertIn(0.8, A.rates_for(A.ROT))
        self.assertEqual(len(A.rates_for(A.HARD)), 5)


class Player(unittest.TestCase):
    """The mixer on SDL's dummy driver: real time, no device."""

    @classmethod
    def setUpClass(cls):
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp()
        import pygame
        cls.pg = pygame
        pygame.init()
        cls.a = A.Audio()
        if not cls.a.ok:
            raise unittest.SkipTest("no mixer")
        cls.a.jobs.stop()

    def test_pack_round_trip(self):
        A.write_pack("t", {"x": array("h", [1, -2, 3]), "y": array("h", [7] * 10)})
        p = self.a._pack("t")
        self.assertEqual(p["x"].get_raw(), array("h", [1, -2, 3]).tobytes())
        self.assertAlmostEqual(p["y"].get_length(), 10 / 44100, places=8)

    def test_nearest_rate(self):
        snd = {f"{k}@{r}": self.pg.mixer.Sound(buffer=bytes(2 * int(100 * r))) for k in range(10) for r in A.rates_for(k)}
        self.a.sets[0] = snd
        self.a.skin = 0
        played = []
        self.a._play = lambda s, v: played.append(s)
        self.a.play(A.C2, 1.0, 1.0 + 3 * 0.05)
        self.a.play(A.HARD, 1.0, 1.04)
        self.a.play(A.C1, 1.0, 9)
        del self.a._play
        self.assertIs(played[0], snd[f"{A.C2}@1.15"])
        self.assertIs(played[1], snd[f"{A.HARD}@1.05"])
        self.assertIs(played[2], snd[f"{A.C1}@2.0"])

    def _song(self, n_ms):
        data = bytes(2 * int(44100 * n_ms / 1000))
        return data, self.pg.mixer.Sound(buffer=data)

    def _run(self, m, secs, until):
        end = time.monotonic() + secs
        while time.monotonic() < end and not until():
            m.tick()
            time.sleep(0.005)
        return until()

    def test_cycle_moves_to_the_next_song(self):
        m = A.Music(self.a)
        for i in range(4):
            m.variants[(i, 1000)] = self._song(120)
        m.set_cycle(True)
        m.select(2)
        m.restart()
        m.play()
        self.assertTrue(self._run(m, 2, lambda: m.current() == 2))
        self.assertTrue(self._run(m, 2, lambda: m.current() == 3), "cycle: 2 then 3")
        self.assertTrue(self._run(m, 2, lambda: m.current() == 0), "and round to 0")
        m.pause()

    def test_loop_stays_on_its_song(self):
        m = A.Music(self.a)
        for i in range(4):
            m.variants[(i, 1000)] = self._song(100)
        m.select(1)
        m.restart()
        m.play()
        self.assertTrue(self._run(m, 2, lambda: m.current() == 1))
        self._run(m, 0.5, lambda: False)
        self.assertEqual(m.current(), 1)
        self.assertEqual(m.cur, (1, 1000))
        m.pause()

    def test_rate_change_keeps_the_place(self):
        m = A.Music(self.a)
        m.variants[(0, 1000)] = self._song(4000)
        m.select(0)
        m.restart()
        m.play()
        self._run(m, 1, lambda: m.cur is not None)
        time.sleep(0.5)
        m.variants[(0, 1200)] = self._song(4000 / 1.2)
        m.set_rate(1.2)
        frac = m._pos() / (44100 * 4)
        m.tick()
        self.assertEqual(m.cur, (0, 1200))
        self.assertAlmostEqual(m.off / (44100 * 4 / 1.2), frac, delta=0.03)
        m.pause()

    def test_nothing_cached_is_silence_not_a_crash(self):
        m = A.Music(self.a)
        m.select(3)
        m.restart()
        m.play()
        for _ in range(5):
            m.tick()
        self.assertEqual(m.current(), -1)
        m.pause()


if __name__ == "__main__":
    unittest.main()
