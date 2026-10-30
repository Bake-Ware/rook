"""Validate a parsed manifest: the JSON Schema, then the semantic checks a
schema can't express (templates, file references, presets, contracts).

Issues carry a JSON Pointer ``path`` into the manifest and a stable ``code``;
the conformance vectors match on (level, path, code), never on message text.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .types import TypeSpecError, compile_fields, compile_type, setting_spec, type_key

RISKS = ("read", "write", "exec", "admin")
EFFECTS = ("destructive", "sensitive", "personal", "physical", "sends_as", "spends",
           "persists", "taints")
# Effects a read cap shouldn't have: they change something.
_CHANGING = {"destructive", "physical", "sends_as", "spends", "persists"}
ACTION_TYPES = ("exec", "http", "tcp", "ws", "file")
RECORD_VERBS = ("list", "get", "search", "create", "update", "archive", "delete")
# delete erases for good, so a record type opts into it.
DEFAULT_RECORD_CAPS = ("list", "get", "search", "create", "update", "archive")
_READ_VERBS = {"list", "get", "search"}
# Core's four presets as shipped: the ceilings a plugin's preset grants must
# fit (None = no effects ceiling). Owners can edit presets on the hub; the
# hub enforces the live values.
CORE_PRESETS = {
    "owner": {"max_risk": "admin", "effects": None},
    "adult": {"max_risk": "admin", "effects": None},
    "child": {"max_risk": "write", "effects": []},
    "guest": {"max_risk": "write", "effects": []},
}
_SECRET_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_TEMPLATE = re.compile(r"\{\{(.*?)\}\}")
_ARG_TEMPLATE = re.compile(r"\{\{\s*args\.")


@dataclass(frozen=True, order=True)
class Issue:
    path: str  # JSON Pointer into the manifest ("" = the whole file)
    code: str
    level: str  # "error" | "warning"
    message: str

    def __str__(self):
        return f"{self.level}: {self.path or '/'}: {self.message} [{self.code}]"

    def as_dict(self) -> dict:
        return {"level": self.level, "path": self.path, "code": self.code, "message": self.message}


def pointer(parts) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


# -- the JSON Schema -----------------------------------------------------------

_HERE = Path(__file__).resolve()
_SCHEMA_PATHS = (
    _HERE.parents[2] / "_conformance" / "manifest.schema.json",  # installed wheel
    _HERE.parents[3] / "conformance" / "manifest" / "manifest.schema.json",  # checkout
)


@functools.cache
def schema() -> dict:
    for p in _SCHEMA_PATHS:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    raise FileNotFoundError("manifest.schema.json not found (conformance/manifest or rook/_conformance)")


def _close(node):
    """Strict copy of the schema: objects with known properties refuse others.
    Only used to find unknown keys, which are warnings."""
    if isinstance(node, dict):
        if "properties" in node and not {"additionalProperties", "patternProperties"} & node.keys():
            node["additionalProperties"] = False
        for v in node.values():
            _close(v)
    elif isinstance(node, list):
        for v in node:
            _close(v)
    return node


@functools.cache
def _validators():
    from jsonschema import Draft202012Validator
    return (Draft202012Validator(schema()),
            Draft202012Validator(_close(copy.deepcopy(schema()))))


def _unknown_keys(err, out: set):
    if err.validator == "additionalProperties" and err.validator_value is False \
            and isinstance(err.instance, dict):
        known = err.schema.get("properties", {})
        out.update(tuple(err.absolute_path) + (k,) for k in err.instance if k not in known)
    for sub in err.context or ():
        _unknown_keys(sub, out)


def _schema_issues(data) -> list[Issue]:
    loose, strict = _validators()
    out = [Issue(pointer(e.absolute_path), f"schema-{e.validator}", "error", e.message)
           for e in loose.iter_errors(data)]
    unknown: set = set()
    for e in strict.iter_errors(data):
        _unknown_keys(e, unknown)
    for path in unknown:
        if len(path) == 4 and path[0] == "caps" and path[2] == "action":
            out.append(Issue(pointer(path), "unknown-action", "warning",
                             f"unknown action type {path[3]!r}: this cap is unavailable"))
        else:
            out.append(Issue(pointer(path), "unknown-key", "warning",
                             f"unknown key {path[-1]!r} (ignored)"))
    return out


# -- helpers -------------------------------------------------------------------

def glob_match(glob: str, name: str) -> bool:
    """Cap/event globs: ``*`` is a whole segment and matches one or more."""
    return re.fullmatch(re.escape(glob).replace(r"\*", ".+"), name) is not None


def record_caps(data: dict) -> dict:
    """The caps a manifest's records generate: <plugin>.<record>.<verb>."""
    out = {}
    plugin = data["plugin"]
    for rec, spec in (data.get("records") or {}).items():
        for verb in spec.get("caps", DEFAULT_RECORD_CAPS):
            risk = spec.get("risk", {}).get(verb, "read" if verb in _READ_VERBS else "write")
            # TODO(castling:kit): generated caps' args come from the records kit.
            out[f"{plugin}.{rec}.{verb}"] = {
                "doc": f"{verb.capitalize()} {rec} records.", "risk": risk, "scope": "local",
                "effects": ["destructive"] if verb == "delete" else [], "on": "hub",
                "record": rec, "verb": verb,
            }
    return out


def hold_effects(cap: dict) -> set:
    """Effects for holds: an effect we don't know counts as destructive."""
    effects = set(cap.get("effects", ()))
    return effects | {"destructive"} if effects - set(EFFECTS) else effects


class _Checker:
    def __init__(self, data: dict, folder: Path | None, contracts: dict | None):
        self.data = data
        self.folder = folder
        self.contracts = {**(contracts or {}), **(data.get("contracts") or {})}
        self.plugin = data["plugin"]
        self.core = data.get("package") == "core"
        self.issues: list[Issue] = []
        self.settings = set(data.get("settings") or {})
        app = data.get("appliance")
        self.appliance = None if app is None else {"host", "port", "name"} | set(app.get("settings") or {})
        self.caps = {f"{self.plugin}.{k}": v for k, v in (data.get("caps") or {}).items()}
        self.own = set(self.caps) | set(record_caps(data))

    def add(self, level, path, code, message):
        self.issues.append(Issue(pointer(path), code, level, message))

    def error(self, path, code, message):
        self.add("error", path, code, message)

    def warn(self, path, code, message):
        self.add("warning", path, code, message)

    # -- small checks

    def type_(self, spec, path, fields=False):
        """Compile a field spec (or a name -> spec map), reporting every bad
        type and default; None if anything was wrong."""
        if fields:
            ok = [self.type_(s, path + (n,)) is not None for n, s in spec.items()]
            return compile_fields(spec) if all(ok) else None
        try:
            compiled = compile_type(spec)
        except TypeSpecError as e:
            self.error(path + e.path, "bad-type", str(e))
            return None
        if "default" in spec:
            self.instance(compiled, spec["default"], path + ("default",), "bad-default", "default")
        return compiled

    def instance(self, schema_, value, path, code, what):
        from jsonschema import Draft202012Validator
        for e in Draft202012Validator(schema_).iter_errors(value):
            self.error(path + tuple(e.absolute_path), code, f"{what}: {e.message}")

    def file(self, rel, path):
        p = PurePosixPath(rel)
        if rel.startswith("/") or "\\" in rel or ".." in p.parts or re.match(r"^[A-Za-z]:", rel):
            self.error(path, "bad-path", f"{rel!r} must be a relative path inside the plugin folder")
        elif self.folder is not None and not (self.folder / rel).is_file():
            self.error(path, "missing-file", f"{rel} does not exist")

    def run(self, ref, path):
        self.file(ref.rsplit(":", 1)[0], path)

    def callable_(self, cap) -> bool:
        return cap in self.own or any(glob_match(g, cap) for g in self.data.get("uses", ()))

    def own_cap(self, cap, path, level="error"):
        if cap not in self.own:
            self.add(level, path, "unknown-cap", f"{cap} is not a cap of this plugin")

    def templates(self, node, path, args=None, bare=()):
        if isinstance(node, dict):
            for k, v in node.items():
                self.templates(v, path + (k,), args, bare)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self.templates(v, path + (i,), args, bare)
        elif isinstance(node, str):
            for m in _TEMPLATE.finditer(node):
                problem = self.template(m.group(1).split("|", 1)[0].strip(), args, bare)
                if problem:
                    self.error(path, *problem)
            if "{{" in _TEMPLATE.sub("", node) or "}}" in _TEMPLATE.sub("", node):
                self.error(path, "template-syntax", f"unbalanced braces in {node!r}")

    def template(self, expr, args, bare):
        if expr.startswith("secret:"):
            if not _SECRET_NAME.match(expr[7:]):
                return "template-syntax", f"bad secret name in {{{{{expr}}}}}"
            return None
        parts = expr.split(".")
        head, rest = parts[0], parts[1:]
        if head == "args" and args is not None and rest:
            if rest[0] not in args:
                return "template-unknown-arg", f"{{{{{expr}}}}}: no arg {rest[0]!r} is declared"
        elif head == "settings" and rest:
            if rest[0] not in self.settings:
                return "template-unknown-setting", f"{{{{{expr}}}}}: no setting {rest[0]!r} is declared"
        elif head == "appliance" and rest and args is not None:
            if self.appliance is None:
                return "template-unknown-appliance", f"{{{{{expr}}}}}: this plugin has no appliance block"
            if rest[0] not in self.appliance:
                return "template-unknown-appliance", f"{{{{{expr}}}}}: no appliance setting {rest[0]!r}"
        elif not (expr in bare or (args is not None and (
                expr == "tmp" or expr in ("worker.os", "worker.arch", "worker.name",
                                          "plugin.dir", "plugin.data")))):
            return "template-unknown", f"{{{{{expr}}}}} is not a template variable here"
        return None

    # -- sections

    def aliases(self):
        names = set(self.own)
        for rel, cap in (self.data.get("caps") or {}).items():
            for i, alias in enumerate(cap.get("aliases") or ()):
                if alias in names:
                    self.error(("caps", rel, "aliases", i), "cap-collision",
                               f"alias {alias} is already a cap or another alias")
                names.add(alias)

    def check(self) -> list[Issue]:
        d = self.data
        self.aliases()
        for name, spec in (d.get("settings") or {}).items():
            self.setting(spec, ("settings", name))
        for rel, cap in (d.get("caps") or {}).items():
            self.cap(rel, cap, ("caps", rel))
        self.records()
        self.events()
        self.hooks()
        for name, route in (d.get("routes") or {}).items():
            path = ("routes", name)
            self.run(route["run"], path + ("run",))
            if route.get("auth") == "none":  # omitted = user
                self.warn(path + ("auth",), "route-auth-none", "this route takes requests from anyone")
            self.templates(route.get("auth"), path + ("auth",))
        if "gate" in d:
            self.run(d["gate"]["run"], ("gate", "run"))
        for name, c in (d.get("contracts") or {}).items():
            self.type_(c.get("args", {}), ("contracts", name, "args"), fields=True)
            if "result" in c:
                self.type_(c["result"], ("contracts", name, "result"))
        self.provides()
        self.appliance_()
        for i, cap in enumerate(d.get("check") or ()):
            self.own_cap(cap, ("check", i))
        self.content()
        self.presets()
        return self.issues

    def setting(self, spec, path):
        if spec.get("type") == "oauth":
            self.error(path + ("type",), "oauth-unsupported",
                       "oauth settings are reserved and not supported in manifest v1")
            return
        self.type_(setting_spec(spec), path)

    def cap(self, rel, cap, path):
        if rel.startswith(self.plugin + "."):
            self.warn(path, "cap-key-prefixed",
                      f"cap keys are relative: this cap is {self.plugin}.{rel}")
        has_action, has_run = "action" in cap, "run" in cap
        if self.data.get("builtin"):
            if has_action or has_run:
                self.error(path, "builtin-has-action", "a builtin plugin's caps have no action or run")
        elif has_action and has_run:
            self.error(path, "action-and-run", "a cap has an action or a run, not both")
        elif not (has_action or has_run):
            self.error(path, "no-action", "a cap needs an action or a run")
        if has_run:
            self.run(cap["run"], path + ("run",))
        args = cap.get("args") or {}
        compiled = self.type_(args, path + ("args",), fields=True)
        if compiled is not None:
            for i, ex in enumerate(cap.get("examples") or ()):
                self.instance(compiled, ex.get("args", {}), path + ("examples", i, "args"),
                              "bad-example", "example args")
        if "result" in cap:
            self.type_(cap["result"], path + ("result",))
        for i, eff in enumerate(cap.get("effects") or ()):
            epath = path + ("effects", i)
            if eff not in EFFECTS:
                self.warn(epath, "unknown-effect", f"unknown effect {eff!r}: treated as destructive")
            elif cap["risk"] == "read" and eff in _CHANGING:
                self.warn(epath, "effect-risk-mismatch", f"a read cap shouldn't be {eff}")
        if "undo" in cap and not self.callable_(cap["undo"]):
            self.own_cap(cap["undo"], path + ("undo",), level="warning")
        if has_action:
            self.action(cap, path + ("action",), args)

    def action(self, cap, path, args):
        action = cap["action"]
        kinds = [k for k in ACTION_TYPES if k in action]
        if len(kinds) > 1:
            self.error(path, "multiple-actions", f"one action type per cap, found {', '.join(kinds)}")
        elif not kinds and not set(action) - {"parse", "returns"}:
            self.error(path, "no-action", "the action names no action type")
        self.templates(action, path, args=args)
        exec_ = action.get("exec")
        if not exec_:
            return
        steps = [((), exec_)] if "argv" in exec_ else []
        steps += [((os,), exec_[os]) for os in ("linux", "windows", "macos", "android") if os in exec_]
        steps += [(("try", i), s) for i, s in enumerate(exec_.get("try", ()))]
        for sub, step in steps:
            first = step["argv"][0]
            if any(_ARG_TEMPLATE.search(s) for s in ([first] if isinstance(first, str) else first)) \
                    and cap["risk"] not in ("exec", "admin"):
                self.error(path + ("exec",) + sub + ("argv", 0), "argv-binary-from-arg",
                           "the program to run comes from an arg: the cap must be risk exec")

    def records(self):
        generated = record_caps(self.data)
        for name in generated:
            if name in self.caps:
                self.error(("caps", name[len(self.plugin) + 1:]), "cap-collision",
                           f"{name} is also generated by a record type")
        for rec, spec in (self.data.get("records") or {}).items():
            path = ("records", rec)
            fields = spec["fields"]
            for fname, f in fields.items():
                self.type_(f, path + ("fields", fname))
            for key in ("index", "search", "semantic"):
                for i, fname in enumerate(spec.get(key) or ()):
                    if fname not in fields:
                        self.error(path + (key, i), "unknown-field", f"no field {fname!r} in {rec}")
            if "migrations" in spec:
                self.file(spec["migrations"], path + ("migrations",))

    def events(self):
        for name, ev in (self.data.get("emits") or {}).items():
            path = ("emits", name)
            if not self.core and not name.startswith(self.plugin + "."):
                self.warn(path, "event-namespace", f"event names start with {self.plugin}.")
            self.type_(ev.get("payload", {}), path + ("payload",), fields=True)

    def hooks(self):
        hooks = self.data.get("hooks") or {}
        for key in ("start", "stop", "status"):
            if key in hooks:
                self.run(hooks[key], ("hooks", key))
        for key in ("every", "on"):
            for i, h in enumerate(hooks.get(key) or ()):
                path = ("hooks", key, i)
                if key == "on" and ("run" in h) == ("call" in h):
                    self.error(path, "hook-action", "an on: hook has a run or a call, not both")
                if "run" in h:
                    self.run(h["run"], path + ("run",))
                if "call" in h and not self.callable_(h["call"]):
                    self.error(path + ("call",), "call-not-in-uses",
                               f"{h['call']} is not this plugin's cap and not in uses")

    def provides(self):
        for contract, cap_name in (self.data.get("provides") or {}).items():
            path = ("provides", contract)
            if cap_name not in self.own:
                self.own_cap(cap_name, path)
                continue
            if contract not in self.contracts:
                self.warn(path, "unknown-contract", f"contract {contract} is not known here; not checked")
                continue
            if cap_name not in self.caps:
                continue  # a record cap: its args come from the records kit
            for problem in contract_problems(self.contracts[contract], self.caps[cap_name]):
                self.error(path, "contract-mismatch", f"{cap_name} vs {contract}: {problem}")

    def appliance_(self):
        app = self.data.get("appliance")
        if app is None:
            return
        for name, spec in (app.get("settings") or {}).items():
            self.setting(spec, ("appliance", "settings", name))
        if "pair" in app:
            self.own_cap(app["pair"], ("appliance", "pair"))
        self.templates(app.get("discover"), ("appliance", "discover"), bare=("host", "port"))

    def content(self):
        d = self.data
        for name, p in (d.get("personas") or {}).items():
            if "prompt" in p:
                self.file(p["prompt"], ("personas", name, "prompt"))
        for section in ("skills", "docs"):
            for name, rel in (d.get(section) or {}).items():
                self.file(rel, (section, name))
        for section in ("pages", "components"):
            for name, item in (d.get(section) or {}).items():
                self.file(item["file"], (section, name, "file"))
                if "props" in item:
                    self.type_(item["props"], (section, name, "props"), fields=True)
        for name, job in (d.get("jobs") or {}).items():
            path = ("jobs", name)
            if len(set(job["trigger"]) - {"filter"}) != 1:
                self.error(path + ("trigger",), "job-trigger",
                           "a trigger is exactly one of cron, every, on, manual, webhook")
            if sum(k in job for k in ("agent", "run", "call")) != 1:
                self.error(path, "job-action", "a job has exactly one of agent, run, call")
            if "run" in job:
                self.run(job["run"], path + ("run",))

    def presets(self):
        if self.core:
            return
        all_caps = {**self.caps, **record_caps(self.data)}
        for name, preset in (self.data.get("presets") or {}).items():
            path = ("presets", name)
            for key in ("max_risk", "effects", "approve_new", "expires"):
                if key in preset:
                    self.error(path + (key,), "preset-ceiling", "only core sets preset ceilings")
            ceiling = CORE_PRESETS.get(name)
            if ceiling is None:
                self.warn(path, "unknown-preset", f"core has no preset {name!r}")
            for i, g in enumerate(preset.get("grants") or ()):
                gpath = path + ("grants", i)
                if not g.startswith(self.plugin + "."):
                    self.error(gpath, "preset-foreign-grant",
                               f"a plugin's presets grant only its own caps ({self.plugin}.*)")
                    continue
                if ceiling is None:
                    continue
                over = sorted(
                    n for n, c in all_caps.items() if glob_match(g, n) and (
                        RISKS.index(c["risk"]) > RISKS.index(ceiling["max_risk"])
                        or (ceiling["effects"] is not None
                            and hold_effects(c) - set(ceiling["effects"]))))
                if over:
                    self.error(gpath, "preset-above-ceiling",
                               f"over the {name} preset's ceiling: {', '.join(over)}")


def contract_problems(contract: dict, cap: dict) -> list[str]:
    """Why ``cap`` can't stand in for ``contract`` (empty = compatible): it
    must accept every call the contract allows and return what it promises."""
    out = []
    cargs, args = contract.get("args") or {}, cap.get("args") or {}
    for name, spec in cargs.items():
        mine = args.get(name)
        if mine is None:
            out.append(f"no arg {name}")
        elif type_key(mine) != type_key(spec):
            out.append(f"arg {name} has another type")
        elif "enum" in mine and ("enum" not in spec
                                 or any(v not in mine["enum"] for v in spec["enum"])):
            out.append(f"arg {name} accepts fewer values")
    for name, spec in args.items():
        if spec.get("required") and not (cargs.get(name) or {}).get("required"):
            out.append(f"requires arg {name}, which the contract doesn't")
    if "result" in contract:
        res = cap.get("result")
        if res is None or type_key(res) != type_key(contract["result"]):
            out.append("result has another type")
        else:
            missing = set(contract["result"].get("fields", {})) - set(res.get("fields", {}))
            if missing:
                out.append(f"result lacks {', '.join(sorted(missing))}")
    return out


def validate(data, folder=None, contracts: dict | None = None) -> list[Issue]:
    """All issues in a parsed manifest, sorted by path. ``folder`` enables
    file-existence checks; ``contracts`` (name -> contract) adds contracts
    defined by other plugins (core) for checking ``provides``."""
    if not isinstance(data, dict):
        return [Issue("", "not-a-mapping", "error", "a manifest is a YAML mapping")]
    issues = _schema_issues(data)
    # Semantic checks assume the structure is right.
    if not any(i.level == "error" for i in issues):
        issues += _Checker(data, Path(folder) if folder else None, contracts).check()
    return sorted(issues)
