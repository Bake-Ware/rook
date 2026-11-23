"""The BAKETRIS mode (the Deck's own): a 4-line clear blows the stack up (games/blast.py)
and it lands as a new stack. Block count and colours are kept, everything ends on a
cell in the well, a seed gives the same landing, landing clears score and can chain
(at most CHAIN_MAX blasts), and the falling piece waits until it has all settled.

Run (from deck-remote/): /tmp/deck/venv/bin/python -m unittest tests.test_baketris_mode"""

import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pygame  # noqa: E402

pygame.init()
pygame.display.set_mode((1280, 800))

from rookmote.games import baketris as B  # noqa: E402
from rookmote.games import blast  # noqa: E402
from rookmote.games import rules as G  # noqa: E402


def board(seed, rows=16, fill=0.8):
    g = [[0] * G.W for _ in range(G.H)]
    rng = random.Random(seed)
    for r in range(rows):
        for c in range(G.W):
            if rng.random() < fill:
                g[r][c] = 1 + rng.randrange(7)
    return g


def colours(g):
    return sorted(v for row in g for v in row if v)


def settle(b):
    n = 0
    while not b.done:
        b.step()
        n += 1
        assert n < 10000
    return n


class Physics(unittest.TestCase):
    def test_conserves_blocks_and_colours_and_lands_on_cells(self):
        for seed in range(6):
            g = board(seed)
            want = colours(g)
            b = blast.Blast(g, [0, 1, 2, 3], seed)
            self.assertEqual(b.count, len(want))
            self.assertEqual(colours(b.grid), [], "everything is in the air")
            settle(b)
            self.assertEqual(colours(b.grid), want)
            self.assertEqual(len(b.grid), G.H)
            self.assertTrue(all(len(r) == G.W for r in b.grid))
            self.assertLessEqual(b.steps, blast.MAX_STEPS)

    def test_deterministic(self):
        a, b = blast.Blast(board(3), [0, 1, 2, 3], ("s", 1)), blast.Blast(board(3), [0, 1, 2, 3], ("s", 1))
        settle(a)
        settle(b)
        self.assertEqual(a.grid, b.grid)
        c = blast.Blast(board(3), [0, 1, 2, 3], ("s", 2))
        settle(c)
        self.assertNotEqual(a.grid, c.grid)

    def test_lands_in_a_pile_not_in_the_air(self):
        b = blast.Blast(board(8, rows=6, fill=0.7), [2], 1)
        settle(b)
        hanging = sum(1 for r in range(1, G.H) for c in range(G.W) if b.grid[r][c] and not b.grid[r - 1][c])
        self.assertLessEqual(hanging, 4, "a few may perch on a neighbour, not float")

    def test_nearly_full_well_still_fits(self):
        b = blast.Blast(board(9, rows=G.H - 1, fill=1.0), [0, 1, 2, 3], 3)
        want = colours(board(9, rows=G.H - 1, fill=1.0))
        settle(b)
        self.assertEqual(colours(b.grid), want)


class SettleClear(unittest.TestCase):
    def test_scores_like_a_lock(self):
        g = G.Game()
        g.reset(G.BAKETRIS, 1)
        g.next_piece()
        g.grid = [[1] * G.W, [2] * G.W, [0, 3] + [0] * 8] + [[0] * G.W for _ in range(G.H - 3)]
        heard = []

        class L(G.Listener):
            def on_lock(self, *a):
                heard.append(a)
        g.listener = L()
        self.assertEqual(blast.settle_clear(g), 2)
        self.assertEqual((g.lines, g.score, g.combo), (2, 300, 0))
        self.assertEqual(g.grid[0], [0, 3] + [0] * 8)
        self.assertEqual(heard[0][0], 2)

    def test_baketris_mode_levels_like_marathon_without_its_end(self):
        g = G.Game()
        g.reset(G.BAKETRIS, 1)
        g.lines = 149
        g.grid[0] = [1] * G.W
        blast.settle_clear(g)
        self.assertEqual((g.level, g.over), (16, False))


class InTheGame(unittest.TestCase):
    def play(self):
        bk = B.Baketris(prefs_path=Path(tempfile.mkdtemp()) / "p.json", seed=1)
        bk.start_game(G.BAKETRIS)
        while bk.state != B.S_PLAY:
            bk.update(16.667)
        return bk

    def quad(self, bk, above):
        g = bk.game
        for r in range(4):
            g.grid[r] = [1 + (c % 7) for c in range(9)] + [0]
        for r, row in enumerate(above):
            g.grid[4 + r] = list(row)
        g.cur, g.rot, g.px, g.py = G.I, 1, 7, 18
        g.hard_drop()

    def test_piece_waits_for_the_landing(self):
        bk = self.play()
        rng = random.Random(2)
        self.quad(bk, [[1 + rng.randrange(7) if rng.random() < 0.6 and c < 9 else 0 for c in range(G.W)]
                       for _ in range(6)])
        self.assertIsNotNone(bk.blast_pending)
        saw_blast = False
        for _ in range(2000):
            bk.update(16.667)
            if bk.blast is not None:
                saw_blast = True
                self.assertEqual(bk.game.cur, -1, "no piece while it flies")
                bk.press(B.A_HARD)
                bk.press(B.A_HOLD)
                self.assertEqual(bk.game.hold, -1)
            if saw_blast and bk.blast is None and bk.blast_pending is None:
                break
        self.assertTrue(saw_blast)
        self.assertGreaterEqual(bk.game.cur, 0, "the piece is back")
        self.assertIsNone(bk.stash)

    def test_chain_and_its_cap(self):
        bk = self.play()
        self.quad(bk, [[1 + (c % 7) for c in range(9)] + [0] for _ in range(14)])     # 126 blocks above
        blasts = 0
        for _ in range(6000):
            had = bk.blast is not None
            bk.update(16.667)
            if bk.blast is not None and not had:
                blasts += 1
            if blasts and bk.blast is None and bk.blast_pending is None:
                break
        self.assertGreaterEqual(blasts, 2, "the landing filled 4+ rows: it blew again")
        self.assertLessEqual(blasts, B.CHAIN_MAX)
        self.assertEqual(bk.chain, 0)

    def test_other_modes_never_blast(self):
        bk = self.play()
        bk.start_game(G.MARATHON)
        while bk.state != B.S_PLAY:
            bk.update(16.667)
        self.quad(bk, [[1] * 9 + [0]])
        self.assertIsNone(bk.blast_pending)

    def test_menu_has_the_mode_and_best(self):
        bk = B.Baketris(prefs_path=Path(tempfile.mkdtemp()) / "p.json", seed=1)
        bk.draw(pygame.display.get_surface())
        self.assertEqual(len(bk.menu_rects), 6)
        bk.sel = 3
        bk.pad_down("a")
        self.assertEqual(bk.mode, G.BAKETRIS)
        bk.game.score = 777
        bk.on_end(False)
        self.assertEqual(bk.best[3], 777)


if __name__ == "__main__":
    unittest.main()
