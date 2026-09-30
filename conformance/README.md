# Rook conformance suite

Checks an implementation of the Rook core ([spec](../docs/spec/core-v1.md))
against the Python reference, in two halves:

- **Offline vectors** (`vectors/*.json`): inputs and expected outputs for
  every piece of the core that can be tested without a network. Generated
  from the reference code by `generate.py`; never edited by hand.
- **Live harness** (`harness.py`): starts your worker against a real
  reference hub and exercises it over the band.

## Offline vectors

| File | Covers | Spec |
|---|---|---|
| `band_crypto.json` | band id, key derivation, ChaCha20-Poly1305 with empty AAD | 2.1, 2.2 |
| `fragments.json` | fragment envelope: `split`, `parse` (valid/invalid chunks), `reassemble` scenarios (reordering, duplicates, junk) | 2.4 |
| `frames.json` | complete UDP datagrams for whole messages, including a multi-fragment one | 2.2–2.4 |
| `messages.json` | what a worker replies (or not) to each inbound message | 3.1, 3.2 |
| `canonical.json` | canonical JSON and `args_hash` (sorting above U+FFFF, escapes) | 6.3 |
| `signatures.json` | test keys (published seeds), grant verification, signed announces (held roles), tickets, replay | 6.3–6.6 |
| `tiers.json` | effective tier resolution, plus the built-in tier table | 4.3 |
| `projection.json` | the `limit`/`fields` output contract | 4.4 |
| `placement.json` | placement expressions over four sample nodes, including invalid ones | 5.2 |
| `core_api.json` | plugin `CORE_API` range matching | 9 |
| `resources.json` | resource connection strings | 9 |

Conventions: byte strings are lowercase hex; u64 values that can exceed 2^53
are decimal strings (`sequence` in `band_crypto.json`); every file has
`"spec"` (the spec version) and `"vector"` (its name). In `signatures.json`
only `ok` and `held_roles` are normative, `reason` is the reference's text.
In `messages.json`, a reply with `error_prefix` matches when its `error`
starts with that prefix; compare replies as JSON objects.

Regenerate after changing the reference, and commit the result:

    python conformance/generate.py          # rewrite vectors/
    python conformance/generate.py --check  # what the unit suite runs

## The candidate contract

The harness starts your worker as a subprocess with:

| Variable | Meaning |
|---|---|
| `ROOK_RELAY` | `host:port` of the relay (UDP) |
| `ROOK_PSK` | band pre-shared key |
| `ROOK_NAME` | worker name to announce |
| `ROOK_IDENTITY` | identity to stamp on calls the worker makes |
| `ROOK_ANCHOR` | base64 root public key: verify the hub's `is_hub` grant with it |
| `ROOK_ANNOUNCE_SECS` | announce interval (the harness sets 5) |

and expects these caps (plus `caps.describe`):

| Cap | Args | Returns |
|---|---|---|
| `conformance.echo` | `value = null` | `value` unchanged |
| `conformance.add` | `a`, `b` (required) | `a + b` |
| `conformance.chat_post` | `room`, `text` (required) | `{"hub": <result of chat.write send on rook>, "rook": {"worker_id", "verified"}}` |
| `conformance.chat_read` | `room` (required), `since_seq = 0` | `{"hub": <result of chat.read read on rook>, "rook": {…}}` |

Checks (18): announce presence, required caps, `tiers`, `facts`, repeat
interval; echo round trip with non-ASCII; a 9 KB echo (fragmented both ways);
a result; `bad args`, `unknown capability` and `args must be an object`
errors; silence on open calls it can't serve and on calls targeted at another
worker; `caps.describe` with a prefix; and a chat room round trip through
`rook`: the candidate posts with `chat.write` (the hub must store it as
`band:<ROOK_IDENTITY>`), proves it verified `rook`'s grant, and reads the
harness's reply back with `chat.read`.

## Running the harness

    # boots a throwaway hub (needs the relay binary: TELESTHETE_HUB or telesthete-hub on PATH)
    python conformance/harness.py --candidate "node examples/ports/typescript/src/main.ts"
    python conformance/harness.py --candidate "examples/ports/rust/target/release/rook-port"
    python conformance/harness.py --candidate "python conformance/reference_worker.py"

    # or reuse a running test hub started with --band-max-risk write
    scripts/test-hub.sh start --data /tmp/conf-hub --port-base 23470 --workers 0 \
        --no-dashboard --band-max-risk write
    python conformance/harness.py --hub-env /tmp/conf-hub/test-hub.env --candidate "…"

`--json` adds a machine-readable report; `--log` keeps the candidate's output.
A hub whose band ceiling is `read` skips the chat checks.

`reference_worker.py` is the Python reference candidate: the stock
`rook.worker.core.Worker` with the conformance caps added.

## In the test suites

- `tests/test_conformance.py` (normal suite): vectors are current and the
  reference verifies them; the ports' vector suites with `ROOK_PORTS=1`.
- `tests/integration/test_it_conformance.py` (`ROOK_IT=1`): the live run for
  the reference candidate, and for both ports with `ROOK_PORTS=1`.
