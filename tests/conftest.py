"""Make `telesthete` (the sibling reference-lib checkout) importable in tests,
matching how build_band_worker.py / stage_worker.py vendor it into bundles."""

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TELESTHETE = os.path.join(os.path.dirname(_REPO), "telesthete")

for p in (_REPO, _TELESTHETE):
    if p not in sys.path:
        sys.path.insert(0, p)
