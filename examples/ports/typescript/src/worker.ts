// A Rook worker: announces its caps, answers calls, and calls other nodes
// (the hub worker "rook" for chat rooms). Spec "Messages" and "Workers".
import { randomBytes } from "node:crypto";
import { BandLink } from "./band.ts";
import { heldRoles } from "./authz.ts";
import { BadArgs, Registry } from "./registry.ts";

export type Msg = Record<string, any>;
export type Send = (msg: Msg) => Promise<void>;

// Python truthiness of an envelope value ("args": [] or false means {}).
const falsy = (v: unknown) => v === undefined || v === null || v === false || v === 0 || v === "" ||
  (Array.isArray(v) && v.length === 0) ||
  (typeof v === "object" && v !== null && !Array.isArray(v) && Object.keys(v).length === 0);

export interface RookPeer { worker_id: string; verified: boolean }

export class Worker {
  readonly registry = new Registry();
  readonly workerId: string;
  name: string;
  identity: string;
  anchor: string;
  band = "";
  rook: RookPeer | null = null;
  send: Send = async () => {};
  private pending = new Map<string, (m: Msg) => void>();

  constructor(opts: { workerId?: string; name: string; identity?: string; anchor?: string }) {
    this.workerId = opts.workerId || randomBytes(16).toString("hex");
    this.name = opts.name;
    this.identity = opts.identity ?? "";
    this.anchor = opts.anchor ?? "";
    this.registry.register("caps.describe", {
      handler: (a) => this.registry.describe(String(a.prefix ?? "")),
      params: [{ name: "prefix", default: "", type: "str" }],
      doc: "Arg schema + docstring for every capability on this worker.",
      meta: { risk: "read" },
    });
  }

  announce(facts: Record<string, unknown> = {}): Msg {
    return {
      kind: "announce", worker_id: this.workerId, name: this.name, description: "",
      caps: this.registry.list(), plugins: [...new Set(this.registry.list().map((c) => c.split(".")[0]))],
      version: "0.1.0-ts", build: 0, app_release: {}, facts, tiers: this.registry.tiers(),
      // This port does not verify hub tickets (mode "off"); see the spec, "Permissions".
      authz: { v: 1, mode: "off", anchors: [], kids: [] },
    };
  }

  /** Handle one inbound band message. Requests are answered through `send`. */
  async onMessage(msg: unknown): Promise<void> {
    if (!msg || typeof msg !== "object" || Array.isArray(msg)) return;
    const m = msg as Msg;
    if (!m.cap) {
      if (m.kind === "announce") this.onAnnounce(m);
      else if ("id" in m && "ok" in m && "from" in m) this.pending.get(String(m.id))?.(m);
      return; // replies and foreign chatter are not requests
    }
    const target = m.target;
    if (target && target !== this.workerId) return;          // addressed to someone else
    const reply = async (body: Msg) => {
      const out: Msg = m.id !== undefined && m.id !== null ? { id: m.id, from: this.workerId } : { from: this.workerId };
      await this.send({ ...out, ...body });
    };
    if (!this.registry.has(m.cap)) {
      if (target === this.workerId) await reply({ ok: false, error: `unknown capability: ${m.cap}` });
      return;                                                  // open call: stay silent
    }
    const args = falsy(m.args) ? {} : m.args;
    if (typeof args !== "object" || Array.isArray(args)) {
      await reply({ ok: false, error: "args must be an object" });
      return;
    }
    try {
      const result = await this.registry.call(m.cap, args);
      await reply({ ok: true, result: result === undefined ? null : result });
    } catch (e) {
      const err = e as Error;
      await reply({ ok: false, error: e instanceof BadArgs ? `bad args: ${err.message}` : `${err.name}: ${err.message}` });
    }
  }

  private onAnnounce(m: Msg): void {
    if (m.name !== "rook" || !m.worker_id) return;
    if (this.anchor) {
      // Only the holder of a root-signed is_hub grant for this band is "rook".
      const roles = heldRoles(m, [this.anchor], { band: this.band, now: Date.now() / 1000 });
      if (roles.includes("is_hub")) this.rook = { worker_id: String(m.worker_id), verified: true };
    } else {
      this.rook = { worker_id: String(m.worker_id), verified: false };
    }
  }

  /** Call a cap on another node and wait for its reply. */
  async call(cap: string, args: Msg, target: string, timeoutMs = 15_000): Promise<Msg> {
    const id = randomBytes(16).toString("hex");
    const got = new Promise<Msg>((resolve, reject) => {
      const t = setTimeout(() => { this.pending.delete(id); reject(new Error(`${cap}: no reply`)); }, timeoutMs);
      this.pending.set(id, (r) => { clearTimeout(t); this.pending.delete(id); resolve(r); });
    });
    const msg: Msg = { id, cap, args, target };
    if (this.identity) msg.identity = this.identity;
    await this.send(msg);
    return got;
  }

  /** Call the hub worker "rook" (waits for its announce, which comes every ~30 s). */
  async callRook(cap: string, args: Msg): Promise<unknown> {
    const end = Date.now() + 40_000;
    while (!this.rook && Date.now() < end) await new Promise((r) => setTimeout(r, 250));
    if (!this.rook) throw new Error("hub worker 'rook' not seen on the band");
    const r = await this.call(cap, args, this.rook.worker_id);
    if (!r.ok) throw new Error(String(r.error ?? `${cap} failed`));
    return r.result;
  }

  /** Join the band through the relay and keep announcing. */
  async run(psk: string, relay: string, announceSecs = 30, facts: Record<string, unknown> = {}): Promise<void> {
    const i = relay.lastIndexOf(":");
    const link = new BandLink(psk, relay.slice(0, i), Number(relay.slice(i + 1)), (payload) => {
      let msg: unknown;
      try { msg = JSON.parse(payload.toString("utf8")); } catch { return; }
      this.onMessage(msg).catch((e) => console.error("dispatch failed:", e));
    });
    this.band = link.band.toString("hex");
    this.send = (m) => link.send(Buffer.from(JSON.stringify(m), "utf8"));
    await link.start();
    const tick = async () => {
      await this.send(this.announce(facts)).catch((e) => console.error("announce failed:", e));
      // ±20% jitter so a fleet started together does not announce in bursts.
      setTimeout(tick, announceSecs * 1000 * (0.8 + Math.random() * 0.4));
    };
    await tick();
    console.log(`worker up: id=${this.workerId} name=${this.name} caps=${this.registry.list().join(",")}`);
  }
}
