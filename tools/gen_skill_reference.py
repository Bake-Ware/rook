#!/usr/bin/env python3
"""Regenerate the tool and capability tables in skills/rook/references/tools.md
from the live code, so the agent skill cannot drift from what the hub and
workers actually expose.

    python tools/gen_skill_reference.py          # rewrite the file
    python tools/gen_skill_reference.py --check  # exit 1 if it is stale

Sources:
- MCP tools: every tool the hub MCP registers (rook/band_mcp/server.py, plus the
  knowledge/task tools the knowledge and tasks hub plugins add), read from a
  throwaway server built in a temp dir. Operator tips (guidance) are not included.
- Hub caps: every cap the hub node serves as worker `rook` (hub-placed plugins
  from rook/hub/plugins plus core caps), with its risk tier, and each hub
  plugin's SKILL fragment. The MCP tools above include the ones generated from
  hub caps declared tool=True.
- Worker caps: every @capability on every Plugin class under
  rook/worker/plugins (whether or not it would load on this host), plus the
  worker core caps (caps.describe, worker.description_*, worker.plugin.*,
  customcap.*). Args use the same introspection as caps.describe.

Only the regions between the GENERATED markers are rewritten; the prose around
them is hand-maintained.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import os
import pkgutil
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "skills" / "rook" / "references" / "tools.md"
sys.path.insert(0, str(ROOT))

DESC_MAX = 110


def _first_sentence(text: str) -> str:
    text = (text or "").split("\n\nTip:")[0]
    text = " ".join(text.split("\n\n")[0].split()).replace("``", "`")
    m = re.match(r"(.+?(?<!e\.g)(?<!i\.e)(?<!etc)[.!?])(\s+[A-Z(`*]|$)", text)
    s = m.group(1) if m else text
    if len(s) > DESC_MAX:
        s = s[:DESC_MAX - 1].rstrip() + "…"
    return s.replace("|", "\\|")


def _fmt_default(v) -> str:
    if isinstance(v, str):
        return repr(v) if len(v) <= 20 else "'…'"
    if isinstance(v, bool) or v is None:
        return str(v).lower() if isinstance(v, bool) else "null"
    if isinstance(v, int) and v >= 1 << 20 and v % (1 << 20) == 0:
        return f"{v >> 20}MiB"
    return str(v)


def _fmt_param(name: str, required: bool, default, has_default: bool) -> str:
    if required:
        return name
    if not has_default or default is None:
        return f"{name}?"
    return f"{name}={_fmt_default(default)}"


# -- MCP tools -----------------------------------------------------------------

class _NoBand:
    def __init__(self) -> None:
        self.workers: dict = {}

    def attach_local(self, node) -> None:
        # Lets the hub node attach, so tools generated from hub caps are listed.
        self.workers[node.worker_id] = node.entry()

    async def call(self, *a, **k):  # pragma: no cover — never invoked
        raise RuntimeError("generator does not call the band")


def mcp_tools() -> list[tuple[str, str, str]]:
    saved = {k: os.environ.get(k) for k in ("ROOK_KNOWLEDGE", "ROOK_KNOWLEDGE_DB",
                                            "ROOK_EMBED_URL", "ROOK_MEMORY", "ROOK_MEMORY_DB")}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ROOK_KNOWLEDGE"] = "1"
        os.environ["ROOK_KNOWLEDGE_DB"] = os.path.join(tmp, "knowledge.db")
        os.environ["ROOK_MEMORY"] = "1"
        os.environ["ROOK_MEMORY_DB"] = os.path.join(tmp, "memory.db")
        os.environ.pop("ROOK_EMBED_URL", None)
        try:
            import logging
            logging.disable(logging.WARNING)
            from rook.band_mcp.server import build_server
            mcp, _ = build_server(_NoBand(), persist_path=os.path.join(tmp, "tokens.json"),
                                  journal_path=os.path.join(tmp, "journal.db"))
            tools = list(mcp._tool_manager._tools.values())
        finally:
            logging.disable(logging.NOTSET)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    rows = []
    for t in sorted(tools, key=lambda t: t.name):
        schema = t.parameters or {}
        req = set(schema.get("required") or [])
        params = []
        for name, prop in (schema.get("properties") or {}).items():
            params.append(_fmt_param(name, name in req, prop.get("default"), "default" in prop))
        rows.append((t.name, ", ".join(params), _first_sentence(t.description or "")))
    return rows


# -- hub caps ------------------------------------------------------------------

def _hub_node():
    """A throwaway hub node with every opt-in built-in plugin enabled
    (knowledge/tasks on a temp store, the chat integrations), so their caps
    and skill fragments are documented."""
    import logging
    logging.disable(logging.WARNING)
    keys = ("ROOK_KNOWLEDGE", "ROOK_KNOWLEDGE_DB", "ROOK_EMBED_URL", "ROOK_TELEGRAM", "ROOK_DISCORD",
            "ROOK_MEMORY", "ROOK_MEMORY_DB")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ROOK_KNOWLEDGE"] = "1"
            os.environ["ROOK_KNOWLEDGE_DB"] = os.path.join(tmp, "knowledge.db")
            os.environ["ROOK_MEMORY"] = "1"
            os.environ["ROOK_MEMORY_DB"] = os.path.join(tmp, "memory.db")
            os.environ.pop("ROOK_EMBED_URL", None)
            os.environ["ROOK_TELEGRAM"] = os.environ["ROOK_DISCORD"] = "1"
            from rook.hub.node import HubNode
            return HubNode(tmp, entry_points=False)
    finally:
        logging.disable(logging.NOTSET)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def hub_caps() -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """((cap, args, risk, description) rows, SKILL fragments) for the hub node."""
    node = _hub_node()
    rows = []
    for cap, d in sorted(node.host.registry.describe().items()):
        params = ", ".join(_fmt_param(p["name"], p["required"], p["default"], True)
                           for p in d["params"])
        extra = []
        if d.get("limit"):
            extra.append(f"limit={d['limit']}")
        if d.get("fields") is not None:
            extra.append("fields?")
        params = ", ".join(x for x in (params, *extra) if x)
        rows.append((cap, params, d.get("risk", "exec"), _first_sentence(d["doc"])))
    skills = [p.SKILL.strip() for p in sorted(node.host.plugins, key=lambda p: p.NAMESPACE)
              if p.SKILL]
    return rows, skills


# -- worker caps -----------------------------------------------------------------

def _describe(fns: dict) -> dict:
    from rook.worker.registry import CapabilityRegistry
    reg = CapabilityRegistry()
    for name, fn in fns.items():
        reg.register(name, fn)
    return reg.describe()


def worker_caps() -> list[tuple[str, str, str, str]]:
    """(cap, args, description, source) for every Python worker cap."""
    from rook.worker.plugin import Plugin
    import rook.worker.plugins as pkg
    fns: dict = {}
    source: dict = {}
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        classes = {c for c in vars(mod).values()
                   if inspect.isclass(c) and issubclass(c, Plugin) and c is not Plugin
                   and c.__module__ == mod.__name__}
        for cls in sorted(classes, key=lambda c: c.__name__):
            for attr, fn in inspect.getmembers(cls, callable):
                suffix = getattr(fn, "_rook_cap_suffix", None)
                if suffix is None:
                    continue
                full = cls.NAMESPACE if not suffix else f"{cls.NAMESPACE}.{suffix}"
                fns[full] = fn
                source[full] = info.name
    from rook.worker.core import Worker
    from rook.worker.admin import WorkerAdmin
    core = {"caps.describe": Worker._caps_describe,
            "worker.description_get": Worker._description_get,
            "worker.description_set": Worker._description_set,
            "worker.plugin.list": WorkerAdmin.plugin_list,
            "worker.plugin.enable": WorkerAdmin.plugin_enable,
            "worker.plugin.disable": WorkerAdmin.plugin_disable,
            "customcap.list": WorkerAdmin.customcap_list,
            "customcap.add": WorkerAdmin.customcap_add,
            "customcap.remove": WorkerAdmin.customcap_remove}
    for k, fn in core.items():
        fns[k] = fn
        source[k] = "core"
    rows = []
    for cap, d in sorted(_describe(fns).items()):
        params = ", ".join(_fmt_param(p["name"], p["required"], p["default"], True)
                           for p in d["params"])
        rows.append((cap, params, _first_sentence(d["doc"]), source[cap]))
    return rows


# -- render ----------------------------------------------------------------------

def render_mcp() -> str:
    lines = ["| Tool | Args | Does |", "|---|---|---|"]
    for name, args, desc in mcp_tools():
        lines.append(f"| `{name}` | {args or '—'} | {desc} |")
    return "\n".join(lines)


def render_hub() -> str:
    rows, skills = hub_caps()
    out = ["| Cap | Args | Risk | Does |", "|---|---|---|---|"]
    for cap, args, risk, desc in rows:
        out.append(f"| `{cap}` | {args or '—'} | {risk} | {desc} |")
    for frag in skills:
        out.extend(["", frag])
    return "\n".join(out)


def render_caps() -> str:
    by_src: dict[str, list] = {}
    for cap, args, desc, src in worker_caps():
        by_src.setdefault(src, []).append((cap, args, desc))
    out = []
    for src in sorted(by_src, key=lambda s: (s != "core", s)):
        out.append(f"**{src}**\n")
        out.append("| Cap | Args | Does |")
        out.append("|---|---|---|")
        for cap, args, desc in by_src[src]:
            out.append(f"| `{cap}` | {args or '—'} | {desc} |")
        out.append("")
    return "\n".join(out).rstrip()


SECTIONS = {"mcp-tools": render_mcp, "hub-caps": render_hub, "worker-caps": render_caps}


def render(text: str) -> str:
    for key, fn in SECTIONS.items():
        begin = f"<!-- BEGIN GENERATED: {key} -->"
        end = f"<!-- END GENERATED: {key} -->"
        pat = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
        if not pat.search(text):
            raise SystemExit(f"{TARGET}: missing markers for {key}")
        body = fn()
        text = pat.sub(lambda _m: f"{begin}\n{body}\n{end}", text)
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = ap.parse_args(argv)
    current = TARGET.read_text(encoding="utf-8")
    fresh = render(current)
    if args.check:
        if fresh != current:
            print(f"{TARGET.relative_to(ROOT)} is stale; run python tools/gen_skill_reference.py",
                  file=sys.stderr)
            return 1
        return 0
    if fresh != current:
        TARGET.write_text(fresh, encoding="utf-8")
        print(f"updated {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
