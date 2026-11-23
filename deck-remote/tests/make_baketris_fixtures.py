"""Makes tests/fixtures/baketris_trace.json.gz: command scripts (seeded random play
in every mode, and built boards for T-spins, kicks, all clears, combos) run
through Baketris's own Game.java (javac, from /home/bake/baketris, read only),
with the state Java printed after every command. test_baketris_rules.py
replays the scripts on the Python port and wants the same states.
Run: python deck-remote/tests/make_baketris_fixtures.py (needs a JDK)."""

import gzip
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path("/home/bake/baketris/src/systems/bake/baketris/Game.java")


def play(mode, seed, n, rnd):
    """Random play: moves, rotations, soft steps, holds, hard drops and frames of time."""
    cmds = [f"reset {mode} {seed}"]
    for _ in range(n):
        x = rnd.random()
        if x < 0.30:
            cmds.append(rnd.choice(["L", "R", "L", "R", "cw", "ccw", "180"]))
        elif x < 0.36:
            cmds.append("soft")
        elif x < 0.40:
            cmds.append("hold")
        elif x < 0.47:
            cmds.append("hard")
        else:
            dt = rnd.choice(["16.666666666666668", "8.0", "33.0", "250.0", "1.0"])
            soft = "1" if rnd.random() < 0.15 else "0"
            cmds.append(f"u {dt} {soft} {rnd.choice([6, 20, 41])}")
    return cmds


def board(rows, put, moves, mode=0, seed=7):
    """A built board: rows of '#'/'.' from the bottom (row 0) up, a piece put, then moves."""
    cmds = [f"reset {mode} {seed}", "clear"]
    for r, s in enumerate(rows):
        for c, ch in enumerate(s):
            if ch == "#":
                cmds.append(f"set {r} {c} 8")
    cmds.append("put " + put)
    return cmds + moves


def scripts():
    rnd = random.Random(20261122)
    out = {"grav": ["grav"]}
    for mode in (0, 1, 2):
        for seed in (0, 1, 42, -7, 123456789012):
            out[f"play-m{mode}-s{seed}"] = play(mode, seed, 700, rnd)
    out["ultra-runs-out"] = ["reset 2 5"] + ["u 30000.0 0 6"] * 5          # a few pieces, then the clock ends it
    out["bag-only"] = ["reset 0 99"] + ["hold", "hard"] * 40
    # T-spin double: a T slot at columns 3..5, rows 0..1; the T comes in by rotating (2 = T)
    # T-spin double: row 0 open at column 4, row 1 open at 3..5, an overhang at row 2 column 3; the T comes
    # in pointing right and turns clockwise into the slot (3 corners filled, both front ones: a full spin)
    out["tspin-double"] = board(["####.#####", "###...####", "...#......"], "2 1 3 2", ["cw", "hard"])
    out["tspin-single-mini"] = board(["#.########", "#........."], "2 0 0 3", ["cw", "hard"])
    out["tst-kick"] = board(["####.#####", "###..#####", "####.#####", "##........", "###......."],
                            "2 0 3 6", ["u 16.0 0 6"] * 20 + ["ccw", "hard"])
    out["i-kicks"] = board(["#.........", "#.........", "#.........", "##########"[:1] + "........."],
                           "0 1 -1 5", ["cw", "ccw", "180", "cw", "R", "hard"])
    out["all-clear-quad"] = board(["#########.", "#########.", "#########.", "#########."], "0 1 7 6", ["hard"])
    out["b2b-quads"] = board(["#########."] * 8, "0 1 7 10", ["hard", "put 0 1 7 6", "hard"])
    out["combo"] = board(["########.."] * 6, "1 0 8 10",            # O pieces into a 2-wide well: 2 lines each
                         ["hard", "put 1 0 8 10", "hard", "put 1 0 8 10", "hard"])
    out["lock-resets"] = ["reset 0 3"] + ["hard"] * 3 + ["u 16.0 0 6", "L", "R"] * 40
    out["top-out"] = ["reset 1 11"] + ["hard"] * 60
    out["sprint-sdf"] = ["reset 1 8"] + ["u 16.666666666666668 1 41", "u 16.666666666666668 1 6", "R", "cw"] * 120
    return out


def main():
    javac = shutil.which("javac")
    if not javac:
        sys.exit("needs a JDK (javac)")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "systems" / "bake" / "baketris"
        pkg.mkdir(parents=True)
        shutil.copy(SRC, pkg / "Game.java")
        shutil.copy(HERE / "java" / "Harness.java", pkg / "Harness.java")
        subprocess.run([javac, "-d", tmp, str(pkg / "Game.java"), str(pkg / "Harness.java")], check=True)
        fixture = {}
        for name, cmds in scripts().items():
            r = subprocess.run(["java", "-cp", tmp, "systems.bake.baketris.Harness"], input="\n".join(cmds) + "\n",
                               capture_output=True, text=True, check=True)
            fixture[name] = {"commands": cmds, "states": [json.loads(l) for l in r.stdout.splitlines()]}
    out = HERE / "fixtures" / "baketris_trace.json.gz"
    out.write_bytes(gzip.compress(json.dumps(fixture, separators=(",", ":")).encode(), 9, mtime=0))
    print(f"{out}: {sum(len(v['commands']) for v in fixture.values())} commands in {len(fixture)} scripts")


if __name__ == "__main__":
    main()
