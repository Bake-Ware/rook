// Example worker: joins a band through the relay, announces a few caps,
// answers calls and posts to hub chat rooms. Configured by environment
// (the conformance contract, conformance/README.md):
//   ROOK_RELAY=host:port  ROOK_PSK=...  ROOK_NAME=ts-worker
//   ROOK_IDENTITY=agent:ts-worker  ROOK_ANCHOR=<root pubkey b64>  ROOK_ANNOUNCE_SECS=30
import os from "node:os";
import { Worker } from "./worker.ts";

const env = process.env;
if (!env.ROOK_RELAY || !env.ROOK_PSK) {
  console.error("set ROOK_RELAY=host:port and ROOK_PSK");
  process.exit(2);
}

const w = new Worker({ name: env.ROOK_NAME || `${os.hostname()}-ts`, workerId: env.ROOK_WORKER_ID,
  identity: env.ROOK_IDENTITY || "agent:ts-worker", anchor: env.ROOK_ANCHOR || "" });

w.registry.register("port.info", {
  handler: () => ({ language: "typescript", runtime: `node ${process.version}`, name: w.name }),
  params: [], doc: "Which implementation this worker is.", meta: { risk: "read" },
});
w.registry.register("conformance.echo", {
  handler: (a) => a.value ?? null, params: [{ name: "value", default: null }],
  doc: "Return value unchanged.", meta: { risk: "read" },
});
w.registry.register("conformance.add", {
  handler: (a) => {
    if (typeof a.a !== "number" || typeof a.b !== "number") throw new TypeError("a and b must be numbers");
    return a.a + a.b;
  },
  params: [{ name: "a", required: true, type: "int" }, { name: "b", required: true, type: "int" }],
  doc: "Return a + b.", meta: { risk: "read" },
});
w.registry.register("conformance.chat_post", {
  handler: async (a) => ({
    hub: await w.callRook("chat.write", { action: "send", room: a.room, text: a.text }), rook: w.rook }),
  params: [{ name: "room", required: true, type: "str" }, { name: "text", required: true, type: "str" }],
  doc: "Post text to a hub chat room (chat.write on worker rook).", meta: { risk: "write" },
});
w.registry.register("conformance.chat_read", {
  handler: async (a) => ({
    hub: await w.callRook("chat.read", { action: "read", room: a.room, since_seq: a.since_seq ?? 0 }),
    rook: w.rook }),
  params: [{ name: "room", required: true, type: "str" }, { name: "since_seq", default: 0, type: "int" }],
  doc: "Read a hub chat room (chat.read on worker rook).", meta: { risk: "read" },
});

const facts = { os: process.platform, arch: os.arch() === "x64" ? "x86_64" : os.arch(),
  cpus: os.cpus().length, mem_gb: Math.round(os.totalmem() / 2 ** 30 * 10) / 10, pty: false };

await w.run(env.ROOK_PSK, env.ROOK_RELAY, Number(env.ROOK_ANNOUNCE_SECS || 30), facts);
for (const sig of ["SIGINT", "SIGTERM"] as const) process.on(sig, () => process.exit(0));
