"""The remote's small games on the Deck (games/g2048.py, towers.py, swarm.py)
against the firmware's own code: each firmware-remote/src/<game>.cpp is
compiled on the host (g++, stub headers in tests/fwhost: millis() and
esp_random() are the harness's), driven by the same random button script and
the same random numbers as the Python port, and its state printed after every
tick; the port must print the same. Skipped without g++.

Run (from deck-remote/): /tmp/deck/venv/bin/python -m unittest tests.test_minigames"""

import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
HERE = Path(__file__).resolve().parent
FW = HERE.parents[1] / "firmware-remote" / "src"
sys.path.insert(0, str(HERE.parent))

import pygame  # noqa: E402

from rookmote.games import g2048  # noqa: E402

HARNESS = r'''
#include "%(src)s"
#include <iostream>
uint32_t host_ms = 1, host_rng = %(seed)uu;
int main() {
    %(ns)s::start(host_ms);
    uint32_t ms; unsigned keys;
    while (std::cin >> ms >> keys) {
        host_ms = ms;
        bool ch = %(ns)s::tick(ms, (uint16_t)keys);
        std::cout << ch << ' ' << %(ns)s::quit() << ' ' << %(ns)s::score();
        %(dump)s
        std::cout << '\n';
    }
}
'''


class Lcg:
    def __init__(self, seed):
        self.s = seed

    def __call__(self):
        self.s = (self.s * 1664525 + 1013904223) & 0xFFFFFFFF
        return self.s


def run_firmware(cpp: str, ns: str, dump: str, seed: int, script) -> list[str]:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "h.cpp").write_text(HARNESS % {"src": str(FW / cpp), "ns": ns, "dump": dump, "seed": seed})
    exe = tmp / "h"
    subprocess.run(["g++", "-std=c++17", "-O1", "-w", f"-I{HERE / 'fwhost'}", f"-I{FW}", "-o", str(exe),
                    str(tmp / "h.cpp")], check=True)
    out = subprocess.run([str(exe)], input="".join(f"{ms} {k}\n" for ms, k in script), capture_output=True,
                         text=True, check=True).stdout
    return out.splitlines()


def script(seed: int, n: int, bits: list[int], step=(16, 17, 33, 200)):
    rnd = random.Random(seed)
    ms, out, keys = 1, [], 0
    for _ in range(n):
        ms += rnd.choice(step)
        r = rnd.random()
        if r < 0.45:
            keys = rnd.choice(bits)
        elif r < 0.8:
            keys = 0
        elif r < 0.9:
            keys |= rnd.choice(bits)
        out.append((ms, keys))
    return out


@unittest.skipUnless(shutil.which("g++") and FW.is_dir(), "needs g++ and firmware-remote/src")
class Against2048(unittest.TestCase):
    DUMP = r'''std::cout << " |"; for (int i = 0; i < 16; i++) std::cout << ' ' << (int)g[i];
        std::cout << " | " << over << won << ' ' << pop_at << ' ' << pop_until << ' ' << won_until;'''

    def py(self, seed, sc):
        r = g2048.Rules(Lcg(seed))
        r.start(1)
        out = []
        for ms, k in sc:
            ch = r.tick(ms, k)
            out.append(f"{int(ch)} {int(r.quit())} {r.score()} | " + " ".join(str(v) for v in r.g)
                       + f" | {int(r.over)}{int(r.won)} {r.pop_at} {r.pop_until} {r.won_until}")
        return out

    def test_same_as_firmware(self):
        bits = [1, 2, 4, 8, 16, 0]
        for seed in (1, 7, 12345, 99):
            sc = script(seed, 3000, bits)
            want = run_firmware("g2048.cpp", "g2048", self.DUMP, seed, sc)
            got = self.py(seed, sc)
            for i, (a, b) in enumerate(zip(want, got)):
                self.assertEqual(a, b, f"seed {seed}, tick {i}")
            self.assertEqual(len(want), len(got))

    def test_quit_and_restart(self):
        sc = [(10, 0), (20, 64)]
        want = run_firmware("g2048.cpp", "g2048", self.DUMP, 3, sc)
        self.assertEqual(want, self.py(3, sc))

    def test_stuck_is_over_and_a_restarts(self):
        r = g2048.Rules(Lcg(5))
        r.start(1)
        r.g = [1, 2, 1, 2, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 3]
        self.assertTrue(r.stuck())
        r.over = True
        r.tick(50, 1)
        self.assertTrue(r.over)                  # moves do nothing
        r.tick(70, 16)
        self.assertFalse(r.over)
        self.assertEqual((r.points, 16 - r.g.count(0)), (0, 2))


class Screen2048(unittest.TestCase):
    def test_play_draw_best_and_leave(self):
        pygame.init()
        s = pygame.display.set_mode((1280, 800))
        from rookmote.games import mini
        bests = mini.Bests(Path(tempfile.mkdtemp()) / "games.json")
        g = g2048.Game2048(bests=bests, rand=Lcg(4))
        for i in range(400):
            g.pad_down(["dpleft", "dpup", "dpright", "dpdown"][i % 4])
            g.update(16)
            g.pad_up(["dpleft", "dpup", "dpright", "dpdown"][i % 4])
            g.update(16)
        g.draw(s)
        self.assertGreater(g.g.points, 0)
        self.assertEqual(bests.get("2048"), max(g.best, g.g.points))
        g.touch_down(1, 500, 400)
        g.touch_up(1, 300, 405)                  # a swipe left
        g.update(16)
        g.pad_down("b")
        g.update(16)
        self.assertTrue(g.done)


if __name__ == "__main__":
    unittest.main()
