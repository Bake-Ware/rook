"""The "fix this bug" target: a tiny repo the harness force-resets before each run.

Kept as data (not files) so the repo's own test run never collects its
deliberately failing test.
"""

BUG = ("median() is wrong for an even number of values: median([4, 1, 3, 2]) returns 3 "
       "but should be 2.5. Fix it so `python3 -m unittest -q` passes.")

FILES = {
    "README.md": "# toy-stats\n\nA tiny statistics module the Rook clean-room harness uses as a \"fix this bug\"\ntarget. Run the tests with `python3 -m pytest -q` (or `python3 -m unittest`).\n",
    "tests/__init__.py": "",
    "tests/test_stats.py": "import unittest\n\nfrom toy.stats import mean, median\n\n\nclass StatsTest(unittest.TestCase):\n    def test_mean(self):\n        self.assertEqual(mean([1, 2, 3, 4]), 2.5)\n\n    def test_median_odd(self):\n        self.assertEqual(median([3, 1, 2]), 2)\n\n    def test_median_even(self):\n        self.assertEqual(median([4, 1, 3, 2]), 2.5)\n\n\nif __name__ == \"__main__\":\n    unittest.main()\n",
    "toy/__init__.py": "",
    "toy/stats.py": "\"\"\"Small statistics helpers.\"\"\"\n\n\ndef mean(xs):\n    xs = list(xs)\n    if not xs:\n        raise ValueError(\"mean of an empty sequence\")\n    return sum(xs) / len(xs)\n\n\ndef median(xs):\n    xs = sorted(xs)\n    if not xs:\n        raise ValueError(\"median of an empty sequence\")\n    mid = len(xs) // 2\n    return xs[mid]\n"
}
