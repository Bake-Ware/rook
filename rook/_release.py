"""Release identity of an installed Rook (wheel, sdist, Docker image).

These are the committed defaults: a source checkout is not a release.
``tools/release_prep.py`` overwrites this file in the release workflow, before
the wheel is built, so a pip-installed hub knows which tag and which build it
is without a git checkout.

Two version schemes, two jobs:

- ``VERSION`` is the PEP 440 package version (tag ``v0.2.0`` -> ``0.2.0``), what
  pip, the GitHub Release and the Docker tag use.
- ``BUILD``/``COMMIT`` are the source of the human ``<build>.<adjective>.<noun>``
  build name (``git rev-list --count`` + a name derived from the commit), which
  the OTA self-update compares. A hub installed from a wheel has no ``.git``, so
  ``rook/remote/build_band_worker.py`` stamps the worker bundles it builds from
  these values; without them every such bundle would be build 0 and its workers
  would never update.

Deliberately separate from ``rook/worker/_build_info.py``: stamping that file
in a wheel would make a pip-installed *worker* accept OTA updates, which only
bundle installs should do.
"""

VERSION = ""     # PEP 440 release version; "" in a source checkout
BUILD = 0        # git rev-list --count of the release commit; 0 = not a release
COMMIT = ""      # short hash of the release commit
NAME = ""        # "<build>.<adjective>.<noun>", e.g. 523.lumpy.moose
