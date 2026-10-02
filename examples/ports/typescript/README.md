# Rook worker in TypeScript

A minimal Rook worker for Node: joins a band through the relay, announces its
caps, answers calls, and posts to the hub's chat rooms. No runtime
dependencies: Node's `crypto` has ChaCha20-Poly1305, HKDF and Ed25519, and
Node ≥ 23.6 runs TypeScript directly (type stripping), so there is no build
step.

| File | Spec | What |
|---|---|---|
| `src/band.ts` | 2 | band id/key, CHANNEL frames, fragmentation, `BandLink` (UDP to the relay) |
| `src/canonical.ts` | 6.3 | canonical JSON, `argsHash` |
| `src/authz.ts` | 4.3, 6 | tiers, grants, signed announces, tickets |
| `src/placement.ts` | 5.2 | placement expression parser/evaluator |
| `src/registry.ts` | 4, 9 | caps with params, `caps.describe`, `limit`/`fields`, `CORE_API`, resources |
| `src/worker.ts` | 3 | dispatch rules, announces, calls to other nodes and to `rook` |
| `src/main.ts` | | the example worker and its caps |

## Run

    ROOK_RELAY=127.0.0.1:7474 ROOK_PSK='<band psk>' ROOK_NAME=ts-worker \
    ROOK_ANCHOR='<hub root public key>' node src/main.ts

Against a throwaway hub from this repo (`scripts/test-hub.sh start --data
/tmp/th --port-base 23470 --band-max-risk write`), the PSK is `ROOK_BAND_PSK`
in `/tmp/th/secrets.env` and the root key `ROOK_IT_ROOT_PUB` in
`/tmp/th/test-hub.env`. The worker then shows up in `rook_workers`, and
`rook_call(worker="ts-worker", cap="port.info")` answers.

## Test

    npm test                # all offline vectors from ../../../conformance/vectors
    npm install && npm run typecheck   # optional: tsc --noEmit (strict, erasable syntax only)
    python ../../../conformance/harness.py --candidate "node examples/ports/typescript/src/main.ts"   # from the repo root

## Adding a cap

```ts
w.registry.register("myapp.greet", {
  handler: (a) => `hello ${a.name ?? "world"}`,
  params: [{ name: "name", default: "world", type: "str" }],
  doc: "Say hello.", meta: { risk: "read" },
});
```

The handler receives exactly the arguments the caller sent (after
`limit`/`fields` handling) and applies its own defaults; missing required or
unknown arguments are rejected with `bad args: …` before it runs.
