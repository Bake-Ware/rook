"""The Rook mod for Claude Code: tell an agent how to install it, or update it.

The mod is a Claude Code plugin in the repo at ``integrations/claude-code/``
(a ``/rook-bands`` pane and the ``mcp__rook__pane`` model tool). The repo root
is also a Claude Code plugin marketplace (``.claude-plugin/marketplace.json``,
marketplace name ``rook``), so Claude Code installs and updates the mod itself:

    claude plugin marketplace add Bake-Ware/rook --sparse .claude-plugin integrations/claude-code
    claude plugin install rook@rook

The hub does not serve the plugin's files. It answers ``rook_install_claude_code``:
the exact commands for the state the agent reports (``claude plugin list
--json``), and whether the installed version is behind the one this hub
ships. Wheels carry the plugin manifest at ``rook/_claude_code/plugin.json``
(see pyproject); a source checkout reads it from the repo.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = "Bake-Ware/rook"
MARKETPLACE = "rook"
PLUGIN = "rook"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
SPARSE = (".claude-plugin", "integrations/claude-code")
_PKG_ROOT = Path(__file__).resolve().parent.parent  # .../rook

ADD = f"claude plugin marketplace add {REPO} --sparse {' '.join(SPARSE)}"
INSTALL = f"claude plugin install {PLUGIN_ID}"
REFRESH = f"claude plugin marketplace update {MARKETPLACE}"
UPDATE = f"claude plugin update {PLUGIN_ID}"
LIST = "claude plugin list --json"
RELOAD = "Then run /reload-plugins in Claude Code (or restart it)."


def manifest() -> dict:
    """The mod's plugin.json as this hub ships it: the wheel copy, else the checkout."""
    for cand in (_PKG_ROOT / "_claude_code" / "plugin.json",
                 _PKG_ROOT.parent / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json"):
        if cand.is_file():
            return json.loads(cand.read_text(encoding="utf-8"))
    raise FileNotFoundError("Claude Code mod manifest not found "
                            "(expected integrations/claude-code/.claude-plugin/plugin.json)")


def _version_key(v: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", v.split("-")[0])[:3])


def _installed(text: str | list | None) -> list[dict] | None:
    """Rows named ``rook@…`` from ``claude plugin list --json`` output (text, or
    the array already parsed), or a bare version string taken as ``rook@rook``.
    None when nothing usable was given."""
    if isinstance(text, list):
        text = json.dumps(text)
    if text is None or not str(text).strip():
        return None
    text = str(text).strip()
    if re.fullmatch(r"v?\d+(\.\d+){0,2}([-+][\w.]+)?", text):
        return [{"id": PLUGIN_ID, "version": text.lstrip("v")}]
    try:
        rows = json.loads(text)
    except ValueError:
        # A pasted listing with a log line around it: take the JSON array.
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            rows = json.loads(text[start:end + 1])
        except ValueError:
            return None
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)
            and str(r.get("id", "")).split("@")[0] == PLUGIN]


def status(installed: str | list | None = None) -> dict:
    """What to run, for the state ``installed`` describes (see ``_installed``)."""
    latest = str(manifest().get("version", "0"))
    rows = _installed(installed)
    out: dict = {"ok": True, "plugin": PLUGIN_ID, "marketplace": REPO, "latest": latest,
                 "provides": "a /rook-bands pane (bands and workers, sessions across the band, "
                             "the work deck) and the mcp__rook__pane tool"}
    notes = [
        "Run the commands in a terminal on the machine where Claude Code runs; if you cannot "
        "run commands there, give them to the user.",
        "The mod talks to this hub through Claude Code's MCP server named \"rook\" or the "
        "claude.ai \"Rook\" connector; without one of them connected the pane shows an error.",
    ]
    if rows is None:
        out["status"] = "unknown"
        out["steps"] = [LIST, ADD, INSTALL]
        notes.insert(0, f"To check first, run `{LIST}` and call this tool again with its output "
                        "as installed=. Otherwise the steps install it; the marketplace add "
                        "fails harmlessly if the marketplace is already added.")
        out["notes"] = notes
        return out

    mine = next((r for r in rows if r.get("id") == PLUGIN_ID), None)
    others = [r for r in rows if r is not mine]
    if others:
        where = ", ".join(f"{r.get('id')} at {r.get('installPath', '?')}" for r in others)
        notes.append(
            f"Another plugin named rook is loaded ({where}). Two copies register the same "
            "/rook-bands command and pane tool. Ask the user before moving or disabling it: a "
            "skills-dir copy is disabled by moving its folder out of the skills directory, "
            "another install with `claude plugin uninstall <id>`.")
    if mine is None:
        out["status"] = "not_installed"
        out["steps"] = [ADD, INSTALL]
        notes.insert(0, "The marketplace add fails harmlessly if the marketplace is already "
                        f"added; run `{REFRESH}` in that case. " + RELOAD)
    else:
        have = str(mine.get("version", ""))
        out["installed"] = have
        try:
            behind = _version_key(have) < _version_key(latest)
            ahead = _version_key(have) > _version_key(latest)
        except ValueError:
            behind = ahead = False
        if behind:
            out["status"] = "outdated"
            out["steps"] = [REFRESH, UPDATE]
            notes.insert(0, RELOAD)
        else:
            out["status"] = "newer" if ahead else "current"
            out["steps"] = []
            notes.insert(0, "Nothing to do. " + (
                "The installed mod is newer than the one this hub ships; the hub may be "
                "behind the repository." if ahead else f"`{REFRESH}` then `{UPDATE}` picks up "
                "a newer release from the repository if there is one."))
        if mine.get("enabled") is False:
            notes.append(f"It is disabled: `claude plugin enable {PLUGIN_ID}`.")
    out["notes"] = notes
    return out


def register(mcp) -> None:
    """Add the ``rook_install_claude_code`` tool to ``mcp``."""

    @mcp.tool()
    async def rook_install_claude_code(installed: list | str | None = None) -> str:
        """Install or update the Rook mod for Claude Code (a pane of the band's
        workers, sessions and work deck). Returns the commands to run for the
        state ``installed`` describes: the output of ``claude plugin list
        --json`` (or just the installed version). Without it, returns the
        install steps and how to check first.
        """
        from . import envelope
        try:
            return envelope.dumps(status(installed))
        except (FileNotFoundError, ValueError) as e:
            return envelope.dumps({"ok": False, "error": str(e)})
