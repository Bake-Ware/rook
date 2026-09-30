// Permissions primitives (spec "Permissions"): risk tiers, domain-separated
// ed25519 signatures, is_hub grants, signed announces and call tickets.
import { createHash, createPublicKey, verify } from "node:crypto";
import { argsHash, canonical } from "./canonical.ts";

export const TIERS = ["read", "write", "exec", "admin"] as const;
export type Tier = typeof TIERS[number];
const FROM_LETTER: Record<string, Tier> = { r: "read", w: "write", x: "exec", a: "admin" };

export function normTier(v: unknown): Tier | null {
  if (typeof v !== "string") return null;
  const t = v.trim().toLowerCase();
  if ((TIERS as readonly string[]).includes(t)) return t as Tier;
  return FROM_LETTER[t] ?? null;
}
export const rank = (t: unknown): number => TIERS.indexOf(normTier(t) ?? "exec");
function maxTier(...ts: unknown[]): Tier {
  const known = ts.map(normTier).filter((t): t is Tier => t !== null);
  return known.length ? known.reduce((a, b) => (rank(b) > rank(a) ? b : a)) : "exec";
}

export interface TierTable { table: Record<string, { tier: Tier }>; prefixes: { prefix: string; tier: Tier }[] }

/** max(builtin, declared); cmd.* fixed; override raises, lowers only with lower=true. */
export function effectiveTier(t: TierTable, cap: string, declared?: unknown, override?: unknown,
                              lower = false): Tier {
  for (const p of t.prefixes) {
    if (cap.startsWith(p.prefix)) return lower ? (normTier(override) ?? p.tier) : maxTier(p.tier, override);
  }
  const base = t.table[cap]?.tier ?? null;
  const dec = normTier(declared);
  let tier: Tier = base === null && dec === null ? "exec" : maxTier(base, dec);
  const ov = normTier(override);
  if (ov) tier = lower ? ov : maxTier(tier, ov);
  return tier;
}

export const PREFIX = { grant: "rook-grant-v1\n", ticket: "rook-ticket-v1\n", announce: "rook-announce-v1\n" };

function rawKey(pub: string): Buffer {
  return Buffer.from(pub.startsWith("ed25519:") ? pub.slice(8) : pub, "base64");
}
export function keyId(pub: string): string {
  return createHash("sha256").update(rawKey(pub)).digest("hex").slice(0, 16);
}

export function verifySig(pub: string, prefix: string, body: object, sig: unknown): boolean {
  if (typeof sig !== "string" || !pub) return false;
  try {
    const x = rawKey(pub);
    if (x.length !== 32) return false;
    const key = createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x: x.toString("base64url") }, format: "jwk" });
    return verify(null, Buffer.from(prefix + canonical(body), "ascii"), key, Buffer.from(sig, "base64"));
  } catch { return false; }
}

type Obj = Record<string, any>;
const withoutSig = (o: Obj): Obj => Object.fromEntries(Object.entries(o).filter(([k]) => k !== "sig"));
const int = (v: unknown): number => {
  const n = typeof v === "number" ? v : typeof v === "string" && /^-?\d+$/.test(v.trim()) ? Number(v) : NaN;
  if (!Number.isFinite(n)) throw new TypeError("not an int");
  return Math.trunc(n);
};

export const GRANT_GRACE = 3600;
export const ANNOUNCE_FRESH = 90;
export const TICKET_SKEW = 300;

export interface GrantCtx { band?: string | null; now: number; revoked?: string[]; workerId?: string | null }

export function verifyGrant(g: unknown, anchors: string[], c: GrantCtx): [boolean, string] {
  if (!g || typeof g !== "object" || (g as Obj).typ !== "rook-grant" || (g as Obj).v !== 1) return [false, "not a v1 grant"];
  const grant = g as Obj;
  const root = anchors.find((a) => a && keyId(a) === grant.iss);
  if (!root) return [false, "issuer is not a trusted root"];
  if (!verifySig(root, PREFIX.grant, withoutSig(grant), grant.sig)) return [false, "bad grant signature"];
  let nbf: number, exp: number;
  try { nbf = int(grant.nbf ?? 0); exp = int(grant.exp ?? 0); } catch { return [false, "bad validity window"]; }
  if (c.now + GRANT_GRACE < nbf || c.now > exp + GRANT_GRACE) return [false, "grant expired or not yet valid"];
  if (grant.role !== "is_hub") return [false, "unknown role"];
  const bands: string[] = grant.scope?.bands ?? [];
  if (c.band != null && !bands.includes(c.band)) return [false, "band not in grant scope"];
  if ((c.revoked ?? []).includes(grant.serial)) return [false, "grant revoked"];
  const sub = grant.sub ?? {};
  if (typeof sub.key !== "string" || keyId(sub.key) !== sub.kid) return [false, "bad grant subject"];
  if (c.workerId && sub.worker_id && sub.worker_id !== c.workerId) return [false, "grant bound to another worker"];
  if (grant.role === "is_hub" && grant.name != null && grant.name !== "rook") return [false, "is_hub grant with a non-reserved name"];
  return [true, "ok"];
}

const announceBody = (m: Obj) => ({ worker_id: m.worker_id ?? null, name: m.name ?? null,
  caps: [...(m.caps ?? [])], ts: m.ts ?? null, seq: m.seq ?? null });

export function verifyAnnounce(m: Obj, grant: Obj, now: number): boolean {
  const asig = m.asig, sub = grant?.sub ?? {};
  if (!asig || typeof asig !== "object" || asig.kid !== sub.kid) return false;
  try { if (Math.abs(now - int(m.ts)) > ANNOUNCE_FRESH) return false; } catch { return false; }
  return verifySig(sub.key ?? "", PREFIX.announce, announceBody(m), asig.sig);
}

/** Roles an announce proves: a verified grant AND an announce signed by its key. */
export function heldRoles(m: Obj, anchors: string[], c: GrantCtx): string[] {
  if (!Array.isArray(m.grants)) return [];
  const out = new Set<string>();
  const wid = m.worker_id ? String(m.worker_id) : null;
  for (const g of m.grants.slice(0, 4)) {
    const [ok] = verifyGrant(g, anchors, { ...c, workerId: wid });
    if (ok && verifyAnnounce(m, g, c.now)) out.add(g.role);
  }
  return [...out].sort();
}

/** Message ids seen within the ticket window (bounded LRU). */
export class ReplayCache {
  private seen = new Map<string, number>();
  private max: number;
  private window: number;
  constructor(max = 10_000, window = 30 + TICKET_SKEW) { this.max = max; this.window = window; }
  check(id: string, now: number): boolean {
    for (const [k, t] of this.seen) {
      if (now - t > this.window || this.seen.size > this.max) this.seen.delete(k); else break;
    }
    if (this.seen.has(id)) return true;
    this.seen.set(id, now);
    if (this.seen.size > this.max) this.seen.delete(this.seen.keys().next().value!);
    return false;
  }
}

export interface TicketEnv { cap: string; target: string; msgId: string; args: unknown;
  keys: Record<string, Obj>; now: number; replay?: ReplayCache }

export function verifyTicket(t: unknown, e: TicketEnv): [boolean, string] {
  if (!t || typeof t !== "object" || (t as Obj).v !== 1) return [false, "no ticket"];
  const ticket = t as Obj;
  const grant = e.keys[ticket.kid];
  if (!grant) return [false, "unknown ticket key"];
  const body = Object.fromEntries(Object.entries(ticket).filter(([k]) => k !== "grant"));
  if (!verifySig(grant.sub?.key ?? "", PREFIX.ticket, withoutSig(body), body.sig)) return [false, "bad ticket signature"];
  if (body.t !== e.target) return [false, "ticket for another worker"];
  if (body.id !== e.msgId) return [false, "ticket for another message"];
  if (body.cap !== e.cap) return [false, "ticket for another cap"];
  if (body.ah !== argsHash(e.args)) return [false, "ticket args mismatch"];
  let iat: number, exp: number;
  try { iat = int(body.iat); exp = int(body.exp); } catch { return [false, "bad ticket window"]; }
  if (!(iat - TICKET_SKEW <= e.now && e.now <= exp + TICKET_SKEW)) return [false, "ticket expired"];
  const maxT = normTier(grant.constraints?.max_tier) ?? "admin";
  if (rank(normTier(body.tier) ?? "exec") > rank(maxT)) return [false, "ticket tier above grant constraint"];
  if (e.replay && e.replay.check(e.msgId, e.now)) return [false, "ticket replayed"];
  return [true, "ok"];
}
