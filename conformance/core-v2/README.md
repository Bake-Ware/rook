# Rook core v2 conformance vectors

Offline test vectors for the pure parts of [core v2](../../docs/spec/core-v2.md).
The transport is unchanged from core v1, so its vectors
([`../vectors/band_crypto.json`, `fragments.json`, `frames.json`](../README.md))
still apply.

Every expected outcome (reason codes, allow/deny, negotiated versions,
canonical strings) is written by hand in `generate.py`; the script computes
only keys, hashes and signatures, checks each expectation against the Python
reference (`rook/core2`), and refuses to write when they disagree. The
reference passes them in `tests/test_core2_vectors.py`.

    python conformance/core-v2/generate.py          # rewrite vectors/
    python conformance/core-v2/generate.py --check  # what the unit suite runs

## Files

| File | Covers | Spec |
|---|---|---|
| `canonical.json` | RFC 8785 canonical JSON: `cases` (`value` → `canonical`, or `error: true` when there is no canonical form) and `args_hash` | 3.2 |
| `signatures.json` | test `keys` (Ed25519 seeds, `ed25519:` public keys, `kid`), the per-`typ` `prefixes`, one `signing_input` spelled out, `hubkey` grant checks (`key` = the certified operational key or `null`), and `verify` cases (`valid`) | 3.3, 5.2 |
| `tickets.json` | the worker check order: a `worker` state plus `cases` with the band the request arrived on, `now`, nonces already `seen`, the `request`, the `ticket` and the expected `reason` (`ok` or the reason code) | 6.2, 6.3 |
| `leases.json` | elevation leases checked against the `ticket` of the call they elevate | 7.3 |
| `memberships.json` | membership documents checked against the home hub's keys | 9.3 |
| `version.json` | `negotiate(mine, theirs)` → `speak` (`null` = no common version); symmetric | 2.2 |
| `rights.json` | the enums (`risks`, `scopes`, `effects`, `hard_stop_effects`) and the intersection rule: `layers` (`[name, rights]`, in order) and a `call` → `{ok}` or `{ok: false, layer, reason}` | 4, 8.2, 8.3 |
| `presets.json` | the four core `presets` and, per case, the person's computed `rights` (preset + plugin grants + owner grants) and the decision for a `call` | 8.5 |
| `gate.json` | combining gate results into one dispatch verdict | 10.2 |
| `journal.json` | a three-entry hash chain and tamper `checks` (`first_break` = index of the first entry that doesn't follow, or `null`) | 11.3 |

Conventions: every file has `"spec"` (`"2.0"`) and `"vector"` (its name).
Files are ASCII JSON; parse them before comparing strings (`canonical` and
`bytes_utf8` hold non-ASCII text once parsed). Times are Unix seconds.
A `call` in `rights.json` and `presets.json` is `{cap, risk, effects,
target}`. In `tickets.json` the worker's trust store is keyed by `hub_id`
with each hub's verified operational keys; a nonce in `seen` was recorded
by an earlier call.

## Reason codes

| Object | Codes |
|---|---|
| ticket (spec 6.3, in check order) | `no_ticket`, `malformed`, `unknown_hub`, `unknown_key`, `bad_signature`, `wrong_band`, `wrong_worker`, `wrong_appliance`, `wrong_call`, `wrong_cap`, `wrong_args`, `bad_lifetime`, `not_yet_valid`, `expired`, `replay`, `unknown_cap`, `outside_ceiling` |
| lease | `malformed`, `unknown_key`, `bad_signature`, `wrong_hub`, `wrong_band`, `wrong_worker`, `wrong_session`, `wrong_identity`, `bad_lifetime`, `expired` |
| membership | `malformed`, `unknown_key`, `bad_signature`, `wrong_worker`, `wrong_root`, `bad_role`, `bad_ceiling` |
| rights layer | `grants`, `risk`, `effects` |
