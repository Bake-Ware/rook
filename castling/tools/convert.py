"""Draft Castling (Rook 0.5) plugin manifests from today's plugins.

Reads every ``@capability`` in the worker, hub and phone plugins (by AST, no
imports), applies Bake's cap decisions (``cap-decisions.json``, a copy of
~/rook-backups/castling-caps/decisions.json on cachyrig; knowledge page
castling-caps) and writes ``castling/manifests/<plugin>/plugin.yaml``:

- binned caps are left out; renamed caps carry the old name in ``aliases``;
- tags physical/sensitive/destructive/personal become ``effects``;
- one manifest per 0.5 plugin; core, agents and workbench caps get one
  manifest per namespace (core ones ``builtin: true``);
- caps in one of the shared contracts (decision C1) that a plugin implements
  keep the contract name as their key and are listed under ``provides``;
- ``form: records`` caps become a ``records:`` entry; verbs the kit can't
  generate stay caps with ``run:``;
- everything else is ``run: scripts/<module>.py:<Class>.<method>``, pointing at
  today's function;
- ``overlays/<plugin>.yaml`` is merged last: hand-written config actions for
  the plugins the fit review says YAML covers.

Every draft is checked with ``rook.kit.manifest`` (errors fail the run). It
also rewrites the generated table in ``castling/coverage.md``.

Run from the repo root: ``python3 castling/tools/convert.py``.
"""

from __future__ import annotations

import ast
import collections
import copy
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
from rook.kit.manifest import _yaml  # noqa: E402  (overlays are read as manifests are: YAML 1.2)
from rook.kit.manifest.load import check_text  # noqa: E402
OUT = REPO / "castling" / "manifests"
COVERAGE = REPO / "castling" / "coverage.md"

ROOTS = (
    ("worker", "rook/worker/plugins"),
    ("hub", "rook/hub/plugins"),
    ("phone", "android/app/src/main/python/rook_android/plugins"),
)
EFFECT_TAGS = {"physical", "sensitive", "destructive", "personal"}
CONTRACTS = {"screen", "hid", "ui", "power", "media", "sound", "battery", "app",
             "notification", "clipboard", "voice", "shell"}
# Plugins outside the devices package (decision C3 and C6).
PACKAGE = {"claudecode": "agents", "decide": "agents"}
# Decision C3 as amended by Bake (Oct 30): decide keeps its drive loop. The
# decisions file still bins it ("the C3 rows need updating").
OVERRIDES = {
    ("hub", "decide", "decide.drive"): {"decision": "keep", "home": "plugin:decide",
                                        "form": "python", "new_names": ["decide.drive"]},
}
# The 13 caps core registers by hand (no decorator); only the survivors.
CORE_REGISTERED = {
    "caps.describe": ("read", "local", "Describe the caps on this node (hub, worker or device), with exact args."),
    "hub.plugin.enable": ("admin", "multi-machine", "Turn a plugin on."),
    "hub.plugin.disable": ("admin", "multi-machine", "Turn a plugin off."),
    "hub.plugin.reload": ("admin", "multi-machine", "Reload a plugin in place."),
    "worker.plugin.list": ("read", "local", "List the plugins on a node and their state."),
}
# Verbs the records kit generates (castling-manifest-records), plus history
# when the record keeps revisions.
RECORD_VERBS = ("list", "get", "search", "create", "update", "archive", "delete", "history")
PY_TYPES = {"str": "str", "int": "int", "float": "float", "bool": "bool", "list": "list",
            "tuple": "list", "dict": "json", "Any": "json", "bytes": "str"}
# The action-argument a split cap no longer takes (castling-caps rule 1).
SPLIT_ARGS = ("action", "kind", "op")
# Args that carry credentials get the type language's `secret` (redacted, vault refs).
SECRET_ARG = re.compile(r"(token|password|passwd|secret|api_key)$")
SETTING_WHERE = {"hub": "hub", "band": "hub", "worker": "worker", "user": "person"}


# ---- source scan ------------------------------------------------------------

def _lit(node, default=None):
    try:
        return ast.literal_eval(node)
    except Exception:
        return default


def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    return ""


def _type(ann, default) -> str:
    """Type language name for a parameter annotation (or its default)."""
    if ann is not None:
        text = ast.unparse(ann).replace("Optional[", "").replace("typing.", "")
        parts = [p.strip().strip("]'\"") for p in text.split("|")]
        parts = [p for p in parts if p not in ("None", "")]
        if len(parts) == 1:
            base = parts[0].split("[")[0]
            if base in PY_TYPES:
                return PY_TYPES[base]
        return "json"
    if default is not None and type(default).__name__ in PY_TYPES:
        return PY_TYPES[type(default).__name__]
    return "json"


def _arg_docs(doc: str) -> dict:
    """``name: text`` lines from a Google-style ``Args:`` block."""
    out, inside = {}, False
    for line in doc.splitlines():
        s = line.strip()
        if s in ("Args:", "Arguments:"):
            inside = True
            continue
        if inside:
            m = re.match(r"^(\w+)(?: \([^)]*\))?: (.+)$", s)
            if m:
                out[m.group(1)] = m.group(2)
            elif s and not line.startswith((" " * 8, "\t\t")) and s.endswith(":"):
                inside = False
    return out


def _doc(fn, meta: dict) -> str:
    if meta.get("description"):
        return " ".join(str(meta["description"]).split())
    doc = ast.get_docstring(fn) or ""
    first = doc.split("\n\n")[0]
    return " ".join(first.split())


def _args(fn) -> tuple[dict, bool]:
    """Arg schemas from a signature; True when it also takes ``**kwargs``."""
    a = fn.args
    params = a.posonlyargs + a.args
    defaults = [None] * (len(params) - len(a.defaults)) + list(a.defaults)
    pairs = list(zip(params, defaults)) + list(zip(a.kwonlyargs, a.kw_defaults))
    docs = _arg_docs(ast.get_docstring(fn) or "")
    out = {}
    for p, d in pairs:
        if p.arg in ("self", "cls") or p.arg.startswith("_"):
            continue
        val = _lit(d) if d is not None else None
        spec = {"type": "secret" if SECRET_ARG.search(p.arg) else _type(p.annotation, val)}
        if d is None:
            spec["required"] = True
        elif val is not None:
            spec["default"] = list(val) if isinstance(val, tuple) else val
        if p.arg in docs:
            spec["doc"] = docs[p.arg]
        out[p.arg] = spec
    return out, a.kwarg is not None


def _settings(cls) -> dict:
    for st in cls.body:
        if isinstance(st, ast.Assign) and any(getattr(t, "id", "") == "SETTINGS" for t in st.targets):
            out = {}
            for call in getattr(st.value, "elts", []):
                if not isinstance(call, ast.Call) or _name(call) not in ("setting", "resource"):
                    continue
                pos = [_lit(x) for x in call.args]
                kw = {k.arg: _lit(k.value) for k in call.keywords if k.arg}
                typ = pos[1] if len(pos) > 1 else kw.get("type", "str")
                if not isinstance(typ, str):  # setting("x", int, ...): a bare type
                    typ = _name(call.args[1]) if len(call.args) > 1 else "str"
                s = {"type": PY_TYPES.get(typ, "str"),
                     "where": SETTING_WHERE.get(kw.get("scope", "hub"), "hub")}
                if kw.get("secret"):
                    s["secret"] = True
                elif kw.get("default") not in (None, ""):
                    s["default"] = kw["default"]
                if kw.get("choices"):
                    s["enum"] = list(kw["choices"])
                if kw.get("apply") in ("reload", "restart"):  # live is the default
                    s["apply"] = kw["apply"]
                doc = kw.get("help") or kw.get("label")
                if doc:
                    s["doc"] = doc
                if pos and isinstance(pos[0], str):
                    out[pos[0]] = s
            return out
    return {}


def _class_attr(cls, name):
    for st in cls.body:
        if isinstance(st, ast.Assign) and any(getattr(t, "id", "") == name for t in st.targets):
            return st.value
    return None


def _module(path: Path) -> str:
    """Script name for a source file: a package's __init__ is named after the package."""
    return path.parent.name if path.stem == "__init__" else path.stem


def scan(repo: Path = REPO) -> tuple[dict, dict]:
    """``caps``: {(side, plugin, cap): info}; ``plugins``: {(side, plugin): info}."""
    caps, plugins = {}, {}
    for side, root in ROOTS:
        base = repo / root
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(base)
            plugin = rel.parts[0] if len(rel.parts) > 1 else path.stem
            if plugin.startswith("_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                ns = _lit(_class_attr(cls, "NAMESPACE"))
                methods = {f.name for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
                pinfo = plugins.setdefault((side, plugin), {
                    "files": set(), "settings": {}, "hooks": set(), "available": False,
                    "placement": None, "main": None})
                if ns:
                    pinfo["main"] = pinfo["main"] or (_module(path), cls.name)
                    pinfo["settings"].update(_settings(cls))
                    pinfo["hooks"] |= methods & {"start", "stop", "heartbeat"}
                    pinfo["available"] |= "available" in methods
                    pl = _class_attr(cls, "PLACEMENT")
                    if pl is not None:
                        pinfo["placement"] = ast.unparse(pl)
                for fn in cls.body:
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for dec in fn.decorator_list:
                        if not (isinstance(dec, ast.Call) and _name(dec) == "capability"):
                            continue
                        suffix = _lit(dec.args[0], "") if dec.args else ""
                        meta = {k.arg: _lit(k.value, "?") for k in dec.keywords if k.arg}
                        if "tier" in meta:
                            meta.setdefault("risk", meta.pop("tier"))
                        name = f"{ns}.{suffix}" if suffix else ns
                        args, open_args = _args(fn)
                        caps[(side, plugin, name)] = {
                            "file": str(path.relative_to(repo)), "module": _module(path),
                            "func": f"{cls.name}.{fn.name}", "doc": _doc(fn, meta),
                            "risk": meta.get("risk"), "scope": meta.get("scope"),
                            "tags": list(meta.get("tags") or ()), "timeout": meta.get("timeout", "-"),
                            "args": args, "open_args": open_args,
                        }
                        pinfo["files"].add(str(path.relative_to(repo)))
    return caps, plugins


# ---- decisions ----------------------------------------------------------------

def load_decisions(path: Path = HERE / "cap-decisions.json") -> list[dict]:
    rows = json.loads(path.read_text())["decisions"]
    for r in rows:
        r.update(OVERRIDES.get((r["side"], r["plugin"], r["cap"]), {}))
    return rows


def target(home: str, new_name: str) -> tuple[str, str | None]:
    """(0.5 plugin, package) for one new cap name."""
    ns = new_name.split(".")[0]
    if home.startswith("plugin:"):
        plugin = home.split(":", 1)[1]
        if ns != plugin and ns not in CONTRACTS:
            plugin = ns  # one namespace, one owner: a stray name goes to its own plugin
        return plugin, PACKAGE.get(plugin, "devices")
    return ns, home  # core | agents | workbench: one manifest per namespace


def local_key(plugin: str, new_name: str) -> str:
    """Cap key inside its manifest; "" is a cap named just the namespace (`help`)."""
    if new_name == plugin:
        return ""
    return new_name[len(plugin) + 1:] if new_name.startswith(plugin + ".") else new_name


# ---- build --------------------------------------------------------------------

def build(caps: dict, plugins: dict, decisions: list[dict]) -> tuple[dict, list[dict]]:
    """Manifests {plugin: dict} and one mapping row per old cap (for coverage)."""
    mans: dict[str, dict] = {}
    rows = []
    for d in decisions:
        key = (d["side"], d["plugin"], d["cap"])
        src = caps.get(key)
        if src is None and d["cap"] in CORE_REGISTERED:
            risk, scope, doc = CORE_REGISTERED[d["cap"]]
            src = {"file": "(core)", "module": "", "func": "", "doc": doc, "risk": risk,
                   "scope": scope, "tags": [], "timeout": "-", "args": {}, "open_args": False}
        row = {"side": d["side"], "plugin": d["plugin"], "cap": d["cap"], "decision": d["decision"],
               "form": d["form"], "new": d["new_names"], "replaced_by": d.get("replaced_by"),
               "found": src is not None, "to": []}
        rows.append(row)
        if d["decision"] == "bin" or src is None:
            continue
        for new in d["new_names"]:
            plugin, package = target(d["home"], new)
            m = mans.setdefault(plugin, {"manifest": 1, "plugin": plugin, "version": "0.5.0-draft.1"})
            m.setdefault("package", package)
            m.setdefault("_sources", set()).add(src["file"])
            m.setdefault("_from", set()).add((d["side"], d["plugin"]))
            row["to"].append(plugin)
            key_name = local_key(plugin, new)
            m.setdefault("_forms", set()).add(d["form"])
            # search needs search fields, which only the port can declare (C4):
            # until then it stays a run: cap beside the record.
            if d["form"] == "records" and key_name.split(".")[-1] in RECORD_VERBS \
                    and not key_name.endswith("search"):
                # <plugin>.<verb> uses a record named after the plugin (the kit
                # generates <plugin>.<verb> for it: manifest 1.1, C1).
                rec_name, _, verb = key_name.rpartition(".")
                rec_name = rec_name or plugin
                rec = m.setdefault("records", {}).setdefault(rec_name, {"fields": {}, "caps": []})
                if verb == "history":
                    rec["history"] = True
                if verb not in rec["caps"]:
                    rec["caps"].append(verb)
                if d["cap"] != new:
                    rec.setdefault("aliases", [])
                    if d["cap"] not in rec["aliases"]:
                        rec["aliases"].append(d["cap"])
                continue
            cap = {"doc": (src["doc"] or f"TODO: document {new}.").replace("``", "`"),
                   "risk": src["risk"] or "exec", "scope": src["scope"] or "local"}
            effects = [t for t in src["tags"] if t in EFFECT_TAGS]
            if new.endswith(".delete") and "destructive" not in effects:
                effects.append("destructive")  # rule 3: erasing for good is its own cap
            if effects:
                cap["effects"] = effects
            tags = [t for t in src["tags"] if t not in EFFECT_TAGS]
            if tags:
                cap["tags"] = tags
            args = copy.deepcopy(src["args"])
            bind = {}
            split = next((a for a in SPLIT_ARGS if a in args), None)
            if len(d["new_names"]) > 1 and split:  # the name now says what the argument did
                # The run target still takes it (B5). The value is a guess from
                # the new name, to check when the cap is ported.
                del args[split]
                bind[split] = new.rsplit(".", 1)[-1]
            if args:
                cap["args"] = args
            t = src["timeout"]
            if isinstance(t, (int, float)) and not isinstance(t, bool):
                cap["timeout"] = f"{int(t)}s"
            elif t is None:
                cap["timeout"] = "none"
            if d["cap"] != new and len(d["new_names"]) == 1:  # a split's old name has no one heir
                cap["aliases"] = [d["cap"]]
            if src["func"]:
                cap["run"] = f"scripts/{src['module']}.py:{src['func']}"
                if bind:
                    cap["bind"] = bind
            if plugin != new.split(".")[0]:  # a shared contract this plugin implements
                m.setdefault("provides", {})[new] = f"{plugin}.{new}"
            caps_ = m.setdefault("caps", {})
            if key_name in caps_:  # a merge: several old caps into one new name
                kept = caps_[key_name]
                for a in cap.get("aliases", []):
                    if a not in kept.setdefault("aliases", []):
                        kept["aliases"].append(a)
                for a, spec in cap.get("args", {}).items():
                    if a not in kept.setdefault("args", {}):  # only some callers pass it
                        kept["args"][a] = {k: v for k, v in spec.items() if k != "required"}
                continue
            caps_[key_name] = cap
    for name, m in mans.items():
        _finish(m, plugins)
    return mans, rows


def _finish(m: dict, plugins: dict) -> None:
    """Plugin-level keys from the old plugins this one draws on."""
    froms = m["_from"]
    if m["_forms"] == {"core"}:  # only builtin when every cap is core's own
        m["builtin"] = True
        for cap in m.get("caps", {}).values():
            cap.pop("run", None)
            cap.pop("bind", None)
    sides = {s for s, _ in froms}
    if sides == {"hub"}:
        m["runs_on"] = "hub"
    if sides == {"phone"}:
        m["platforms"] = ["android"]
    settings, hooks = {}, {}
    for f in sorted(froms):
        p = plugins.get(f)
        if not p:
            continue
        settings.update(p["settings"])
        if m.get("builtin"):
            continue
        mod, cls = p["main"] or (f[1], "Plugin")
        for h, k in (("start", "start"), ("stop", "stop"), ("heartbeat", "status")):
            if h in p["hooks"]:
                hooks.setdefault(k, f"scripts/{mod}.py:{cls}.{h}")
    if settings:
        m["settings"] = settings
    if hooks:
        m["hooks"] = hooks

OVERLAY_DIR = HERE / "overlays"


def merge(base: dict, over: dict) -> dict:
    """Overlay ``over`` onto ``base``: maps merge, a ``null`` deletes, the rest replaces."""
    for k, v in over.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict):
            base[k] = merge(base[k] if isinstance(base.get(k), dict) else {}, v)
        else:
            base[k] = v
    return base


def apply_overlays(mans: dict, overlay_dir: Path = OVERLAY_DIR) -> set[str]:
    done = set()
    for path in sorted(overlay_dir.glob("*.yaml")):
        name = path.stem
        if name not in mans:
            raise SystemExit(f"overlay {path.name}: no plugin {name!r} in the draft")
        over = _yaml.loads(path.read_text()) or {}
        merge(mans[name], over)
        for key, cap in (over.get("caps") or {}).items():  # exactly one of action | run
            if cap and "action" in cap:
                mans[name]["caps"][key].pop("run", None)
                mans[name]["caps"][key].pop("bind", None)
            elif cap and "run" in cap:
                mans[name]["caps"][key].pop("action", None)
        done.add(name)
    return done


# ---- check ----------------------------------------------------------------------

def check(m: dict) -> list:
    """rook.kit.manifest's issues for one draft, read back from its YAML."""
    return check_text(render(m))[1]


def lint(m: dict) -> list[str]:
    """Errors in one draft, as ``plugin: issue`` lines."""
    return [f"{m.get('plugin', '?')}: {i}" for i in check(m) if i.level == "error"]


# ---- output -------------------------------------------------------------------

ORDER = ("manifest", "plugin", "version", "title", "doc", "package", "builtin", "runs_on",
         "platforms", "replaces", "requires", "uses", "settings", "appliance", "caps",
         "provides", "records", "hooks", "check")


def render(m: dict) -> str:
    clean = {k: m[k] for k in ORDER if k in m}
    clean.update({k: v for k, v in m.items() if k not in clean and not k.startswith("_")})
    head = ["# DRAFT generated by castling/tools/convert.py; edit overlays/, not this file.",
            "# Converted from: " + ", ".join(sorted(m.get("_sources", ())))]
    body = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=100)
    return "\n".join(head) + "\n" + body


def write(mans: dict, out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*/plugin.yaml"):
        if old.parent.name not in mans:
            old.unlink()
            old.parent.rmdir()
    for name, m in sorted(mans.items()):
        d = out / name
        d.mkdir(exist_ok=True)
        (d / "plugin.yaml").write_text(render(m), encoding="utf-8")


def mapping(m: dict) -> str:
    """How a 0.5 plugin is expressed: builtin, config, run, records or a mix."""
    if m.get("builtin"):
        return "builtin"
    kinds = collections.Counter("config" if "action" in c else "run" for c in m.get("caps", {}).values())
    if m.get("records"):
        kinds["records"] += len(m["records"])
    return " + ".join(f"{k} {n}" for k, n in sorted(kinds.items()))


def coverage_table(mans: dict, rows: list[dict]) -> str:
    by_old = collections.defaultdict(list)
    for r in rows:
        by_old[(r["side"], r["plugin"])].append(r)
    lines = ["### Today's plugins -> 0.5", "",
             "| side | plugin | caps | kept | binned | streams | goes to |",
             "|---|---|---|---|---|---|---|"]
    for (side, plugin), rs in sorted(by_old.items()):
        live = [r for r in rs if r["decision"] != "bin"]
        streams = [r for r in rs if r["decision"] == "bin" and "stream" in (r["replaced_by"] or "")]
        to = sorted({t for r in live for t in r["to"]})
        missing = [r["cap"] for r in live if not r["found"]]
        note = f" (not found in source: {', '.join(missing)})" if missing else ""
        lines.append(f"| {side} | {plugin} | {len(rs)} | {len(live)} | {len(rs) - len(live)} | "
                     f"{len(streams)} | {', '.join(to) or '-'}{note} |")
    lines += ["", "### 0.5 manifests", "",
              "| plugin | package | caps | form | provides | from |", "|---|---|---|---|---|---|"]
    for name, m in sorted(mans.items()):
        froms = ", ".join(sorted(f"{s}:{p}" for s, p in m["_from"]))
        lines.append(f"| {name} | {m.get('package', '-')} | {len(m.get('caps', {}))} | {mapping(m)} | "
                     f"{', '.join(sorted(m.get('provides', {}))) or '-'} | {froms} |")
    total = collections.Counter(r["decision"] for r in rows)
    lines += ["", f"Totals: {len(rows)} caps today; keep {total['keep']}, reshape {total['reshape']}, "
              f"bin {total['bin']}; {len(mans)} manifests, "
              f"{sum(len(m.get('caps', {})) for m in mans.values())} declared caps, "
              f"{sum(len(m.get('records', {})) for m in mans.values())} record types."]
    return "\n".join(lines)


def write_coverage(table: str, path: Path = COVERAGE) -> None:
    start, end = "<!-- generated:start -->", "<!-- generated:end -->"
    text = path.read_text(encoding="utf-8") if path.exists() else f"{start}\n{end}\n"
    pre, rest = text.split(start, 1)
    post = rest.split(end, 1)[1]
    path.write_text(f"{pre}{start}\n{table}\n{end}{post}", encoding="utf-8")


def main(argv: list[str]) -> int:
    caps, plugins = scan()
    mans, rows = build(caps, plugins, load_decisions())
    apply_overlays(mans)
    issues = [(m["plugin"], i) for m in mans.values() for i in check(m)]
    errs = [f"{p}: {i}" for p, i in issues if i.level == "error"]
    for e in errs:
        print("lint:", e, file=sys.stderr)
    warned = collections.Counter(i.code for _, i in issues if i.level == "warning")
    if warned:
        print("warnings:", ", ".join(f"{c} {n}" for c, n in sorted(warned.items())), file=sys.stderr)
    write(mans)
    write_coverage(coverage_table(mans, rows))
    missing = [r["cap"] for r in rows if r["decision"] != "bin" and not r["found"]]
    print(f"{len(mans)} manifests; {len(rows)} caps read; survivors not found in source: {missing or 'none'}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
