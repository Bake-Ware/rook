# Rook worker in Rust

A minimal Rook worker: joins a band through the relay, announces its caps,
answers calls, and posts to the hub's chat rooms. Standard-library networking
and threads (no async runtime); RustCrypto crates for the AEAD, HKDF and
hashes, `ed25519-dalek` for signatures, `serde_json` for messages.

| File | Spec | What |
|---|---|---|
| `src/band.rs` | 2 | band id/key, CHANNEL frames, fragmentation, `Link` (UDP to the relay) |
| `src/canonical.rs` | 6.3 | canonical JSON, `args_hash` |
| `src/authz.rs` | 4.3, 6 | tiers, grants, signed announces, tickets |
| `src/placement.rs` | 5.2 | placement expression parser/evaluator |
| `src/registry.rs` | 4, 9 | caps with params, `caps.describe`, `limit`/`fields`, `CORE_API`, resources |
| `src/worker.rs` | 3 | `Worker` (dispatch rules, announces) and `Peer` (calls to other nodes and to `rook`) |
| `src/main.rs` | | the example worker and its caps |

## Build and run

    cargo build --release
    ROOK_RELAY=127.0.0.1:7474 ROOK_PSK='<band psk>' ROOK_NAME=rs-worker \
    ROOK_ANCHOR='<hub root public key>' target/release/rook-port

Against a throwaway hub from this repo (`scripts/test-hub.sh start --data
/tmp/th --port-base 23470 --band-max-risk write`), the PSK is `ROOK_BAND_PSK`
in `/tmp/th/secrets.env` and the root key `ROOK_IT_ROOT_PUB` in
`/tmp/th/test-hub.env`.

## Test

    cargo test              # all offline vectors from ../../../conformance/vectors
    python ../../../conformance/harness.py --candidate "examples/ports/rust/target/release/rook-port"   # from the repo root

## Adding a cap

```rust
w.register("myapp.greet", Cap {
    handler: Arc::new(|a| Ok(json!(format!("hello {}", a.get("name").and_then(Value::as_str).unwrap_or("world"))))),
    params: vec![Param::opt("name", json!("world"), "str")],
    doc: "Say hello.".into(),
    meta: Meta { risk: Some("read"), ..Default::default() },
});
```

Each inbound request runs on its own thread, so a cap may block, including
on a call to another node through `Peer::call` / `Peer::call_rook`.
