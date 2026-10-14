"""Rook's managed hooks in Claude Code's ``settings.json``: merge, unmerge, plan.

Claude Code reads hooks from its user settings (``~/.claude/settings.json``,
or under ``CLAUDE_CONFIG_DIR``). That file is the person's own, so Rook's
part of it is a *managed block* in JSON terms:

* every hook Rook adds is a command that runs ``rook-ccmod-hook.py`` (the
  marker: :data:`MARK` appears in the command and nowhere else), in a group
  of its own appended after the person's groups for that event;
* the status line is Rook's only when the person has none (theirs is never
  replaced; the plan says so);
* the containers Rook had to create (``hooks``, ``hooks.<Event>``,
  ``statusLine``) are listed in a manifest the worker keeps in its own state
  folder, so taking Rook's part out removes exactly what it added and leaves
  the file as it was (an empty ``"PreToolUse": []`` the person wrote stays).

Everything else in the file is kept: other keys, their order, the person's
hooks (even in a group Rook's entry ended up in), unknown fields. The file
is rewritten as JSON with its own indent; a file that is not valid JSON is
refused, never rewritten. :func:`plan` says what would change, with a diff,
before anything is written; the worker keeps a backup of the previous file
on every write.

Stdlib only: this ships in the worker bundle.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from pathlib import Path

from .ccmod_hook import MARK, TOOL_EVENTS

HOOK_TIMEOUT = 10          # seconds Claude Code gives one hook run
MAX_FILE = 1024 * 1024
STATUS = "statusLine"


class SettingsError(ValueError):
    """The settings file cannot be merged safely (not JSON, not an object)."""


def is_ours(item) -> bool:
    return isinstance(item, dict) and MARK in str(item.get("command") or "")


def load(text: str) -> dict:
    """The settings object from a file's text (``{}`` for an empty file)."""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SettingsError(f"settings.json is not valid JSON ({e}); fix it by hand first") from None
    if not isinstance(data, dict):
        raise SettingsError("settings.json does not hold a JSON object")
    return data


def indent_of(text: str) -> int | str:
    m = re.search(r"\n([ \t]+)\S", text)
    if not m:
        return 2
    ws = m.group(1)
    return "\t" if ws.startswith("\t") else max(1, min(len(ws), 8))


def dump(data: dict, like: str = "") -> str:
    """JSON text in the style of ``like`` (its indent, line endings and final newline)."""
    nl = "\r\n" if "\r\n" in like else "\n"
    text = json.dumps(data, indent=indent_of(like), ensure_ascii=False)
    if nl != "\n":
        text = text.replace("\n", nl)
    return text + (nl if (not like or like.endswith(("\n", "\r\n"))) else "")


def unmerge(data: dict, created=None) -> dict:
    """``data`` without Rook's hooks and status line. ``created`` lists the
    containers Rook added (from the manifest); emptied ones in it are
    removed. ``None`` (no manifest): any container Rook's removal emptied
    goes."""
    out = copy.deepcopy(data)
    made = None if created is None else set(created)
    hooks = out.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept, touched = [], False
            for g in groups:
                inner = g.get("hooks") if isinstance(g, dict) else None
                if isinstance(inner, list) and any(is_ours(i) for i in inner):
                    touched = True
                    rest = [i for i in inner if not is_ours(i)]
                    if rest:
                        kept.append({**g, "hooks": rest})
                    continue
                kept.append(g)
            hooks[event] = kept
            if not kept and ((made is None and touched) or (made is not None and f"hooks.{event}" in made)):
                del hooks[event]
        if not hooks and ((made is None and isinstance(data.get("hooks"), dict) and data["hooks"])
                          or (made is not None and "hooks" in made)):
            del out["hooks"]
    if is_ours(out.get(STATUS)):
        del out[STATUS]
    return out


def command(python: str, script: str, arg: str) -> str:
    """The hook command line. Forward slashes on Windows too: Claude Code runs
    hooks through a POSIX shell there, and Python takes either."""
    def q(p: str) -> str:
        return '"' + str(p).replace("\\", "/").replace('"', "") + '"'
    return f"{q(python)} -I -S {q(script)} {arg}"


def merge(data: dict, events: dict, python: str, script: str,
          statusline: bool = False) -> tuple[dict, list[str], list[str]]:
    """Add Rook's hooks to ``data`` (which holds none of Rook's yet).

    ``events`` maps a hook event to its tool matcher (``None``/``""`` = every
    tool, or an event without matchers). Returns ``(new, created, notes)``:
    the containers this added and plain-language notes for the preview."""
    out = copy.deepcopy(data)
    created: list[str] = []
    notes: list[str] = []
    if events:
        hooks = out.get("hooks")
        if hooks is None:
            hooks = out["hooks"] = {}
            created.append("hooks")
        elif not isinstance(hooks, dict):
            raise SettingsError('"hooks" in settings.json is not an object; fix it by hand first')
        for event in sorted(events):
            groups = hooks.get(event)
            if groups is None:
                groups = hooks[event] = []
                created.append(f"hooks.{event}")
            elif not isinstance(groups, list):
                raise SettingsError(f'"hooks.{event}" in settings.json is not a list')
            group: dict = {}
            matcher = events[event]
            if event in TOOL_EVENTS and matcher and matcher != "*":
                group["matcher"] = matcher
            group["hooks"] = [{"type": "command", "command": command(python, script, event),
                               "timeout": HOOK_TIMEOUT}]
            groups.append(group)
    if statusline:
        if STATUS in out:
            notes.append("Kept your own status line: Rook's status line rules are not shown "
                         "in this installation.")
        else:
            out[STATUS] = {"type": "command", "command": command(python, script, "statusline")}
            created.append(STATUS)
    if out.get("disableAllHooks") is True:
        notes.append("Hooks are switched off in these settings (disableAllHooks): "
                     "Rook's rules will not run here until they are switched back on.")
    return out, created, notes


def diff(old: str, new: str, path) -> str:
    name = Path(str(path)).name   # the full path is shown beside the diff
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=f"{name} (now)", tofile=f"{name} (after)"))[:20000]


def plan(path: Path, events: dict, python: str, script: str, statusline: bool,
         created=None) -> dict:
    """What applying would do to one settings file, without writing:
    ``{action, old, new, created, notes, diff}``. ``action`` is ``create``,
    ``update``, ``remove``, ``unchanged`` or ``absent``. ``created``: the
    manifest's list for this file (``None``: no manifest)."""
    exists = path.exists()
    if exists and path.stat().st_size > MAX_FILE:
        raise SettingsError(f"{path} is larger than {MAX_FILE} bytes")
    old = path.read_bytes().decode("utf-8") if exists else ""
    current = load(old)
    base = unmerge(current, created)
    if events or statusline:
        new_data, made, notes = merge(base, events, python, script, statusline)
    else:
        new_data, made, notes = base, [], []
    if new_data == current:
        return {"action": "unchanged" if (events or statusline) else "absent", "old": old,
                "new": old, "created": made, "notes": notes, "diff": "",
                "exists": exists}
    if not new_data and not exists:
        return {"action": "absent", "old": old, "new": old, "created": [], "notes": notes,
                "diff": "", "exists": exists}
    new = dump(new_data, old)
    if not (events or statusline):
        action = "remove"
    else:
        action = "create" if not exists else "update"
    return {"action": action, "old": old, "new": new, "created": made, "notes": notes,
            "diff": diff(old, new, path), "exists": exists, "empty": not new_data}
