"""ed25519 public key (base64) that signs OTA update manifests.

Empty by default = no signing key configured, so the worker refuses every
auto-update (fail closed). To enable OTA self-update, generate a keypair on the
build host and paste the printed public key here:

    python rook/remote/update_keys.py generate

The matching PRIVATE key stays on the build host (never in the repo); this
public half is safe to commit and is what every worker uses to verify a
manifest's signature before swapping its bundle.

The value below is the maintainer's key. A self-hosted fleet should use its
own: set ROOK_UPDATE_PUBKEY in each worker's environment (it takes precedence)
or replace the value here before building the worker bundle.
"""

PUBKEY_B64 = "D5hj75uLaN91Ml6cBIvZ+ZCTjCgtntiMNz48lNrHROw="
