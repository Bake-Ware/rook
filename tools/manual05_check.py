#!/usr/bin/env python3
"""Check the Rook 0.5 manual (docs/manual-0.5) and the plugin manuals.

    python tools/manual05_check.py        # prints each problem; exit 1 if any

Problems it finds (tests/test_manual05.py runs the same checks):

1. a page doesn't start with a ``# `` title, isn't listed in the README, or
   the README lists a page that doesn't exist;
2. a relative link (page or repo file, with or without ``#anchor``) or a
   repo path in backticks (``rook/...``, ``docs/...``) points nowhere;
3. a YAML block that starts with ``manifest: 1`` doesn't validate cleanly
   (rook.kit.manifest, no errors and no warnings);
4. a plugin (``rook/plugins/*`` or a hub builtin) has no ``docs.manual``,
   or its manual is missing, or plugins.md's "Every plugin" doesn't link it;
5. something people or agents type isn't in the manual: a ``hub.yaml`` or
   ``worker.yaml`` key, a hub or worker command, an HTTP door route, an MCP
   tool, a core preset.

The inventory is read from the source (ast and regexes), so the check
needs no running hub; only check 3 imports rook.kit.manifest.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "docs" / "manual-0.5"
PLUGIN_GLOBS = ("rook/plugins/*/plugin.yaml", "rook/core2/hub/doors/web/builtin/*/plugin.yaml")
REPO_DIRS = ("rook/", "docs/", "tests/", "tools/", "deploy/", "conformance/", "castling/")
PLUGIN_RELATIVE = {"docs/manual.md"}     # a path inside a plugin folder, not the repo's

LINK = re.compile(r"\]\(([^)\s]+)\)")
CODE_PATH = re.compile(r"`((?:%s)[^`\s<>*{}]*)`" % "|".join(map(re.escape, REPO_DIRS)))
FENCE = re.compile(r"^ *```(\w*)\n(.*?)^ *```", re.M | re.S)   # list items indent theirs


def pages() -> list[Path]:
    return sorted(MANUAL.glob("*.md"))


def anchor(heading: str) -> str:
    """GitHub's heading anchor."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    return {anchor(m[1]) for m in re.finditer(r"^#+\s+(.+)$", text, re.M)}


def _prose(text: str) -> str:
    """The text outside fenced code blocks."""
    return FENCE.sub("", text)


# -- 1, 2: pages and links ------------------------------------------------------------

def page_problems() -> list[str]:
    out = []
    readme = MANUAL / "README.md"
    listed = {m for m in LINK.findall(readme.read_text(encoding="utf-8"))
              if not m.startswith(("http", "#", "../")) and "#" not in m}
    for p in pages():
        text = p.read_text(encoding="utf-8")
        if not text.startswith("# "):
            out.append(f"{p.name}: doesn't start with a '# ' title")
        if p.name != "README.md" and p.name not in listed:
            out.append(f"{p.name}: not listed in README.md")
    out += [f"README.md: lists {m}, which doesn't exist" for m in sorted(listed)
            if not (MANUAL / m).is_file()]
    for p in pages():
        out += link_problems(p)
    return out


def link_problems(path: Path) -> list[str]:
    out = []
    prose = _prose(path.read_text(encoding="utf-8"))
    for target in LINK.findall(prose):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file, _, frag = target.partition("#")
        dest = (path.parent / file).resolve() if file else path
        if not dest.exists():
            out.append(f"{path.name}: link to {target}: no such file")
        elif frag and dest.suffix == ".md" and frag not in anchors(dest):
            out.append(f"{path.name}: link to {target}: no heading #{frag}")
    for ref in CODE_PATH.findall(prose):
        if ref not in PLUGIN_RELATIVE and not (ROOT / ref.rstrip("/")).exists():
            out.append(f"{path.name}: `{ref}` doesn't exist in the repo")
    return out


# -- 3: manifest examples -------------------------------------------------------------

def manifest_blocks() -> list[tuple[str, str]]:
    """(page, YAML text) of every block that is a whole manifest."""
    out = []
    for p in pages():
        for lang, body in FENCE.findall(p.read_text(encoding="utf-8")):
            first = next((ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")), "")
            if lang == "yaml" and first.strip() == "manifest: 1":
                out.append((p.name, body))
    return out


def example_problems() -> list[str]:
    from rook.kit.manifest.load import check_text
    out = []
    for page, text in manifest_blocks():
        data, issues = check_text(text)
        name = (data or {}).get("plugin", "?") if isinstance(data, dict) else "?"
        out += [f"{page}: example {name}: {i}" for i in issues]
    return out


# -- 4: plugin manuals ----------------------------------------------------------------

def plugins() -> list[tuple[str, Path, Path | None]]:
    """(plugin name, folder, manual path or None) for every shipped plugin."""
    import yaml
    out = []
    for glob in PLUGIN_GLOBS:
        for f in sorted(ROOT.glob(glob)):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            rel = ((data.get("docs") or {}).get("manual"))
            out.append((data.get("plugin", f.parent.name), f.parent, f.parent / rel if rel else None))
    return out


def plugin_problems() -> list[str]:
    out = []
    listing = (MANUAL / "plugins.md").read_text(encoding="utf-8").partition("## Every plugin")[2]
    linked = {(MANUAL / t).resolve() for t in LINK.findall(listing)}
    for name, folder, manual in plugins():
        where = folder.relative_to(ROOT)
        if manual is None:
            out.append(f"{where}: plugin {name} has no docs: {{manual: docs/manual.md}}")
            continue
        if not manual.is_file():
            out.append(f"{where}: plugin {name}'s manual {manual.name} is missing")
            continue
        if not manual.read_text(encoding="utf-8").startswith("# "):
            out.append(f"{manual.relative_to(ROOT)}: doesn't start with a '# ' title")
        if manual.resolve() not in linked:
            out.append(f"plugins.md: 'Every plugin' doesn't link {name} ({manual.relative_to(ROOT)})")
    return out


# -- 5: coverage ----------------------------------------------------------------------

def _dataclass_fields(path: Path, cls: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return [n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]


def _subcommands(path: Path) -> list[str]:
    return re.findall(r'add_parser\(\s*"([a-z-]+)"', path.read_text(encoding="utf-8"))


def inventory() -> dict[str, list[tuple[str, str]]]:
    """``{page: [(what, text the page must contain)]}``."""
    core = ROOT / "rook" / "core2"
    http = (core / "hub" / "doors" / "http.py").read_text(encoding="utf-8")
    routes = sorted(set(re.findall(r'\("(GET|POST)", "(/[^"]*)"\)', http)))
    opened = re.search(r"^OPEN = \{(.*?)\}", http, re.M | re.S)[1]
    routes += [("POST", p) for p in re.findall(r'"(/[a-z/-]+)":', opened)]
    routes += [("GET", "/health"), ("POST", "/holds/<hold>")]
    mcp = (core / "hub" / "doors" / "mcp.py").read_text(encoding="utf-8")
    tools = re.findall(r'\{"name": "([a-z_]+)"', mcp.partition("TOOLS = [")[2].partition("\n]\n")[0])
    import yaml
    presets = (yaml.safe_load((core / "builtin" / "core.yaml").read_text(encoding="utf-8")) or {}).get("presets") or {}
    return {
        "reference.md": [("hub.yaml key", f"`{k}`") for k in _dataclass_fields(core / "hub" / "runtime.py", "Config")]
        + [("worker.yaml key", f"`{k}`") for k in _dataclass_fields(core / "worker" / "runtime.py", "Config")]
        + [("hub command", f"rook.core2.hub --config hub.yaml {s}") for s in _subcommands(core / "hub" / "__main__.py")]
        + [("worker command", f"rook.core2.worker --config worker.yaml {s}")
           for s in _subcommands(core / "worker" / "__main__.py")]
        + [("HTTP route", f"`{m} {p}`") for m, p in routes],
        "mcp.md": [("MCP tool", f"| `{t}` |") for t in tools],
        "people.md": [("preset", f"| `{p}` |") for p in presets],
    }


def coverage_problems() -> list[str]:
    out = []
    for page, needs in inventory().items():
        text = (MANUAL / page).read_text(encoding="utf-8")
        out += [f"{page}: no {what} {needle.strip('|` ')}" for what, needle in needs if needle not in text]
    return out


def problems() -> list[str]:
    return page_problems() + example_problems() + plugin_problems() + coverage_problems()


def main() -> int:
    found = problems()
    for p in found:
        print(p)
    print(f"{len(found)} problem(s) in the 0.5 manual" if found else "the 0.5 manual is complete")
    return 1 if found else 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
