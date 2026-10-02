// Canonical JSON (spec section "Canonical JSON"): keys sorted by Unicode code
// point, no whitespace, every non-ASCII character escaped as \uXXXX, exactly
// like Python's json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=True). Signed bodies contain no floats.
import { createHash } from "node:crypto";

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

const SHORT: Record<string, string> = {
  '"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t",
};

function str(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    const code = s.charCodeAt(i); // UTF-16 unit: surrogate pairs come out as two escapes
    if (SHORT[ch] !== undefined) out += SHORT[ch];
    else if (code < 0x20 || code > 0x7e) out += "\\u" + code.toString(16).padStart(4, "0");
    else out += ch;
  }
  return out + '"';
}

// Sort by code point, not by UTF-16 unit (JS's default string order differs
// above U+FFFF).
export function codePointCompare(a: string, b: string): number {
  const ai = a[Symbol.iterator](), bi = b[Symbol.iterator]();
  for (;;) {
    const x = ai.next(), y = bi.next();
    if (x.done || y.done) return (x.done ? 1 : 0) === (y.done ? 1 : 0) ? 0 : x.done ? -1 : 1;
    const cx = x.value.codePointAt(0)!, cy = y.value.codePointAt(0)!;
    if (cx !== cy) return cx - cy;
  }
}

function num(n: number): string {
  if (!Number.isFinite(n)) throw new TypeError("non-finite number in canonical JSON");
  if (Number.isInteger(n)) {
    if (!Number.isSafeInteger(n)) throw new TypeError("integer beyond 2^53 in canonical JSON");
    return String(n);
  }
  return String(n); // floats are not portable in signed bodies (see spec)
}

export function canonical(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return num(v);
  if (typeof v === "string") return str(v);
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    const keys = Object.keys(o).filter((k) => o[k] !== undefined).sort(codePointCompare);
    return "{" + keys.map((k) => str(k) + ":" + canonical(o[k])).join(",") + "}";
  }
  throw new TypeError(`cannot canonicalise ${typeof v}`);
}

/** base64url(sha256(canonical(args))) without padding: binds a ticket to its args. */
export function argsHash(args: unknown): string {
  const d = createHash("sha256").update(canonical(args ?? {}), "ascii").digest();
  return d.toString("base64url");
}
