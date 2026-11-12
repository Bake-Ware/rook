# Rook conformance

- [`core-v2/`](core-v2/README.md): the core v2 vectors (docs/spec/core-v2.md).
- [`manifest/`](manifest/): the plugin manifest schema and its cases.
- `vectors/`: core v1's transport vectors, which core v2 incorporates by
  reference (docs/spec/core-v2.md section 2.1). They were generated from the
  0.4 reference code, which is gone, so they are fixed now: never edit them.

| File | Covers | core v1 |
|---|---|---|
| `band_crypto.json` | band id, key derivation, ChaCha20-Poly1305 with empty AAD | 2.1, 2.2 |
| `fragments.json` | fragment envelope: `split`, `parse` (valid/invalid chunks), `reassemble` scenarios (reordering, duplicates, junk) | 2.4 |
| `frames.json` | complete UDP datagrams for whole messages, including a multi-fragment one | 2.2–2.4 |

Byte strings are lowercase hex; u64 values that can exceed 2^53 are decimal
strings (`sequence` in `band_crypto.json`). The Rust worker (`worker/`) checks
`frames.json` and `band_crypto.json` in its `cargo test`.
