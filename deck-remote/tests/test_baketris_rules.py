"""Baketris's rules in Python (rookmote/games/rules.py) against Baketris's own
Game.java: every script in fixtures/baketris_trace.json.gz (made by
make_baketris_fixtures.py, which runs Game.java) is replayed here, and the
state after every command must be the one Java printed: piece, rotation,
place, hold, the queue (the 7-bag from java.util.Random), score, lines,
level, combo, back-to-back, lock timer and resets, the board, and the
listener's calls (locks with their T-spins, hard drops, the end)."""

import gzip
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from rookmote.games import rules as R  # noqa: E402


class Rec(R.Listener):
    def __init__(self):
        self.events = []

    def on_move(self): self.events.append("move")
    def on_rotate(self): self.events.append("rotate")
    def on_hold(self): self.events.append("hold")
    def on_hard_drop(self, t, rot, x, f, to): self.events.append(["hard", t, rot, x, f, to])
    def on_lock(self, cleared, rows, spin, b2b, combo, pc, hard):
        self.events.append(["lock", cleared, list(rows), spin, b2b, combo, pc, hard])
    def on_end(self, win): self.events.append(["end", win])


def run(g, rec, line):
    p = line.split()
    rec.events = []
    ret = None
    c = p[0]
    if c == "reset":
        g.reset(int(p[1]), int(p[2]))
        g.next_piece()
    elif c in ("L", "R"):
        ret = g.shift(-1 if c == "L" else 1)
    elif c in ("cw", "ccw", "180"):
        ret = g.rotate({"cw": 1, "ccw": -1, "180": 2}[c])
    elif c == "soft":
        ret = g.soft_step()
    elif c == "hard":
        g.hard_drop()
    elif c == "hold":
        g.do_hold()
    elif c == "u":
        g.update(float(p[1]), p[2] == "1", int(p[3]))
    elif c == "set":
        g.grid[int(p[1])][int(p[2])] = int(p[3])
    elif c == "clear":
        for row in g.grid:
            row[:] = [0] * R.W
    elif c == "put":
        g.cur, g.rot, g.px, g.py = (int(x) for x in p[1:5])
        g.last_rotate, g.last_kick, g.lock_timer, g.resets, g.lowest_y = False, -1, 0.0, 0, g.py
    return ret


def state(g, rec, ret):
    return {"ret": ret, "cur": g.cur, "rot": g.rot, "px": g.px, "py": g.py, "hold": g.hold, "holdUsed": g.hold_used,
            "queue": list(g.queue), "score": g.score, "lines": g.lines, "level": g.level, "combo": g.combo,
            "b2b": g.b2b, "pieces": g.pieces, "over": g.over, "won": g.won, "lastKick": g.last_kick,
            "resets": g.resets, "lockTimer": g.lock_timer, "timeMs": g.time_ms,
            "grid": "".join(str(g.grid[r][c]) for r in range(24) for c in range(R.W)), "events": rec.events}


class RulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = json.loads(gzip.decompress((HERE / "fixtures" / "baketris_trace.json.gz").read_bytes()))

    def test_gravity_table(self):
        g = R.Game()
        want = self.fx["grav"]["states"][0]["grav"]
        got = []
        for lv in range(1, 23):
            g.level = lv
            got.append(g.gravity_ms())
        self.assertEqual(got, want)

    def test_traces(self):
        n = 0
        for name, sc in self.fx.items():
            if name == "grav":
                continue
            g, rec = R.Game(), Rec()
            g.listener = rec
            for i, (cmd, want) in enumerate(zip(sc["commands"], sc["states"])):
                ret = run(g, rec, cmd)
                got = json.loads(json.dumps(state(g, rec, ret)))
                if got != want:
                    diff = {k: (got[k], want[k]) for k in want if got.get(k) != want[k]}
                    self.fail(f"{name}, command {i} ({cmd}): differs from Game.java: {diff}")
                n += 1
        self.assertGreater(n, 10000)

    def test_the_built_boards_mean_something(self):
        """The hand-built scripts reach what they were built for (so the comparison tests that too)."""
        def locks(name):
            return [e for s in self.fx[name]["states"] for e in s["events"] if isinstance(e, list) and e[0] == "lock"]
        self.assertEqual(locks("tspin-double")[0][1:4], [2, [0, 1], 2])                  # a T-spin double
        self.assertEqual(locks("all-clear-quad")[0][6], True)                            # an all clear
        self.assertTrue(any(l[4] for l in locks("b2b-quads")))                           # back-to-back
        self.assertEqual([l[5] for l in locks("combo")], [0, 1, 2])                      # a combo
        self.assertTrue(any(s["over"] for s in self.fx["top-out"]["states"]))
        self.assertTrue(self.fx["ultra-runs-out"]["states"][-1]["won"])


if __name__ == "__main__":
    unittest.main()
