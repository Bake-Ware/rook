// Placement expressions (spec "Placement"): a tiny Python-syntax predicate
// language over node facts, parsed here without eval.
export interface NodeFacts { roles: string[]; hw: Record<string, unknown> }

type Node =
  | { k: "const"; v: unknown }
  | { k: "name"; id: string }
  | { k: "seq"; items: Node[] }
  | { k: "not"; e: Node }
  | { k: "bool"; op: "and" | "or"; es: Node[] }
  | { k: "cmp"; first: Node; ops: string[]; rest: Node[] }
  | { k: "has"; fact: string; conds: Node[]; kw: [string, Node][] };

export class PlacementError extends Error {}

const KEYWORDS = new Set(["and", "or", "not", "in", "is", "if", "else", "for", "lambda", "True",
  "False", "None", "import", "def", "return", "yield", "await", "async", "del", "pass"]);

interface Tok { t: "name" | "str" | "num" | "op"; v: string }

function tokenize(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === " " || c === "\t") { i++; continue; }
    if (/[A-Za-z_]/.test(c)) {
      let j = i + 1;
      while (j < src.length && /[A-Za-z0-9_]/.test(src[j])) j++;
      out.push({ t: "name", v: src.slice(i, j) }); i = j; continue;
    }
    if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(src[i + 1] ?? ""))) {
      const m = /^(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?/.exec(src.slice(i))!;
      out.push({ t: "num", v: m[0] }); i += m[0].length; continue;
    }
    if (c === "'" || c === '"') {
      let j = i + 1, s = "";
      while (j < src.length && src[j] !== c) {
        if (src[j] === "\\") {
          const n = src[j + 1];
          s += n === "n" ? "\n" : n === "t" ? "\t" : n; j += 2;
        } else s += src[j++];
      }
      if (j >= src.length) throw new PlacementError("unterminated string");
      out.push({ t: "str", v: s }); i = j + 1; continue;
    }
    const two = src.slice(i, i + 2);
    if (["==", "!=", "<=", ">="].includes(two)) { out.push({ t: "op", v: two }); i += 2; continue; }
    if ("()[],=<>".includes(c)) { out.push({ t: "op", v: c }); i++; continue; }
    throw new PlacementError(`unexpected ${JSON.stringify(c)}`); // . + - ; { etc.
  }
  return out;
}

class Parser {
  private i = 0;
  private toks: Tok[];
  constructor(toks: Tok[]) { this.toks = toks; }
  peek(v?: string): Tok | undefined {
    const t = this.toks[this.i];
    return v === undefined || (t && (t.t === "op" || t.t === "name") && t.v === v) ? t : undefined;
  }
  eat(v: string): boolean { if (this.peek(v)) { this.i++; return true; } return false; }
  need(v: string): void { if (!this.eat(v)) throw new PlacementError(`expected ${v}`); }
  done(): boolean { return this.i >= this.toks.length; }

  expr(): Node { return this.or(); }
  or(): Node {
    const es = [this.and()];
    while (this.eat("or")) es.push(this.and());
    return es.length > 1 ? { k: "bool", op: "or", es } : es[0];
  }
  and(): Node {
    const es = [this.not()];
    while (this.eat("and")) es.push(this.not());
    return es.length > 1 ? { k: "bool", op: "and", es } : es[0];
  }
  not(): Node { return this.eat("not") ? { k: "not", e: this.not() } : this.cmp(); }
  cmp(): Node {
    const first = this.atom();
    const ops: string[] = [], rest: Node[] = [];
    for (;;) {
      const t = this.peek();
      if (!t) break;
      let op: string | null = null;
      if (t.t === "op" && ["==", "!=", "<", "<=", ">", ">="].includes(t.v)) { this.i++; op = t.v; }
      else if (this.peek("in")) { this.i++; op = "in"; }
      else if (this.peek("not") && this.toks[this.i + 1]?.v === "in") { this.i += 2; op = "not in"; }
      else if (this.peek("is")) throw new PlacementError("'is' is not allowed");
      if (!op) break;
      ops.push(op); rest.push(this.atom());
    }
    return ops.length ? { k: "cmp", first, ops, rest } : first;
  }
  seqItems(close: string): Node[] {
    const items: Node[] = [];
    while (!this.eat(close)) {
      items.push(this.expr());
      if (!this.eat(",")) { this.need(close); break; }
    }
    return items;
  }
  atom(): Node {
    const t = this.toks[this.i++];
    if (!t) throw new PlacementError("unexpected end");
    if (t.t === "str") return { k: "const", v: t.v };
    if (t.t === "num") return { k: "const", v: Number(t.v) };
    if (t.t === "op" && t.v === "(") {
      if (this.eat(")")) return { k: "seq", items: [] };
      const first = this.expr();
      if (this.eat(")")) return first;
      this.need(",");
      return { k: "seq", items: [first, ...this.seqItems(")")] };
    }
    if (t.t === "op" && t.v === "[") return { k: "seq", items: this.seqItems("]") };
    if (t.t === "name") {
      if (t.v === "True") return { k: "const", v: true };
      if (t.v === "False") return { k: "const", v: false };
      if (t.v === "None") return { k: "const", v: null };
      if (KEYWORDS.has(t.v)) throw new PlacementError(`${t.v} is not allowed`);
      if (this.eat("(")) {
        if (t.v !== "has") throw new PlacementError("only has(...) calls are allowed");
        const first = this.toks[this.i];
        if (!first || first.t !== "str") throw new PlacementError("has() needs a fact name first");
        this.i++;
        const conds: Node[] = [], kw: [string, Node][] = [];
        while (this.eat(",")) {
          if (this.peek(")")) break;
          const n = this.toks[this.i], eq = this.toks[this.i + 1];
          if (n?.t === "name" && eq?.t === "op" && eq.v === "=") {
            this.i += 2; kw.push([n.v, this.expr()]);
          } else {
            if (kw.length) throw new PlacementError("positional argument after keyword");
            conds.push(this.expr());
          }
        }
        this.need(")");
        return { k: "has", fact: first.v, conds, kw };
      }
      return { k: "name", id: t.v };
    }
    throw new PlacementError(`unexpected ${t.v}`);
  }
}

export function compilePlacement(expr: string): Node {
  if (typeof expr !== "string" || !expr.trim()) throw new PlacementError("empty placement");
  const p = new Parser(tokenize(expr.trim()));
  const n = p.expr();
  if (!p.done()) throw new PlacementError("trailing input");
  return n;
}

// Python-like value semantics: truthiness, ==, ordering (errors on mixed types).
const truthy = (v: unknown): boolean =>
  !(v === null || v === undefined || v === false || v === 0 || v === "" ||
    (Array.isArray(v) && v.length === 0) ||
    (typeof v === "object" && !Array.isArray(v) && Object.keys(v as object).length === 0));
const asNum = (v: unknown): number | null => (typeof v === "number" ? v : typeof v === "boolean" ? Number(v) : null);

function eq(a: unknown, b: unknown): boolean {
  const x = asNum(a), y = asNum(b);
  if (x !== null && y !== null) return x === y;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => eq(v, b[i]));
  if (a && b && typeof a === "object" && typeof b === "object" && !Array.isArray(a) && !Array.isArray(b)) {
    const ka = Object.keys(a), kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => k in b && eq((a as any)[k], (b as any)[k]));
  }
  return (a ?? null) === (b ?? null);
}
function order(a: unknown, b: unknown): number {
  const x = asNum(a), y = asNum(b);
  if (x !== null && y !== null) return x - y;
  if (typeof a === "string" && typeof b === "string") return a < b ? -1 : a > b ? 1 : 0;
  throw new TypeError("unorderable types");
}
function contains(item: unknown, box: unknown): boolean {
  if (typeof box === "string") {
    if (typeof item !== "string") throw new TypeError("'in <string>' requires a string");
    return box.includes(item);
  }
  if (Array.isArray(box)) return box.some((v) => eq(item, v));
  if (box && typeof box === "object") return typeof item === "string" && item in box;
  throw new TypeError("argument is not iterable");
}
const CMP: Record<string, (a: unknown, b: unknown) => boolean> = {
  "==": eq, "!=": (a, b) => !eq(a, b),
  "<": (a, b) => a != null && b != null && order(a, b) < 0,
  "<=": (a, b) => a != null && b != null && order(a, b) <= 0,
  ">": (a, b) => a != null && b != null && order(a, b) > 0,
  ">=": (a, b) => a != null && b != null && order(a, b) >= 0,
  "in": (a, b) => b != null && contains(a, b),
  "not in": (a, b) => b != null && !contains(a, b),
};

function ev(n: Node, f: NodeFacts, scope: Record<string, unknown> | null): unknown {
  switch (n.k) {
    case "const": return n.v;
    case "seq": return n.items.map((e) => ev(e, f, scope));
    case "name": {
      if (scope) return scope[n.id] ?? null;
      if (n.id === "any" || n.id === "anywhere") return true;
      if (n.id === "is_embedded") return truthy(f.hw.embedded);
      if (n.id.startsWith("is_")) return f.roles.includes(n.id);
      return f.hw[n.id] ?? null;
    }
    case "not": return !truthy(ev(n.e, f, scope));
    case "bool":
      return n.op === "and" ? n.es.every((e) => truthy(ev(e, f, scope)))
        : n.es.some((e) => truthy(ev(e, f, scope)));
    case "cmp": {
      let left = ev(n.first, f, scope);
      for (let i = 0; i < n.ops.length; i++) {
        const right = ev(n.rest[i], f, scope);
        if (!CMP[n.ops[i]](left, right)) return false;
        left = right;
      }
      return true;
    }
    case "has": {
      const v = f.hw[n.fact];
      const want = n.kw.map(([k, e]) => [k, ev(e, f, null)] as const);
      for (const item of Array.isArray(v) ? v : [v]) {
        if (!truthy(item)) continue;
        if (!n.conds.length && !want.length) return true;
        const d = item && typeof item === "object" && !Array.isArray(item) ? item as Record<string, unknown> : {};
        if (want.every(([k, w]) => eq(d[k] ?? null, w)) && n.conds.every((c) => truthy(ev(c, f, d)))) return true;
      }
      return false;
    }
  }
}

/** Evaluate a predicate; invalid expressions and evaluation errors are false. */
export function evaluatePlacement(expr: string | null, facts: NodeFacts): boolean {
  if (expr === null) return true;
  try { return truthy(ev(compilePlacement(expr), facts, null)); } catch { return false; }
}
