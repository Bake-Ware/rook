"""Build/version identity for the worker bundle.

These values are the committed *defaults* — what you get running from a source
checkout. At bundle time, ``rook/remote/build_band_worker.py`` overwrites this
file with the real stamp (git commit count + short hash + build time).

``BUILD`` is the monotonic comparator the OTA self-update uses: a worker updates
only when a signed manifest advertises a ``build`` strictly greater than its own.
An unstamped/dev worker has ``BUILD = 0``, which is never greater than any
published build, so it never auto-updates itself out from under a developer.
"""

from __future__ import annotations

BUILD = 0            # monotonic integer (git rev-list --count HEAD); 0 = unstamped/dev
VERSION = "0.dev"    # human-readable "<BUILD>.<commit>" once stamped
COMMIT = ""          # git short hash
BUILT_AT = ""        # ISO-8601 UTC build timestamp


def as_dict() -> dict:
    return {"version": VERSION, "build": BUILD, "commit": COMMIT, "built_at": BUILT_AT}
