"""Baketris's energy (games/energy.py) and what it drives: the curve (rises fast with
the pace and with clears, falls back slowly once it eases off, four steps), the
music's hotter arrangement per step (audio.heat_up, a variant number), and the
game's use of it (the meter, the setting, shake, music rate).

Run (from deck-remote/): /tmp/deck/venv/bin/python -m unittest tests.test_energy"""

import os
import sys
import tempfile
import unittest
from array import array
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pygame  # noqa: E402

pygame.init()
pygame.display.set_mode((1280, 800))

from rookmote.games import audio as A  # noqa: E402
from rookmote.games import baketris as B  # noqa: E402
from rookmote.games import energy as E  # noqa: E402
from rookmote.games import rules as G  # noqa: E402


def drive(en, pps, seconds, aps=0.0, frame=1000 / 60):
    """pps pieces and aps other inputs a second, spread over frames, for seconds."""
    t, owe, owa = 0.0, 0.0, 0.0
    while t < seconds * 1000:
        owe += pps * frame / 1000
        owa += aps * frame / 1000
        while owe >= 1:
            en.piece()
            owe -= 1
        while owa >= 1:
            en.act()
            owa -= 1
        en.update(frame)
        t += frame
    return en


class Curve(unittest.TestCase):
    def test_idle_and_calm_play_stay_calm(self):
        self.assertEqual(drive(E.Energy(), 0, 5).step, 0)
        self.assertEqual(drive(E.Energy(), 0.6, 10, aps=2).step, 0, "a relaxed game: a piece every 1.7 s")

    def test_pieces_a_second_set_the_step(self):
        steps = [drive(E.Energy(), pps, 10, aps=pps * 3).step for pps in (0.6, 1.0, 1.5, 2.0)]
        self.assertEqual(steps, [0, 1, 2, 3])

    def test_three_quick_drops_charge_it(self):
        en = E.Energy()
        for k in range(3):
            en.piece()
            for _ in range(20):                       # a drop every third of a second
                en.update(1000 / 60)
        self.assertGreaterEqual(en.step, 1)

    def test_moves_alone_add_little(self):
        self.assertLessEqual(drive(E.Energy(), 0, 6, aps=6).step, 0)
        self.assertGreater(drive(E.Energy(), 1.0, 8, aps=8).e, drive(E.Energy(), 1.0, 8, aps=1).e)

    def test_falls_back_slowly(self):
        en = drive(E.Energy(), 2.5, 5)
        self.assertEqual(en.step, 3)
        drive(en, 0, 1.0)
        self.assertGreaterEqual(en.step, 2, "a breath doesn't drop it")
        drive(en, 0, 7)
        self.assertEqual(en.step, 0)

    def test_clears_lift_it(self):
        en = E.Energy()
        en.clear(4, 0, True, 3, False)
        self.assertAlmostEqual(en.e, 0.22 + 0.06 + 0.075)
        self.assertEqual(en.step, 1)
        en.clear(2, 2, True, 6, True)
        self.assertGreater(en.e, 0.95)

    def test_steps_and_names(self):
        en = E.Energy()
        for e, want in ((0.0, 0), (0.24, 0), (0.25, 1), (0.5, 2), (0.74, 2), (0.75, 3), (1.0, 3)):
            en.e = e
            self.assertEqual(en.step, want)
        self.assertEqual(len(E.NAMES), 4)


class HotMusic(unittest.TestCase):
    def test_heat_variants(self):
        src = array("h", [0] * 24000)
        self.assertIs(A.heat_up(src, 0, 0), src)
        hot = [A.heat_up(src, 0, h) for h in (1, 2, 3)]
        self.assertTrue(all(len(x) == len(src) for x in hot))
        self.assertEqual(max(hot[0]), 0, "drive alone adds nothing to silence")
        self.assertGreater(max(hot[1]), 1000, "step 2 adds the hats")
        e = sum(abs(v) for v in hot[2])
        self.assertGreater(e, sum(abs(v) for v in hot[1]), "step 3 adds the kick")

    def test_variant_numbers(self):
        self.assertEqual(A.song_rate_name(1, 1028), "song1_r1028")
        self.assertEqual(A.song_rate_name(1, 1028 + 2 * A.HEAT), "song1_r1028_h2")


class InTheGame(unittest.TestCase):
    def play(self, on=True):
        bk = B.Baketris(prefs_path=Path(tempfile.mkdtemp()) / "p.json", seed=1)
        bk.energy_on = on
        rates = []
        bk.music.set_rate = lambda r, heat=0: rates.append((round(r, 3), heat))
        bk.start_game(G.MARATHON)
        while bk.state != B.S_PLAY:
            bk.update(16.667)
        return bk, rates

    def mash(self, bk, frames, drop_every=24):
        """Moves, and a hard drop every drop_every frames (24: 2.5 pieces a second)."""
        for i in range(frames):
            if i % 4 == 0:
                bk.press(B.A_LEFT if (i // 4) % 2 else B.A_RIGHT)
                bk.release(B.A_LEFT)
                bk.release(B.A_RIGHT)
            if i % drop_every == drop_every - 1:
                bk.last_drop_t = 0
                bk.press(B.A_HARD)
                for r in range(G.H):                    # (keep the well empty: no topping out, no restart)
                    bk.game.grid[r] = [0] * G.W
            bk.update(16.667)

    def test_busy_play_heats_the_music_and_shake(self):
        bk, rates = self.play()
        self.mash(bk, 300)                              # 2.5 pieces a second
        self.assertEqual(bk.heat, 3)
        self.assertEqual(rates[-1][1], 3)
        self.assertAlmostEqual(rates[-1][0], 1 + 0.035 * 3, places=3)
        bk.trauma = 0
        bk.add_trauma(0.2)
        self.assertAlmostEqual(bk.trauma, 0.2 * (1 + 0.9), places=5)
        bk.draw(pygame.display.get_surface())
        for _ in range(500):
            bk.update(16.667)
        self.assertEqual(bk.heat, 0, "eased off: back to calm")
        self.assertEqual(rates[-1][1], 0)

    def test_first_drops_step_up_at_once(self):
        bk, rates = self.play()
        for k in range(4):
            self.mash(bk, 20, drop_every=20)
            if bk.heat:
                break
        self.assertGreaterEqual(bk.heat, 1, "a few quick drops")
        self.assertGreater(bk.heat_toast_t, 0, "the banner shows at once")
        self.assertEqual(rates[-1][1], bk.heat, "the music is asked for the step at once")

    def test_a_step_up_is_heard_while_its_music_is_made(self):
        bk, rates = self.play()
        played = []
        bk.audio.play = lambda k, vol, rate=1.0: played.append(k)
        bk.music.playing_wanted = lambda: False          # the hotter version isn't cached yet
        bk.energy.e = 0.8
        for _ in range(120):                             # two seconds
            bk.energy.e = max(bk.energy.e, 0.8)
            bk.update(16.667)
        self.assertEqual(bk.heat, 3)
        self.assertGreaterEqual(len(played), 6, "a pulse of the skin's sounds keeps the beat")
        played.clear()
        bk.music.playing_wanted = lambda: True           # and stops once the real thing plays
        for _ in range(60):
            bk.update(16.667)
        self.assertEqual(played, [])

    def test_off_keeps_it_calm(self):
        bk, rates = self.play(on=False)
        self.mash(bk, 300)
        self.assertEqual(bk.heat, 0)
        self.assertTrue(all(h == 0 for _, h in rates))

    def test_setting_persists(self):
        bk, _ = self.play()
        bk.state, bk.sel = B.S_SETTINGS, B.SETTINGS_ROWS.index("ENERGY")
        bk.pad_down("a")
        self.assertFalse(bk.energy_on)
        self.assertFalse(B.Baketris(prefs_path=bk.prefs_path).energy_on)


if __name__ == "__main__":
    unittest.main()
