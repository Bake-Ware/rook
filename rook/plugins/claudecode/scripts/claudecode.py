"""claudecode.*: the hub's Claude Code rules, put into effect on a worker
(0.4 ccmod; docs/design/ccmod.md).

Rules are the plugin's hub record (``claudecode.create`` ... ``delete``,
admin): a trigger (a Claude Code hook event, a tool matcher, text it must
contain), a scope (all, a project, an agent) and local actions: ``block``
(deny a tool call or a prompt), ``inject`` (add context, or on Stop ask
Claude to carry on) and ``statusline``. ``claudecode.install`` on a worker
reads the enabled rules from the hub (as the caller) and:

* writes them to ``<plugin data>/rules.json`` and the hook command
  (scripts/hook.py) beside them as ``rook-ccmod-hook.py``;
* adds one hook entry per event to every Claude Code installation here
  (``~/.claude/settings.json``, ``$CLAUDE_CONFIG_DIR``, the folders in
  ``ROOK_CCMOD_CLAUDE_DIRS``), merged with the person's own settings and
  never replacing them, keeping a backup before each write and a copy of
  the file as it was before Rook first went in (removal restores that copy
  when the file is untouched since).

No enabled rules takes everything out again (``claudecode.remove`` does the
same); the hook script stays while any settings file may still run it.
``dry_run`` shows each file's diff without writing. The marker and file
names are 0.4's, so a worker that ran 0.4 ccmod is updated in place.

Not carried from 0.4: remote actions (posts, notifications, caps run by the
hub) and the Idle trigger, which need hook events to reach the hub
(castling/coverage.md N21), and band or worker scopes (a rule reaches a
worker when someone installs there).
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path

from rook.kit.ctx import KitError

_spec = importlib.util.spec_from_file_location("rook_claudecode_hook", Path(__file__).with_name("hook.py"))
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)
MARK, TOOL_EVENTS = hook.MARK, hook.TOOL_EVENTS

LOCAL = ("block", "inject", "statusline")
MAX_RULES = 200
MAX_TEXT = 4000
KEEP_BACKUPS = 5
HOOK_TIMEOUT = 10          # seconds Claude Code gives one hook run
MAX_FILE = 1024 * 1024
STATUS = "statusLine"


# -- caps --------------------------------------------------------------------------

def install(ctx, dry_run=False) -> dict:
    rules = ctx.call("claudecode.list", where={"enabled": True}, limit=500)
    if len(rules) > MAX_RULES:
        raise KitError(f"{len(rules)} enabled rules; at most {MAX_RULES} go to one worker")
    cfg = {"v": 1, "worker": socket.gethostname()[:100], "band": str(ctx.band or "")[:100],
           "rules": [hook_rule(r) for r in rules]}
    return Rules(ctx.data).apply(cfg, bool(dry_run))


def start(ctx) -> None:
    """Plugin load: a newer plugin ships a newer hook, so refresh the copy the
    settings files run while there are rules."""
    try:
        r = Rules(ctx.data)
    except KitError:                # no data dir here: nothing installed either
        return
    if r.rules_path.exists():
        r._write_script()


def remove(ctx, dry_run=False) -> dict:
    return Rules(ctx.data).apply({"v": 1, "rules": []}, bool(dry_run))


def status(ctx) -> dict:
    r = Rules(ctx.data)
    cfg = r._read_json(r.rules_path, {})
    rows = []
    for d in installations():
        path = d / "settings.json"
        row = {"dir": str(d), "path": str(path), "exists": path.exists(), "managed": False}
        try:
            data = load(path.read_text(encoding="utf-8")) if path.exists() else {}
            hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
            row["events"] = sorted(ev for ev, groups in hooks.items() if isinstance(groups, list)
                                   and any(is_ours(i) for g in groups if isinstance(g, dict)
                                           for i in (g.get("hooks") or [])))
            row["managed"] = bool(row["events"]) or is_ours(data.get(STATUS))
            row["disabled"] = data.get("disableAllHooks") is True
        except (SettingsError, OSError) as e:
            row["error"] = str(e)
        rows.append(row)
    return {"rules": len(cfg.get("rules") or []), "installations": rows}


def hook_rule(rec: dict) -> dict:
    """A rule record in the shape the hook reads."""
    t = {"event": rec["event"]}
    if rec.get("matcher"):
        t["matcher"] = str(rec["matcher"])[:200]
    if rec.get("contains"):
        t["contains"] = str(rec["contains"])[:200]
    local = [{"type": a["type"], "text": str(a.get("text") or "")[:MAX_TEXT],
              "message": str(a.get("text") or "")[:MAX_TEXT]}
             for a in rec.get("actions") or () if isinstance(a, dict) and a.get("type") in LOCAL]
    return {"id": rec["id"], "name": str(rec.get("name") or "")[:120], "trigger": t,
            "scope": {"kind": rec.get("scope") or "all", "target": str(rec.get("target") or "")[:300]},
            "local": local}


def hook_spec(cfg: dict) -> tuple[dict, bool]:
    """``({event: matcher|None}, statusline)`` the settings need."""
    events: dict = {}
    statusline = False
    for r in cfg.get("rules") or ():
        ev = r["trigger"]["event"]
        if any(a["type"] == "statusline" for a in r["local"]):
            statusline = True
        m = r["trigger"].get("matcher") or ""
        if ev not in TOOL_EVENTS or not m or m == "*":
            events[ev] = None
        elif ev not in events:
            events[ev] = {m}
        elif events[ev] is not None:
            events[ev].add(m)
    return {ev: (None if m is None else "|".join(sorted(m))) for ev, m in events.items()}, statusline


# -- settings.json: merge, unmerge, plan (0.4 rook/worker/ccmod_settings.py) ------
#
# Rook's part of the person's settings file is a managed block in JSON terms:
# every hook Rook adds is a command that runs rook-ccmod-hook.py (MARK appears
# in the command and nowhere else), in a group of its own appended after the
# person's groups; the status line is Rook's only when the person has none;
# the containers Rook had to create are listed in managed.json, so taking
# Rook's part out removes exactly what it added. A file that is not valid
# JSON is refused, never rewritten.

class SettingsError(ValueError):
    """The settings file can't be merged safely (not JSON, not an object)."""


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
    hooks through a POSIX shell there, and Python takes either.

    It ends in ``|| exit 0`` (the same in sh, bash and cmd.exe): when the
    script or the Python is gone (the worker folder deleted or moved), the
    command still exits 0 and Claude Code carries on. Without it, Python's
    status 2 for a missing script would read as "block" to Claude Code:
    every hooked tool call denied, every prompt erased, and a Stop hook that
    never lets Claude stop."""
    def q(p: str) -> str:
        return '"' + str(p).replace("\\", "/").replace('"', "") + '"'
    return f"{q(python)} -I -S {q(script)} {arg} || exit 0"


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
    ``{action, old, new, created, notes, diff, exists, had_ours}``.
    ``action`` is ``create``, ``update``, ``remove``, ``unchanged`` or
    ``absent``; ``had_ours``: the file holds Rook's entries now. ``created``:
    the manifest's list for this file (``None``: no manifest)."""
    exists = path.exists()
    if exists and path.stat().st_size > MAX_FILE:
        raise SettingsError(f"{path} is larger than {MAX_FILE} bytes")
    old = path.read_bytes().decode("utf-8") if exists else ""
    current = load(old)
    base = unmerge(current, created)
    had_ours = base != current
    if events or statusline:
        new_data, made, notes = merge(base, events, python, script, statusline)
    else:
        new_data, made, notes = base, [], []
    if new_data == current:
        return {"action": "unchanged" if (events or statusline) else "absent", "old": old,
                "new": old, "created": made, "notes": notes, "diff": "",
                "exists": exists, "had_ours": had_ours}
    if not new_data and not exists:
        return {"action": "absent", "old": old, "new": old, "created": [], "notes": notes,
                "diff": "", "exists": exists, "had_ours": had_ours}
    new = dump(new_data, old)
    if not (events or statusline):
        action = "remove"
    else:
        action = "create" if not exists else "update"
    return {"action": action, "old": old, "new": new, "created": made, "notes": notes,
            "diff": diff(old, new, path), "exists": exists, "empty": not new_data,
            "had_ours": had_ours}


# -- installations and files (0.4 rook/worker/plugins/ccmod.py) ---------------------

def _lstat_uid(path) -> int:
    return os.lstat(path).st_uid


def _stat_uid(path) -> int:
    return os.stat(path).st_uid


def foreign_link(path: Path) -> str | None:
    """Why a worker running as root must not write ``path``: it reaches,
    through a symlink (the file, or a folder above it), a file owned by
    someone other than the owner of the link and of the folder it sits in.
    User U could otherwise point their settings.json (or their ``.claude``
    folder) at a root-owned file and have a root worker rewrite it. ``None``:
    safe, or not root (a worker writes only what its own user can)."""
    if not _is_root():
        return None
    nominal = Path(os.path.abspath(path))
    real = Path(os.path.realpath(path))
    if real == nominal:
        return None
    try:
        owners = {_lstat_uid(nominal.parent)}
        try:
            owners.add(_lstat_uid(nominal))
        except FileNotFoundError:
            pass
        target_uid = _stat_uid(real if real.exists() else real.parent)
    except OSError as e:
        return f"{path} goes through a symlink that cannot be checked ({e}); not written"
    if owners != {target_uid}:
        return (f"{path} is a symlink to {real}, which belongs to another user (uid {target_uid}); "
                "a worker running as root does not write through it")
    return None


def _owner(before) -> tuple[int, int] | None:
    """Who a rewritten (or new) settings file goes back to: the user who
    owned it (or its folder). A worker running as root writing into someone's
    ``ROOK_CCMOD_CLAUDE_DIRS`` folder must not leave them a root-owned 0600
    file they cannot read. ``before`` comes from ``os.lstat`` (a symlink is
    not followed) and the owner is set on the temp file's descriptor before
    the rename (:func:`_atomic_write`), never by path afterwards: that
    folder is the user's, and a path swapped for a symlink to /etc/shadow
    between the rename and a chown would hand them that file."""
    if before is None or not hasattr(os, "chown"):
        return None
    return before.st_uid, before.st_gid


def installations(environ=None) -> list[Path]:
    """Claude Code config folders on this machine that exist."""
    env = os.environ if environ is None else environ
    dirs = [env.get("CLAUDE_CONFIG_DIR") or "~/.claude"]
    dirs += [d for d in (env.get("ROOK_CCMOD_CLAUDE_DIRS") or "").split(os.pathsep) if d.strip()]
    out, seen = [], set()
    for d in dirs:
        p = Path(os.path.expanduser(d.strip()))
        if not p.is_dir():
            continue
        key = os.path.realpath(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _atomic_write(path, text: str, mode: int = 0o600, owner: tuple[int, int] | None = None) -> None:
    """Replace ``path`` with ``text`` (a temp file beside it, then a rename);
    mode and owner are set on the temp file's descriptor, never by path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)
            if owner is not None and hasattr(os, "fchown") and _is_root():
                os.fchown(f.fileno(), *owner)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Rules:
    """The files of one worker's rules, under the plugin's data dir."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.rules_path = self.base / "rules.json"
        self.script_path = self.base / hook.SCRIPT_NAME
        self.manifest_path = self.base / "managed.json"

    def _read_json(self, path: Path, default):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, type(default)) else default
        except (OSError, ValueError):
            return default

    def _write_script(self) -> None:
        src = Path(__file__).with_name("hook.py").read_text(encoding="utf-8")
        try:
            if self.script_path.read_text(encoding="utf-8") == src:
                return
        except OSError:
            pass
        _atomic_write(self.script_path, src, 0o700)

    def _plan(self, path: Path, want: tuple, entry: dict, python: str, script: str) -> dict:
        """:func:`plan` for one file, plus the verbatim restore: taking
        Rook out of a file nobody changed since Rook last wrote it gives back
        the exact bytes it had before Rook first went in (the merge rewrites
        the file as JSON, so a hand-formatted file would otherwise come back
        reformatted). A file a root worker must not write through a symlink
        (:func:`foreign_link`) is refused like a file that is not JSON."""
        refused = foreign_link(path)
        if refused:
            raise SettingsError(refused)
        p = plan(path, want[0], python, script, want[1],
                    entry.get("created") if entry else None)
        if p["action"] == "remove" and entry.get("sha") and _sha(p["old"]) == entry["sha"]:
            original = None
            if entry.get("pristine"):
                try:
                    original = self._pristine_path(path).read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    original = None
            if original is not None:
                p.update(new=original, empty=False, verbatim=True,
                         diff=diff(p["old"], original, path))
            elif entry.get("file_created"):
                p.update(new="", empty=True, verbatim=True, diff=diff(p["old"], "", path))
        return p

    def _pristine_path(self, path) -> Path:
        name = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        return self.base / "originals" / f"{name}.json"

    def _drop_pristine(self, path) -> None:
        try:
            self._pristine_path(path).unlink()
        except OSError:
            pass

    def _save_pristine(self, path: Path, text: str) -> None:
        dest = self._pristine_path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(dest.parent, 0o700)
        except OSError:
            pass
        _atomic_write(dest, text, 0o600)

    def _write_manifest(self, files: dict) -> None:
        if files:
            _atomic_write(self.manifest_path, json.dumps({"files": files}, indent=2) + "\n",
                              0o600)
            return
        try:
            self.manifest_path.unlink()
        except OSError:
            pass

    def apply(self, cfg: dict, dry_run: bool) -> dict:
        events, statusline = hook_spec(cfg)
        python = sys.executable or "python3"
        script = str(self.script_path)
        manifest = self._read_json(self.manifest_path, {})
        files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        if not dry_run:
            # A managed file that is gone holds none of Rook's hooks any more.
            for gone in [f for f in files if not Path(f).exists()]:
                files.pop(gone, None)
                self._drop_pristine(gone)
        paths = [d / "settings.json" for d in installations()]
        known = {str(p) for p in paths}
        # A folder that was managed before but is no longer configured: take Rook out.
        stale = [Path(p) for p in files if p not in known and Path(p).exists()]
        rows, writes = [], []
        for path in paths + stale:
            entry = files.get(str(path)) or {}
            want = (events, statusline) if str(path) in known else ({}, False)
            row = {"dir": str(path.parent), "path": str(path)}
            try:
                p = self._plan(path, want, entry, python, script)
            except (SettingsError, OSError, UnicodeDecodeError) as e:
                row.update(action="error", error=str(e))
                rows.append(row)
                continue
            row.update(action=p["action"], diff=p["diff"], notes=p["notes"])
            rows.append(row)
            if p["action"] in ("create", "update", "remove"):
                writes.append((path, p, entry, row, want))
            elif not (want[0] or want[1]) and not p["had_ours"] and str(path) in files and not dry_run:
                # Taking Rook out of a file that holds none of it (the person
                # took Rook's hooks out by hand): nothing runs the script there.
                files.pop(str(path), None)
                self._drop_pristine(path)
            elif p["action"] == "unchanged" and not entry and p["created"]:
                # Rook's part is already there but the manifest was lost: record it again.
                files[str(path)] = {"created": p["created"], "file_created": False}
        old_rules = self.rules_path.read_text(encoding="utf-8") if self.rules_path.exists() else ""
        new_rules = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n" if cfg["rules"] else ""
        result = {"ok": True, "dry_run": dry_run,
                  "rules": len(cfg["rules"]), "events": sorted(events), "statusline": statusline,
                  "installations": rows,
                  "rules_file": {"path": str(self.rules_path),
                                 "action": ("unchanged" if old_rules == new_rules else
                                            "remove" if not new_rules else
                                            "create" if not old_rules else "update"),
                                 "diff": diff(old_rules, new_rules, self.rules_path)}}
        if not installations():
            result["note"] = ("No Claude Code installation found on this machine (no ~/.claude "
                              "folder); the rules are kept for when there is one.")
        if dry_run:
            return result
        for path, p, entry, row, want in writes:
            real = Path(os.path.realpath(path))
            # Claude Code (a /model or /permissions change) may have written the
            # file since it was read: plan again from what is there now.
            try:
                refused = foreign_link(path)   # again: the link may have changed since
                if refused:
                    raise SettingsError(refused)
                now = real.read_bytes().decode("utf-8") if real.exists() else ""
                if now != p["old"]:
                    p = self._plan(path, want, entry, python, script)
                    row.update(action=p["action"], diff=p["diff"], notes=p["notes"])
            except (SettingsError, OSError, UnicodeDecodeError) as e:
                row.update(action="error", error=str(e))
                continue
            if p["action"] not in ("create", "update", "remove"):
                continue
            try:
                before = os.lstat(real) if p["exists"] else os.lstat(real.parent)
            except OSError:
                before = None
            if before is not None and stat.S_ISLNK(before.st_mode):
                # ``real`` was resolved a moment ago: a symlink there now was
                # swapped in since. Its own mode (0777) and owner are not the
                # file's; refuse it like any symlink that cannot be trusted.
                row.update(action="error", error=f"{real} turned into a symlink while it was being "
                                                 "updated; not written")
                continue
            if p["exists"]:
                row["backup"] = self._backup(path)
            if p["action"] == "remove" and p.get("empty") and entry.get("file_created"):
                os.unlink(real)
                row["deleted_empty_file"] = True
            else:
                mode = before.st_mode & 0o777 if (before is not None and p["exists"]) else 0o600
                _atomic_write(real, p["new"], mode, owner=_owner(before))
            if p.get("verbatim"):
                row["restored_original"] = True
            if p["action"] == "remove":
                files.pop(str(path), None)
                self._drop_pristine(path)
                continue
            new_entry = {"created": p["created"],
                         "file_created": bool(entry.get("file_created") or not p["exists"]),
                         "sha": _sha(p["new"])}
            if p["exists"] and not p["had_ours"] and not entry.get("file_created"):
                # Rook goes into this file for the first time: keep it as it was.
                self._save_pristine(path, p["old"])
                new_entry["pristine"] = True
            elif entry.get("pristine") and entry.get("sha") == _sha(p["old"]):
                new_entry["pristine"] = True        # untouched since: the copy still holds
            else:
                self._drop_pristine(path)           # edited since: removal unmerges instead
            files[str(path)] = new_entry
        if cfg["rules"]:
            self._write_script()
            _atomic_write(self.rules_path, new_rules, 0o600)
            self._write_manifest(files)
        else:
            try:
                self.rules_path.unlink()   # the hook does nothing without its rules file
            except OSError:
                pass
            # Never delete the script while a settings file may still run it: a
            # file that could not be changed (an error above) or one still in
            # the manifest keeps Rook's hook entries until the next removal.
            held = sorted(set(files) | {r["path"] for r in rows if r.get("action") == "error"})
            if held:
                result["script_kept"] = held
            else:
                try:
                    self.script_path.unlink()
                except OSError:
                    pass
            self._write_manifest(files)
            shutil.rmtree(self.base / "status", ignore_errors=True)
        return result

    def _backup(self, path: Path) -> str:
        """Copy ``path`` to the worker's backups folder; keep the newest few."""
        folder = self.base / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        label = re.sub(r"[^A-Za-z0-9]+", "-", str(path.parent)).strip("-")[-60:] or "settings"
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1000000:06d}"
        dest = folder / f"{label}.settings.{stamp}.json"
        shutil.copyfile(os.path.realpath(path), dest)
        os.chmod(dest, 0o600)
        mine = sorted(folder.glob(f"{label}.settings.*.json"))
        for old in mine[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(dest)
