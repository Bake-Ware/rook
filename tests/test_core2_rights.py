"""rights.matches beyond the vectors: patterns are caller input (a session's
declared caps come from the agent), so matching must stay linear."""
from __future__ import annotations

import random
import re
import subprocess
import sys

from rook.core2 import rights


def _reference(pattern: str, name: str) -> bool:
    return bool(re.fullmatch(".*".join(re.escape(p) for p in pattern.split("*")), name, re.S))


def test_matches_agrees_with_the_glob_definition():
    rnd = random.Random(7)
    for _ in range(5000):
        pat = "".join(rnd.choice("ab.*") for _ in range(rnd.randint(0, 7)))
        name = "".join(rnd.choice("ab.") for _ in range(rnd.randint(0, 8)))
        assert rights.matches(pat, name) == _reference(pat, name), (pat, name)
    assert rights.matches("light.*@down*", "light.set", "downstairs-lamp")
    assert not rights.matches("light.*@down*", "light.set", "upstairs")


def test_many_stars_match_in_linear_time():
    # A child process with a hard timeout: a backtracking matcher never returns.
    code = ("from rook.core2 import rights\n"
            "assert not rights.matches('*a' * 30 + '*!', 'a' * 60)\n"
            "assert not rights.matches('x@' + '*a' * 30 + '*!', 'x', 'a' * 60)\n")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10)
