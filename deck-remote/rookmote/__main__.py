"""python -m rookmote [--window]: the Rook remote, fullscreen (--window: in a window, with a pointer)."""

import sys

from .app import main

sys.exit(main())
