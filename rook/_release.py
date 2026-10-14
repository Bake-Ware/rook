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

``RELAY`` and ``RELAY_SHA256`` pin the relay (telesthete-hub) this Rook runs
with. They are committed, not stamped: the Dockerfile's default
``TELESTHETE_RELAY_VERSION`` must equal ``RELAY`` (a test checks), and the image,
scripts/install-hub.sh and scripts/fetch-relay.py check each download against
``RELAY_SHA256`` rather than a checksum file from the same release page. To
move to a new telesthete release: ``python tools/release_prep.py --pin-relay
vX.Y.Z`` (it also updates the Dockerfile). The release workflow fills
``RELAY_SHA256`` the same way when it is still empty.

Deliberately separate from ``rook/worker/_build_info.py``: stamping that file
in a wheel would make a pip-installed *worker* accept OTA updates, which only
bundle installs should do.
"""

VERSION = ""     # PEP 440 release version; "" in a source checkout
BUILD = 0        # git rev-list --count of the release commit; 0 = not a release
COMMIT = ""      # short hash of the release commit
NAME = ""        # "<build>.<adjective>.<noun>", e.g. 523.lumpy.moose
RELAY = "v0.3.0"
RELAY_SHA256 = {'x86_64': '0b40e3aef245e1d294bbf865441e21f945998c64a0d8e4d6b6bcf295d7371a6d', 'aarch64': '3a3cff7786814047d20bc6775b5df9f1fd1613047990d20aeeb5c5a2cf48d474', 'armv7': 'e16abf547082e0f40e38bcb09ab0a67e4ce5a06a7fc17dbbc70abb853793e2cf'}   # {"x86_64": "<sha256 of telesthete-hub-x86_64-linux.tar.gz>", ...}
