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

from rookmote.games import g2048, swarm, towers  # noqa: E402

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


@unittest.skipUnless(shutil.which("g++") and FW.is_dir(), "needs g++ and firmware-remote/src")
class AgainstTowers(unittest.TestCase):
    DUMP = r'''std::cout << " | " << gold << ' ' << lives << ' ' << wave << ' ' << to_spawn << ' ' << spawn_wait
            << ' ' << kills << ' ' << cx << ' ' << cy << ' ' << sel << ' ' << over << paused << in_wave << ' '
            << wave_at << ' ' << next_step << " |";
        for (int i = 0; i < CELLS; i++) std::cout << ' ' << (int)cell[i] << ':' << (int)lvl[i] << ':' << (int)cool[i];
        std::cout << " |";
        for (auto& f : foes) if (f.on) std::cout << ' ' << (int)f.kind << ',' << f.hp << ',' << f.x << ',' << f.y << ',' << f.d << ',' << f.slow;
        std::cout << " |";
        for (auto& f : flashes) std::cout << ' ' << (int)f.t << ',' << (int)f.r << ',' << f.x1 << ',' << f.y1;'''

    @staticmethod
    def py(sc):
        r = towers.Rules()
        r.start(1)
        out = []
        for ms, k in sc:
            ch = r.tick(ms, k)
            line = (f"{int(ch)} {int(r.quit())} {r.score()} | {r.gold} {r.lives} {r.wave} {r.to_spawn} {r.spawn_wait} "
                    f"{r.kills} {r.cx} {r.cy} {r.sel} {int(r.over)}{int(r.paused)}{int(r.in_wave)} {r.wave_at} "
                    f"{r.next_step} |")
            line += "".join(f" {r.cell[i]}:{r.lvl[i]}:{r.cool[i]}" for i in range(towers.CELLS)) + " |"
            line += "".join(f" {f.kind},{f.hp},{f.x},{f.y},{f.d},{int(f.slow)}" for f in r.foes if f.on) + " |"
            line += "".join(f" {f[4]},{f[5]},{f[2]},{f[3]}" for f in r.flashes)
            out.append(line)
        return out

    def test_same_as_firmware(self):
        # builds and upgrades all over, cursor moves, held keys, waves called early, pauses
        bits = [1, 2, 4, 8, 16, 16, 16, 128, 256, 0, 0, 32, 1 | 16, 8 | 16]
        for seed in (2, 11, 404):
            sc = script(seed, 9000, bits, step=(16, 17, 33, 34, 50))
            want = run_firmware("towers.cpp", "towers", self.DUMP, 1, sc)
            got = self.py(sc)
            self.assertEqual(len(want), len(got))
            for i, (a, b) in enumerate(zip(want, got)):
                self.assertEqual(a, b, f"seed {seed}, tick {i}")

    def test_reaches_waves_and_game_over(self):
        sc = [(1 + 33 * i, 0) for i in range(30000)] + [(1 + 33 * 30000, 64)]
        want = run_firmware("towers.cpp", "towers", self.DUMP, 1, sc)
        got = self.py(sc)
        self.assertEqual(want, got)
        last = got[-2].split("|")[1].split()
        self.assertEqual(last[1], "0")           # no towers: the lives ran out
        self.assertEqual(int(last[2]), 2)           # gone in the second wave


@unittest.skipUnless(shutil.which("g++") and FW.is_dir(), "needs g++ and firmware-remote/src")
class AgainstSwarm(unittest.TestCase):
    DUMP = r'''std::cout << " | " << ship_x << ' ' << lives << ' ' << stage << ' ' << sway << ' ' << enter_y << ' '
            << frame << ' ' << fire_wait << ' ' << next_step << ' ' << dead_until << ' ' << safe_until << ' '
            << clear_at << ' ' << next_dive << ' ' << over << paused << " |";
        for (auto& f : foes) std::cout << ' ' << (int)f.state << ',' << (int)f.hp << ',' << f.x << ',' << f.y << ',' << f.vx;
        std::cout << " |";
        for (auto& x : shots) std::cout << ' ' << x.on << ',' << x.x << ',' << x.y;
        for (auto& x : bombs) std::cout << ' ' << x.on << ',' << x.x << ',' << x.y;
        for (auto& x : booms) std::cout << ' ' << x.x << ',' << x.y << ',' << (int)x.t;
        for (int i = 0; i < 12; i++) std::cout << ' ' << (int)star_x[i] << ',' << (int)star_y[i];'''

    @staticmethod
    def py(seed, sc):
        r = swarm.Rules(Lcg(seed))
        r.start(1)
        out = []
        for ms, k in sc:
            ch = r.tick(ms, k)
            line = (f"{int(ch)} {int(r.quit())} {r.score()} | {r.ship_x} {r.lives} {r.stage} {r.sway} {r.enter_y} "
                    f"{r.frame} {r.fire_wait} {r.next_step} {r.dead_until} {r.safe_until} {r.clear_at} {r.next_dive} "
                    f"{int(r.over)}{int(r.paused)} |")
            line += "".join(f" {f.state},{f.hp},{f.x},{f.y},{f.vx}" for f in r.foes) + " |"
            line += "".join(f" {int(a)},{b},{c}" for a, b, c in r.shots + r.bombs)
            line += "".join(f" {a},{b},{c}" for a, b, c in r.booms)
            line += "".join(f" {r.star_x[i]},{r.star_y[i]}" for i in range(12))
            out.append(line)
        return out

    def test_same_as_firmware(self):
        # moving and firing mostly, pauses now and then; long enough for stages, deaths and game over
        bits = [1 | 16, 2 | 16, 16, 16, 1, 2, 8, 0, 1 | 16, 2 | 16, 4 | 16, 128 | 2]
        for seed in (3, 21, 777):
            sc = script(seed, 30000, bits, step=(16, 17, 33, 34))
            sc = [(ms, k | (32 if i % 4000 == 3999 else 0)) for i, (ms, k) in enumerate(sc)]
            want = run_firmware("swarm.cpp", "swarm", self.DUMP, seed, sc)
            got = self.py(seed, sc)
            self.assertEqual(len(want), len(got))
            for i, (a, b) in enumerate(zip(want, got)):
                self.assertEqual(a, b, f"seed {seed}, tick {i}")
            stages = {int(l.split("|")[1].split()[2]) for l in got}
            self.assertGreater(max(stages), 1, "the script clears a stage")


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


class ScreenTowers(unittest.TestCase):
    def test_touch_builds_and_buttons(self):
        pygame.init()
        s = pygame.display.set_mode((1280, 800))
        from rookmote.games import mini
        g = towers.TowersGame(bests=mini.Bests(Path(tempfile.mkdtemp()) / "games.json"))
        g.update(16)
        g.draw(s)
        x = g.ox + 9 * towers.T * towers.S + 20          # tile (9, 0): grass
        y = g.oy + towers.TOP * towers.S + 20
        g.touch_down(1, x, y)                            # pick it
        self.assertEqual((g.g.cx, g.g.cy), (9, 0))
        g.update(16)
        blast = next(r for r, fn in g.buttons if r.x == g.buttons[1][0].x)
        g.touch_down(2, *blast.center)
        self.assertEqual(g.g.sel, towers.BLAST)
        g.touch_down(3, x, y)                            # the picked tile again: build
        g.update(16)
        self.assertEqual(g.g.cell[9], towers.BLAST)
        self.assertEqual(g.g.gold, 5)
        g.pad_down("x")                                  # the next wave now: +2
        g.update(16)
        self.assertEqual(g.g.gold, 7)
        g.pad_up("x")
        g.pad_down("b")
        g.update(16)
        self.assertTrue(g.done)


class ScreenSwarm(unittest.TestCase):
    def test_touch_moves_and_fires_then_leave(self):
        pygame.init()
        s = pygame.display.set_mode((1280, 800))
        from rookmote.games import mini
        g = swarm.SwarmGame(bests=mini.Bests(Path(tempfile.mkdtemp()) / "games.json"), rand=Lcg(9))
        g.update(16)
        x0 = g.g.ship_x
        g.touch_down(1, g.ox + 2 * swarm.S, 700)         # a finger at the far left
        for _ in range(30):
            g.update(34)
        self.assertLess(g.g.ship_x, x0)
        self.assertTrue(any(on for on, _, _ in g.g.shots) or g.g.fire_wait > 0)
        g.touch_up(1, 0, 0)
        g.draw(s)
        g.pad_down("b")
        g.update(16)
        self.assertTrue(g.done)
