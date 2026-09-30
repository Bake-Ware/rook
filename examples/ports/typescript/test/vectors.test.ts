// Runs every offline conformance vector (conformance/vectors/*.json) against
// this port. `node --test test/`  (ROOK_VECTORS overrides the directory).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { argsHash, canonical } from "../src/canonical.ts";
import { Reassembler, bandId, bandKey, fragment, open, packFrame, parseChunk, seal, unpackFrame } from "../src/band.ts";
import { ReplayCache, effectiveTier, heldRoles, keyId, verifyGrant, verifyTicket } from "../src/authz.ts";
import { compilePlacement, evaluatePlacement } from "../src/placement.ts";
import { Registry, coreApiCompatible, parseResource } from "../src/registry.ts";
import { Worker } from "../src/worker.ts";

const DIR = process.env.ROOK_VECTORS ?? join(dirname(fileURLToPath(import.meta.url)), "../../../../conformance/vectors");
const load = (n: string) => JSON.parse(readFileSync(join(DIR, `${n}.json`), "utf8"));
const hex = (s: string) => Buffer.from(s, "hex");

test("band_crypto", () => {
  for (const c of load("band_crypto").cases) {
    assert.equal(bandId(c.psk).toString("hex"), c.band_id);
    const key = bandKey(c.psk);
    assert.equal(key.toString("hex"), c.key);
    const seq = BigInt(c.sequence);
    assert.equal(seal(key, seq, hex(c.plaintext)).toString("hex"), c.ciphertext);
    assert.equal(open(key, seq, hex(c.ciphertext)).toString("hex"), c.plaintext);
  }
});

test("fragments", () => {
  const v = load("fragments");
  for (const c of v.split) {
    assert.deepEqual(fragment(hex(c.payload), hex(c.fragment_id), c.chunk_size).map((b) => b.toString("hex")), c.chunks);
  }
  for (const c of v.parse) {
    const got = parseChunk(hex(c.chunk));
    assert.equal(got !== null, c.valid, c.label);
    if (got) assert.deepEqual([got.fid, got.seq, got.total, got.data.toString("hex")], [c.fragment_id, c.seq, c.total, c.data]);
  }
  for (const s of v.reassemble) {
    const r = new Reassembler();
    const out = s.feed.map((c: string) => r.feed(hex(c))).filter((x: Buffer | null) => x !== null);
    assert.deepEqual(out.map((b: Buffer) => b.toString("hex")), s.emits, s.label);
  }
});

test("frames", () => {
  for (const c of load("frames").cases) {
    const key = bandKey(c.psk), band = bandId(c.psk);
    let seq = BigInt(c.first_sequence);
    const built = fragment(hex(c.message), hex(c.fragment_id)).map((chunk) => {
      const f = packFrame(band, seq, seal(key, seq, chunk)); seq += 1n; return f.toString("hex");
    });
    assert.deepEqual(built, c.frames, c.label);
    const r = new Reassembler();
    let msg: Buffer | null = null;
    for (const f of c.frames) {
      const fr = unpackFrame(hex(f))!;
      assert.ok(fr.band.equals(band) && fr.type === 2 && fr.channel === 0);
      msg = r.feed(open(key, fr.seq, fr.sealed)) ?? msg;
    }
    assert.equal(msg!.toString("hex"), c.message);
  }
});

test("canonical", () => {
  for (const c of load("canonical").cases) {
    assert.equal(canonical(c.value), c.canonical);
    assert.equal(argsHash(c.value), c.args_hash);
  }
});

test("signatures", () => {
  const v = load("signatures");
  for (const k of v.keys) assert.equal(keyId(k.public), k.kid);
  for (const c of v.grants) {
    const x = c.context;
    const [ok] = verifyGrant(c.grant, x.anchors, { band: x.band, now: x.now, revoked: x.revoked, workerId: x.worker_id });
    assert.equal(ok, c.ok, c.label);
  }
  for (const c of v.announces) {
    assert.deepEqual(heldRoles(c.announce, c.context.anchors, { band: c.context.band, now: c.context.now }), c.held_roles, c.label);
  }
  for (const c of v.tickets) {
    const e = c.envelope;
    const [ok] = verifyTicket(c.ticket, { cap: e.cap, target: e.target, msgId: e.msg_id, args: e.args, keys: c.grants_by_kid, now: c.now });
    assert.equal(ok, c.ok, c.label);
  }
  const r = v.ticket_replay, replay = new ReplayCache();
  for (const want of r.results) {
    const e = r.envelope;
    const [ok] = verifyTicket(r.ticket, { cap: e.cap, target: e.target, msgId: e.msg_id, args: e.args, keys: r.grants_by_kid, now: r.now, replay });
    assert.equal(ok, want.ok);
  }
});

test("placement", () => {
  const v = load("placement");
  for (const c of v.cases) {
    let valid = true;
    try { compilePlacement(c.expr); } catch { valid = false; }
    assert.equal(valid, c.valid, `valid: ${c.expr}`);
    for (const [n, want] of Object.entries(c.results)) {
      assert.equal(evaluatePlacement(c.expr, v.nodes[n]), want, `${c.expr} on ${n}`);
    }
  }
});

test("tiers", () => {
  const v = load("tiers");
  for (const c of v.cases) {
    assert.equal(effectiveTier(v, c.cap, c.declared, c.override, c.lower), c.tier, JSON.stringify(c));
  }
});

test("projection", async () => {
  for (const c of load("projection").cases) {
    const reg = new Registry();
    let received: unknown = null;
    reg.register("t.x", {
      handler: (a) => { received = a; return structuredClone(c.handler_result); },
      params: c.handler_params.map((n: string) => ({ name: n })),
      doc: "", meta: c.meta ?? undefined,
    });
    assert.deepEqual(await reg.call("t.x", structuredClone(c.args)), c.result, c.label);
    assert.deepEqual(received, c.handler_received, c.label);
  }
});

test("core_api", () => {
  for (const c of load("core_api").cases) assert.equal(coreApiCompatible(c.spec, c.have), c.compatible, JSON.stringify(c));
});

test("resources", () => {
  for (const c of load("resources").cases) {
    let got: unknown = null;
    try { got = parseResource(c.url); } catch { got = null; }
    assert.equal(got !== null, c.valid, c.url);
    if (got) assert.deepEqual(got, { scheme: c.scheme, target: c.target, path: c.path });
  }
});

test("messages", async () => {
  for (const c of load("messages").cases) {
    const w = new Worker({ workerId: c.worker_id, name: "worker-a" });
    w.registry.register("conformance.echo", { handler: (a) => a.value ?? null, params: [{ name: "value", default: null }], doc: "" });
    w.registry.register("conformance.add", {
      handler: (a) => (a.a as number) + (a.b as number),
      params: [{ name: "a", required: true }, { name: "b", required: true }], doc: "",
    });
    const sent: Record<string, unknown>[] = [];
    w.send = async (m) => { sent.push(JSON.parse(JSON.stringify(m))); };
    if (c.input_hex !== undefined) {
      let parsed: unknown;
      try { parsed = JSON.parse(hex(c.input_hex).toString("utf8")); } catch { parsed = undefined; }
      if (parsed !== undefined) await w.onMessage(parsed);
    } else await w.onMessage(structuredClone(c.input));
    assert.equal(sent.length, c.replies.length, c.label);
    c.replies.forEach((want: Record<string, any>, i: number) => {
      const got = sent[i] as Record<string, any>;
      const { error_prefix, ...exact } = want;
      if (error_prefix) {
        assert.ok(String(got.error).startsWith(error_prefix), c.label);
        delete exact.error; delete got.error;
      }
      assert.deepEqual(got, exact, c.label);
    });
  }
});
