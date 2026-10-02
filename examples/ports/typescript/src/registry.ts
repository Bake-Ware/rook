// Capability registry (spec "Capabilities"): dot-namespaced names, declared
// params (for arg checking and caps.describe), and the core-enforced
// limit/fields output contract.
import type { Tier } from "./authz.ts";

export interface Param { name: string; required?: boolean; default?: unknown; type?: string | null }
export interface CapMeta { risk?: Tier; limit?: number; fields?: "*" | string[]; tags?: string[] }
export type Handler = (args: Record<string, unknown>) => unknown | Promise<unknown>;
export interface Cap { handler: Handler; params: Param[]; doc: string; meta?: CapMeta }

export class BadArgs extends Error {}

function parseFields(v: unknown): string[] | null {
  if (v === null || v === undefined) return null;
  let names: unknown[];
  if (typeof v === "string") names = v.split(",").map((s) => s.trim());
  else if (Array.isArray(v)) names = v;
  else throw new BadArgs("fields must be a list of names or a comma-separated string");
  const out = names.map(String).filter((s) => s.trim());
  return !out.length || out.includes("*") ? null : out;
}

function project(result: unknown, fields: string[]): unknown {
  const one = (d: unknown) => (d && typeof d === "object" && !Array.isArray(d)
    ? Object.fromEntries(fields.filter((k) => k in (d as object)).map((k) => [k, (d as any)[k]])) : d);
  if (Array.isArray(result)) return result.map(one);
  if (result && typeof result === "object") {
    const r = result as Record<string, unknown>;
    return Array.isArray(r.items) ? { ...r, items: r.items.map(one) } : one(r);
  }
  return result;
}

function trim(result: unknown, limit: number): unknown {
  if (Array.isArray(result)) return result.slice(0, limit);
  if (result && typeof result === "object") {
    const r = result as Record<string, unknown>;
    if (Array.isArray(r.items) && r.items.length > limit) {
      return { ...r, items: r.items.slice(0, limit), truncated: true, total: r.total ?? r.items.length };
    }
  }
  return result;
}

export class Registry {
  private caps = new Map<string, Cap>();

  register(name: string, cap: Cap): void {
    if (!name) throw new Error("capability name cannot be empty");
    if (this.caps.has(name)) throw new Error(`capability already registered: ${name}`);
    this.caps.set(name, cap);
  }
  has(name: string): boolean { return this.caps.has(name); }
  get(name: string): Cap | undefined { return this.caps.get(name); }
  list(): string[] { return [...this.caps.keys()].sort(); }

  /** {cap: r|w|x|a} for caps that declare a risk (announce "tiers"). */
  tiers(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const n of this.list()) {
      const r = this.caps.get(n)!.meta?.risk;
      if (r) out[n] = { read: "r", write: "w", exec: "x", admin: "a" }[r];
    }
    return out;
  }

  describe(prefix = ""): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [n, c] of this.caps) {
      if (!n.startsWith(prefix)) continue;
      const e: Record<string, unknown> = {
        doc: c.doc.split("\n\n")[0].replace(/\n/g, " ").trim(),
        params: c.params.map((p) => ({ name: p.name, required: !!p.required,
          default: p.required ? null : p.default ?? null, type: p.type ?? null })),
      };
      const m = c.meta;
      if (m?.risk) e.risk = m.risk;
      if (m?.tags?.length) e.tags = m.tags;
      if (m?.limit) e.limit = m.limit;
      if (m?.fields !== undefined) e.fields = m.fields;
      out[n] = e;
    }
    return out;
  }

  /** Check args against params (the "bad args" rule), apply limit/fields, run. */
  async call(name: string, input: Record<string, unknown>): Promise<unknown> {
    const c = this.caps.get(name);
    if (!c) throw new Error(`no such capability: ${name}`);
    const args = { ...input };
    const accepts = (p: string) => c.params.some((x) => x.name === p);
    let trimTo: number | null = null;
    let fields: string[] | null = null;
    if (c.meta?.limit) {
      if (accepts("limit")) {
        if (args.limit === undefined || args.limit === null) args.limit = c.meta.limit;
      } else {
        const asked = args.limit;
        delete args.limit;
        trimTo = typeof asked === "number" && Number.isInteger(asked) && asked > 0 ? asked : c.meta.limit;
      }
    }
    if (c.meta?.fields !== undefined && !accepts("fields")) {
      if ("fields" in args) { fields = parseFields(args.fields); delete args.fields; }
      else fields = c.meta.fields === "*" ? null : parseFields([...c.meta.fields]);
    }
    for (const k of Object.keys(args)) {
      if (!accepts(k)) throw new BadArgs(`${name}() got an unexpected keyword argument '${k}'`);
    }
    for (const p of c.params) {
      if (p.required && !(p.name in args)) throw new BadArgs(`${name}() missing required argument: '${p.name}'`);
    }
    // Only what the caller sent (after limit/fields handling); the handler
    // applies its own defaults, as a Python keyword-argument handler does.
    let result = await c.handler(args);
    if (trimTo !== null) result = trim(result, trimTo);
    if (fields !== null) result = project(result, fields);
    return result;
  }
}

// -- plugin manifest helpers ----------------------------------------------

function vtuple(v: string): number[] {
  const parts: number[] = [];
  for (const p of String(v).trim().split(".")) { if (!/^\d+$/.test(p)) break; parts.push(Number(p)); }
  if (!parts.length) throw new Error(`not a version: ${v}`);
  while (parts.length < 2) parts.push(0);
  return parts;
}
function cmpv(a: number[], b: number[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (a[i] ?? -1) - (b[i] ?? -1);
    if (d) return d;
  }
  return 0;
}

/** Whether core API version `have` satisfies a plugin's CORE_API range. */
export function coreApiCompatible(spec: string, have: string): boolean {
  try {
    const h = vtuple(have);
    const s = (spec ?? "").trim();
    if (!s) return true;
    if (/^\d/.test(s)) { const lo = vtuple(s); return cmpv(lo, h) <= 0 && cmpv(h, [lo[0] + 1, 0]) < 0; }
    for (const clause of s.split(",")) {
      const m = /^(>=|<=|==|!=|>|<)\s*([\d.]+)$/.exec(clause.trim());
      if (!m) return false;
      const c = cmpv(h, vtuple(m[2]));
      const ok = { ">=": c >= 0, "<=": c <= 0, "==": c === 0, "!=": c !== 0, ">": c > 0, "<": c < 0 }[m[1]];
      if (!ok) return false;
    }
    return true;
  } catch { return false; }
}

export interface Resource { scheme: string; target: string; path: string }

/** Parse a resource connection string: cap://<worker|any>/<cap>, http(s)://, sqlite://, file://. */
export function parseResource(url: string): Resource {
  const m = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^/?#]*)([^?#]*)/.exec(url);
  const scheme = m ? m[1].toLowerCase() : "";
  if (!["cap", "http", "https", "sqlite", "file"].includes(scheme)) throw new Error(`unsupported resource scheme in ${url}`);
  const [, , netloc, path] = m!;
  if (scheme === "cap") {
    const cap = path.replace(/^\/+/, "");
    if (!netloc || !cap) throw new Error("cap resource must look like cap://<worker|any>/<cap>");
    return { scheme, target: netloc, path: cap };
  }
  return { scheme, target: netloc, path };
}
