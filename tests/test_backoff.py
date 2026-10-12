"""rook.core.backoff and the reconnect loops that use it."""

import sys
from pathlib import Path

import pytest

from rook.core.backoff import Backoff


def test_doubles_to_cap_without_jitter():
    b = Backoff(base=1.0, cap=30.0, jitter=0.0)
    assert [b.next() for _ in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    b.reset()
    assert b.next() == 1.0


def test_jitter_stays_within_half_to_full_nominal():
    lo = Backoff(base=2.0, cap=60.0, rng=lambda: 1.0)   # max jitter
    hi = Backoff(base=2.0, cap=60.0, rng=lambda: 0.0)   # none
    assert [lo.next() for _ in range(3)] == [1.0, 2.0, 4.0]
    assert [hi.next() for _ in range(3)] == [2.0, 4.0, 8.0]
    b = Backoff(base=1.0, cap=8.0)
    for _ in range(200):
        nominal = b.peek()
        d = b.next()
        assert nominal / 2 <= d <= nominal <= 8.0


def test_jitter_spreads_a_fleet():
    delays = {round(Backoff(base=1.0, cap=30.0).next(), 6) for _ in range(50)}
    assert len(delays) > 40


def test_long_running_loop_does_not_overflow():
    b = Backoff(base=1.0, cap=5.0, jitter=0.0)
    b.attempt = 10_000
    assert b.next() == 5.0


@pytest.mark.parametrize("kw", [dict(base=0), dict(base=2, cap=1), dict(factor=0.5),
                                dict(jitter=1.5)])
def test_rejects_bad_parameters(kw):
    with pytest.raises(ValueError):
        Backoff(**kw)


def test_core_backoff_is_stdlib_only_and_bundled():
    import ast
    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "rook/core/backoff.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module]
        else:
            continue
        assert all(n.split(".")[0] in sys.stdlib_module_names for n in names)
    # Both bundle builders copy rook/core wholesale, so backoff.py ships.
    for script in ("rook/remote/build_band_worker.py", "android/stage_worker.py"):
        text = (root / script).read_text(encoding="utf-8")
        assert '"core"' in text and "_copy_pkg" in text
