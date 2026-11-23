"""Baketris's announcer: the committed clips (rookmote/games/announcer, made by
tools/make_announcer.py) are there, decode, are speech-length and not silent;
the game calls the right ones; the setting and its volume persist.

Run (from deck-remote/): /tmp/deck/venv/bin/python -m unittest tests.test_announcer"""

import os
import sys
import tempfile
import time
import unittest
from array import array
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pygame  # noqa: E402

pygame.init()
pygame.display.set_mode((1280, 800))

from rookmote.games import audio as A  # noqa: E402
from rookmote.games import baketris as B  # noqa: E402
from rookmote.games import rules as G  # noqa: E402

CLIPS = HERE.parent / "rookmote" / "games" / "announcer"
NAMES = {"single", "double", "triple", "baketris", "tspin", "combo", "megacombo", "backtoback", "allclear", "blast"}


class Clips(unittest.TestCase):
    def test_every_clip_is_there_and_sounds(self):
        self.assertEqual({p.stem for p in CLIPS.glob("*.ogg")}, NAMES)
        if not pygame.mixer.get_init():
            pygame.mixer.init(44100, -16, 1)
        for p in CLIPS.glob("*.ogg"):
            self.assertLess(p.stat().st_size, 60_000, p.name)
            snd = pygame.mixer.Sound(str(p))
            self.assertGreater(snd.get_length(), 0.5, p.name)
            self.assertLess(snd.get_length(), 3.5, p.name)
            a = array("h", snd.get_raw())
            peak = max(max(a), -min(a))
            self.assertGreater(peak, 8000, f"{p.name} is not silent")
            self.assertLess(peak, 32767, f"{p.name} does not clip")


class Calls(unittest.TestCase):
    def setUp(self):
        self.bk = B.Baketris(prefs_path=Path(tempfile.mkdtemp()) / "p.json", seed=1)
        self.said = []
        self.bk.audio.announce = lambda names, vol: self.said.append(([n for n in names if n], vol))

    def test_what_each_clear_calls(self):
        bk = self.bk
        for args, want in [((1, 0, False, 0, False), ["single"]), ((2, 0, False, 0, False), ["double"]),
                           ((3, 0, False, 0, False), ["triple"]), ((4, 0, True, 0, False), ["baketris", "backtoback"]),
                           ((2, 2, False, 3, False), ["tspin", "combo"]), ((1, 0, False, 6, False), ["single", "megacombo"]),
                           ((4, 0, False, 0, True), ["allclear"]), ((0, 1, False, -1, False), ["tspin"])]:
            self.said.clear()
            bk.call_lock(*args)
            self.assertEqual(self.said[0][0], want, args)
        self.said.clear()
        bk.call_lock(0, 0, False, -1, False)
        self.assertEqual(self.said, [], "a plain lock says nothing")

    def test_setting_off_and_volume(self):
        bk = self.bk
        bk.ann_vol = 2
        bk.call_lock(1, 0, False, 0, False)
        self.assertEqual(self.said[-1][1], 0.5)
        bk.state, bk.sel = B.S_SETTINGS, B.SETTINGS_ROWS.index("ANNOUNCER")
        bk.pad_down("a")
        self.assertFalse(bk.announcer)
        n = len(self.said)
        bk.call_lock(3, 0, False, 0, False)
        self.assertEqual(len(self.said), n)
        bk2 = B.Baketris(prefs_path=bk.prefs_path)
        self.assertEqual((bk2.announcer, bk2.ann_vol), (False, 2))

    def test_lock_in_play_calls_it(self):
        bk = self.bk
        bk.start_game(G.MARATHON)
        bk.on_lock(4, [0, 1, 2, 3], 0, False, 0, False, True)
        self.assertEqual(self.said[-1][0], ["baketris"])


class Queue(unittest.TestCase):
    def test_plays_in_turn_on_its_channel(self):
        os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp()
        a = A.Audio()
        if not a.ok:
            self.skipTest("no mixer")
        a.jobs.stop()
        a.load_announcer()
        end = time.time() + 5
        while len(a.voice) < len(NAMES) and time.time() < end:
            time.sleep(0.05)
        a.announce(["single", "backtoback", None, "combo", "megacombo"], 0.75)
        self.assertEqual(a.voice_q, ["single", "backtoback", "combo"])
        a.tick_announcer()
        ch = pygame.mixer.Channel(1)
        self.assertTrue(ch.get_busy())
        self.assertAlmostEqual(ch.get_volume(), 0.75, places=2)
        self.assertEqual(a.voice_q, ["backtoback", "combo"])
        a.tick_announcer()
        self.assertEqual(a.voice_q, ["backtoback", "combo"], "waits for the first to finish")


if __name__ == "__main__":
    unittest.main()


class Robust(unittest.TestCase):
    """Bake heard no voice on the Deck: on when an older settings file doesn't say, a TEST
    VOICE row, the music dipping under a call, calls in every mode, a word when clips fail."""

    def test_on_when_an_old_settings_file_does_not_say(self):
        import json
        p = Path(tempfile.mkdtemp()) / "baketris.json"
        p.write_text(json.dumps({"das": 150, "skin": 2, "sound": True}))       # from before the announcer
        bk = B.Baketris(prefs_path=p)
        self.assertTrue(bk.announcer)
        self.assertEqual(bk.ann_vol, 4)
        self.assertTrue(bk.energy_on)

    def test_test_row_speaks_even_when_off(self):
        bk = B.Baketris(prefs_path=Path(tempfile.mkdtemp()) / "p.json")
        said = []
        bk.audio.announce = lambda names, vol: said.append([n for n in names if n])
        bk.announcer = False
        bk.state, bk.sel = B.S_SETTINGS, B.SETTINGS_ROWS.index("TEST VOICE")
        bk.pad_down("a")
        bk.pad_down("a")
        self.assertEqual(said, [list(B.TEST_CALLS[0]), list(B.TEST_CALLS[1])])
        bk.draw(pygame.display.get_surface())

    def test_every_mode_calls(self):
        for mode in (G.MARATHON, G.SPRINT, G.ULTRA, G.BAKETRIS):
            bk = B.Baketris(prefs_path=Path(tempfile.mkdtemp()) / "p.json")
            said = []
            bk.audio.announce = lambda names, vol, said=said: said.append(names)
            bk.start_game(mode)
            bk.on_lock(2, [0, 1], 0, False, 0, False, True)
            self.assertTrue(said, mode)

    def test_music_dips_under_a_call(self):
        bk = B.Baketris(prefs_path=Path(tempfile.mkdtemp()) / "p.json")
        vols = []
        bk.music.set_volume = vols.append
        bk.audio.speaking = lambda: True
        bk.music_level(16)
        bk.audio.speaking = lambda: False
        bk.music_level(16)
        self.assertLess(vols[0], vols[1] * 0.7)

    def test_missing_clips_are_said(self):
        import io
        a = A.Audio()
        if not a.ok:
            self.skipTest("no mixer")
        a.jobs.stop()
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        real = A.Path
        try:
            class P(type(Path())):
                def glob(self, pat):
                    return iter([Path("/nonexistent/broken.ogg")]) if pat == "*.ogg" else super().glob(pat)
            A.Path = P
            a.load_announcer()
            end = time.time() + 3
            while "did not load" not in err.getvalue() and time.time() < end:
                time.sleep(0.02)
        finally:
            A.Path = real
            sys.stderr = old
        self.assertIn("announcer clip broken.ogg did not load", err.getvalue())


class Build(unittest.TestCase):
    def test_build_names_version_and_commit(self):
        import rookmote
        b = rookmote.build()
        self.assertTrue(b.startswith(rookmote.VERSION))
        self.assertEqual((HERE.parent / ".gitattributes").read_text().strip(), "rookmote/BUILD export-subst")
        self.assertIn(b, B.BUILD)
