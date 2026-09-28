"""ed25519 public key (base64) that signs OTA update manifests and deauth orders.

Empty in the source tree: a worker run straight from a checkout or a pip
install trusts nobody's updates (fail closed) unless ``ROOK_UPDATE_PUBKEY`` is
set in its environment.

Worker bundles get the key written in at build time. The hub generates its
signing key on first start (see ``rook/remote/update_keys.py``), and
``build_band_worker.py`` stamps the matching public key into the copy of this
file inside ``band-worker.pyz``, so each hub's workers trust only that hub.
"""

PUBKEY_B64 = ""
