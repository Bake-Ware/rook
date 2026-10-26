"""ccmod.*: the hub's Claude Code rules, put into effect on this machine.

The hub keeps Claude Code rules (Manage > Claude Code; docs/design/ccmod.md):
a trigger (a Claude Code hook event such as PreToolUse, or Idle), a scope and
actions. It pushes each worker the rules that apply to it with
``ccmod.apply``. This plugin then:

* writes the rules to ``<worker state>/ccmod/rules.json`` and the hook
  command, :mod:`rook.worker.ccmod_hook`, beside them as
  ``rook-ccmod-hook.py``;
* adds one hook entry per event the rules need to every Claude Code
  installation here (``~/.claude/settings.json``, ``$CLAUDE_CONFIG_DIR``,
  and the folders in ``ROOK_CCMOD_CLAUDE_DIRS``), merged with the person's
  own settings and never replacing them (:mod:`rook.worker.ccmod_settings`),
  keeping a backup of the file before each write and a copy of it as it
  was before Rook first went in (removal restores that copy when the file
  is untouched since);
* takes the events the hook hands over (on the session shim's owner-only
  socket, else through ``ccmod/outbox``), notices sessions left idle, and
  serves them to the hub with ``ccmod.events``, a long poll. The hub runs the
  remote actions (posts, notifications, capabilities) under its own policy.

Pushing a config with no rules takes everything out again (``ccmod.remove``
does the same); the hook script stays while any settings file may still run
it. ``dry_run`` shows each file's diff without writing.
Workers without this plugin never see a push: the hub checks for
``ccmod.apply`` first.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import pkgutil
import re
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path

from ...core.db import atomic_write_text
from .. import ccmod_hook as hook
from .. import ccmod_settings as cs
from ..plugin import Plugin, capability, place
from ..session_mirror import state_dir

log = logging.getLogger("rook.worker.plugins.ccmod")

MAX_RULES = 200
MAX_TEXT = 4000
MAX_WAIT = 25.0
QUEUE = 500
#: Outbox events older than this are dropped unread: they wait there only
#: while the worker is down, and a stopped worker should not fire an hour of
#: stale posts when it starts.
OUTBOX_MAX_AGE = 600.0
KEEP_BACKUPS = 5
TICK = 1.0
IDLE_TICK = 15.0
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LOCAL = ("block", "inject", "statusline")


def base_dir() -> Path:
    return state_dir() / "ccmod"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


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
    the rename (:func:`atomic_write_text`), never by path afterwards: that
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


def _text(v, room=MAX_TEXT) -> str:
    return str(v or "")[:room]


def clean_config(config) -> dict:
    """The pushed config, checked and stripped to what the hook reads.
    Raises ValueError on anything malformed (the push is untrusted input)."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    if config.get("v") != 1:
        raise ValueError("unsupported config version (this worker understands v1)")
    rules = config.get("rules") or []
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise ValueError(f"rules must be a list of at most {MAX_RULES}")
    out = []
    for r in rules:
        if not isinstance(r, dict) or not _ID.match(str(r.get("id") or "")):
            raise ValueError("every rule needs an id (lowercase letters, digits, - and _)")
        trig = r.get("trigger") if isinstance(r.get("trigger"), dict) else {}
        event = trig.get("event")
        if event not in hook.HOOK_EVENTS and event != hook.IDLE:
            raise ValueError(f"rule {r['id']}: unknown event {event!r}")
        matcher = _text(trig.get("matcher"), 200)
        if matcher:
            try:
                re.compile(matcher)
            except re.error:
                raise ValueError(f"rule {r['id']}: the tool matcher is not a valid pattern") from None
        t = {"event": event}
        if matcher:
            t["matcher"] = matcher
        if trig.get("contains"):
            t["contains"] = _text(trig["contains"], 200)
        if event == hook.IDLE:
            t["minutes"] = max(1, min(int(trig.get("minutes") or 10), 1440))
        scope = r.get("scope") if isinstance(r.get("scope"), dict) else {}
        kind = scope.get("kind") if scope.get("kind") in ("project", "agent") else "all"
        local = []
        for a in (r.get("local") or [])[:10]:
            if isinstance(a, dict) and a.get("type") in LOCAL and event != hook.IDLE:
                local.append({"type": a["type"], "text": _text(a.get("text")),
                              "message": _text(a.get("message"))})
        out.append({"id": r["id"], "name": _text(r.get("name"), 120), "trigger": t,
                    "scope": {"kind": kind, "target": _text(scope.get("target"), 300)},
                    "local": local, "forward": bool(r.get("forward"))})
    return {"v": 1, "rev": int(config.get("rev") or 0), "sha": _text(config.get("sha"), 64),
            "worker": _text(config.get("worker"), 100), "band": _text(config.get("band"), 100),
            "rules": out}


def hook_spec(cfg: dict) -> tuple[dict, bool, bool]:
    """``({event: matcher|None}, statusline, idle)`` the settings need."""
    events: dict = {}
    statusline = idle = False
    for r in cfg.get("rules") or ():
        ev = r["trigger"]["event"]
        if any(a["type"] == "statusline" for a in r["local"]):
            statusline = True
        if ev == hook.IDLE:
            idle = True
            continue
        m = r["trigger"].get("matcher") or ""
        if ev not in hook.TOOL_EVENTS or not m or m == "*":
            events[ev] = None
        elif ev not in events:
            events[ev] = {m}
        elif events[ev] is not None:
            events[ev].add(m)
    if idle:
        for ev in hook.ACTIVITY:
            events[ev] = None
    return ({ev: (None if m is None else "|".join(sorted(m))) for ev, m in events.items()},
            statusline, idle)


class ClaudeCodeRules(Plugin):
    NAMESPACE = "ccmod"
    NAME = "ccmod"
    PLACEMENT = place("not is_hub and os != 'android'")
    SKILL = ("Claude Code rules from the hub (Manage > Claude Code): `ccmod.status` shows which "
             "Claude Code installations here carry Rook's managed hooks and the rules' revision. "
             "The hub pushes them with `ccmod.apply` (admin); `ccmod.remove` takes them out. "
             "Only Rook's own entries in settings.json are touched.")

    def __init__(self) -> None:
        super().__init__()
        self._worker = None
        self._cfg: dict | None = None
        self._events: collections.deque = collections.deque(maxlen=QUEUE)
        self._seq = 0
        self._boot = uuid.uuid4().hex[:12]
        self._wake = asyncio.Event()
        self._sessions: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._socket: str | None = None
        self._lock = asyncio.Lock()
        self.idle_tick = IDLE_TICK

    def bind_worker(self, worker) -> None:
        self._worker = worker

    # -- files ----------------------------------------------------------------------
    @property
    def rules_path(self) -> Path:
        return base_dir() / "rules.json"

    @property
    def script_path(self) -> Path:
        return base_dir() / hook.SCRIPT_NAME

    @property
    def manifest_path(self) -> Path:
        return base_dir() / "managed.json"

    def _read_json(self, path: Path, default):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, type(default)) else default
        except (OSError, ValueError):
            return default

    def _load_cfg(self) -> dict | None:
        data = self._read_json(self.rules_path, {})
        try:
            return clean_config(data) if data else None
        except ValueError:
            return None

    def _rules_file(self, cfg: dict) -> str:
        _events, _status, idle = hook_spec(cfg)
        doc = {**cfg, "idle": idle, "socket": self._socket or "",
               "outbox": str(base_dir() / "outbox")}
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    def _write_script(self) -> bool:
        src = pkgutil.get_data("rook.worker", "ccmod_hook.py")
        if not src:
            raise RuntimeError("ccmod_hook.py missing from the worker bundle")
        try:
            if self.script_path.read_bytes() == src:
                return False
        except OSError:
            pass
        atomic_write_text(self.script_path, src.decode("utf-8"), 0o700, fsync=False)
        return True

    # -- lifecycle --------------------------------------------------------------------
    async def start(self) -> None:
        self._cfg = await asyncio.to_thread(self._load_cfg)
        if self._cfg and self._cfg["rules"]:
            try:
                await asyncio.to_thread(self._write_script)  # a worker update ships a newer hook
                await self._ensure_socket()
            except Exception:
                log.exception("Claude Code rules: could not refresh the hook")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _shim(self):
        for p in getattr(self._worker, "plugins", None) or []:
            if hasattr(p, "serve_hooks"):
                return p
        return None

    async def _ensure_socket(self) -> None:
        shim = self._shim()
        path = await shim.serve_hooks(self.ingest) if shim is not None else None
        if path != self._socket:
            self._socket = path
            if self._cfg and self._cfg["rules"]:
                await asyncio.to_thread(atomic_write_text, self.rules_path,
                                        self._rules_file(self._cfg), 0o600)

    async def _loop(self) -> None:
        last_idle = 0.0
        while True:
            try:
                await asyncio.sleep(TICK)
                if not self._cfg or not self._cfg["rules"]:
                    continue
                for msg in await asyncio.to_thread(self._drain_outbox):
                    self.ingest(msg)
                now = time.time()
                if now - last_idle >= self.idle_tick:
                    last_idle = now
                    self.check_idle(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Claude Code rules: event loop step failed")

    def _drain_outbox(self) -> list[dict]:
        box = base_dir() / "outbox"
        try:
            names = sorted(n for n in os.listdir(box) if n.endswith(".json") and not n.startswith("."))
        except OSError:
            return []
        out = []
        now = time.time()
        for name in names[:200]:
            p = box / name
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                ts = data.get("ts") if isinstance(data, dict) else None
                if not isinstance(ts, (int, float)):
                    ts = p.stat().st_mtime
                if isinstance(data, dict) and now - ts <= OUTBOX_MAX_AGE:
                    out.append(data)
            except (OSError, ValueError):
                pass
            try:
                p.unlink()
            except OSError:
                pass
        return out

    # -- events -------------------------------------------------------------------------
    def _rule(self, rid: str) -> dict | None:
        for r in (self._cfg or {}).get("rules") or ():
            if r["id"] == rid:
                return r
        return None

    def _push(self, ev: dict) -> None:
        self._seq += 1
        self._events.append({**ev, "seq": self._seq})
        wake, self._wake = self._wake, asyncio.Event()
        wake.set()

    def ingest(self, msg: dict) -> None:
        """One event from the hook (socket or outbox). Untrusted shape: only
        known fields, clipped, and only rules this worker holds."""
        if not isinstance(msg, dict) or msg.get("op") != "hook":
            return
        event = msg.get("event")
        if event not in hook.HOOK_EVENTS:
            return
        ev = {"event": event, "ts": float(msg.get("ts") or time.time())}
        for k in ("tool", "project", "cwd", "session", "agent", "prompt", "message"):
            if msg.get(k):
                ev[k] = _text(msg[k], 500)
        session = ev.get("session") or ""
        if session and (msg.get("activity") or event in hook.ACTIVITY):
            self._activity(event, ev)
        matched = [str(i) for i in (msg.get("matched") or [])[:50]
                   if isinstance(i, str) and (self._rule(i) or {}).get("forward")]
        if matched:
            self._push({**ev, "matched": matched})

    def _activity(self, event: str, ev: dict) -> None:
        sid = ev["session"]
        if event == "SessionEnd":
            self._sessions.pop(sid, None)
            return
        s = self._sessions.setdefault(sid, {"idle_since": None, "fired": set()})
        s["ctx"] = {k: ev.get(k, "") for k in ("cwd", "project", "agent", "session")}
        if event == "Stop":
            s["idle_since"], s["fired"] = ev["ts"], set()
        elif event in ("UserPromptSubmit", "SessionStart"):
            s["idle_since"], s["fired"] = None, set()
        if len(self._sessions) > 500:  # sessions that never sent SessionEnd
            for old in sorted(self._sessions, key=lambda k: self._sessions[k]["idle_since"] or 0)[:100]:
                self._sessions.pop(old, None)

    def check_idle(self, now: float | None = None) -> int:
        """Report sessions waiting on the person past an Idle rule's minutes."""
        now = time.time() if now is None else now
        rules = [r for r in (self._cfg or {}).get("rules") or ()
                 if r["trigger"]["event"] == hook.IDLE and r["forward"]]
        n = 0
        for s in self._sessions.values():
            since = s.get("idle_since")
            if not since:
                continue
            ctx = s.get("ctx") or {}
            for r in rules:
                minutes = r["trigger"].get("minutes") or 10
                if r["id"] in s["fired"] or now - since < minutes * 60:
                    continue
                if not hook.in_scope(r.get("scope"), ctx):
                    continue
                s["fired"].add(r["id"])
                self._push({"event": hook.IDLE, "ts": now, "minutes": str(minutes),
                            **{k: v for k, v in ctx.items() if v}, "matched": [r["id"]]})
                n += 1
        return n

    # -- caps ------------------------------------------------------------------------------
    @capability("apply", risk="admin", scope="local")
    async def apply(self, config: dict, dry_run: bool = False) -> dict:
        """Put the hub's Claude Code rules into effect here (the hub pushes them).

        ``config``: {v: 1, rev, sha, worker, band, rules: [...]}. Adds Rook's
        managed hooks to each Claude Code installation's settings.json, merged
        with the person's own (a backup is kept), and writes the rules the hook
        reads. No rules: takes Rook's hooks out. ``dry_run`` returns each
        file's diff without writing."""
        cfg = clean_config(config)
        async with self._lock:
            if not dry_run and cfg["rules"]:
                await self._ensure_socket()
            return await asyncio.to_thread(self._apply, cfg, bool(dry_run))

    @capability("remove", risk="admin", scope="local")
    async def remove(self, dry_run: bool = False) -> dict:
        """Take Rook's managed hooks and status line out of every Claude Code
        installation here and delete the rules (the person's settings stay)."""
        return await self.apply({"v": 1, "rules": []}, dry_run=dry_run)

    def _plan(self, path: Path, want: tuple, entry: dict, python: str, script: str) -> dict:
        """:func:`cs.plan` for one file, plus the verbatim restore: taking
        Rook out of a file nobody changed since Rook last wrote it gives back
        the exact bytes it had before Rook first went in (the merge rewrites
        the file as JSON, so a hand-formatted file would otherwise come back
        reformatted). A file a root worker must not write through a symlink
        (:func:`foreign_link`) is refused like a file that is not JSON."""
        refused = foreign_link(path)
        if refused:
            raise cs.SettingsError(refused)
        p = cs.plan(path, want[0], python, script, want[1],
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
                         diff=cs.diff(p["old"], original, path))
            elif entry.get("file_created"):
                p.update(new="", empty=True, verbatim=True, diff=cs.diff(p["old"], "", path))
        return p

    def _pristine_path(self, path) -> Path:
        name = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        return base_dir() / "originals" / f"{name}.json"

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
        atomic_write_text(dest, text, 0o600, fsync=False)

    def _write_manifest(self, files: dict) -> None:
        if files:
            atomic_write_text(self.manifest_path, json.dumps({"files": files}, indent=2) + "\n",
                              0o600, fsync=False)
            return
        try:
            self.manifest_path.unlink()
        except OSError:
            pass

    def _apply(self, cfg: dict, dry_run: bool) -> dict:
        events, statusline, _idle = hook_spec(cfg)
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
            except (cs.SettingsError, OSError, UnicodeDecodeError) as e:
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
        new_rules = self._rules_file(cfg) if cfg["rules"] else ""
        result = {"ok": True, "dry_run": dry_run, "rev": cfg["rev"], "sha": cfg["sha"],
                  "rules": len(cfg["rules"]), "events": sorted(events), "statusline": statusline,
                  "installations": rows,
                  "rules_file": {"path": str(self.rules_path),
                                 "action": ("unchanged" if old_rules == new_rules else
                                            "remove" if not new_rules else
                                            "create" if not old_rules else "update"),
                                 "diff": cs.diff(old_rules, new_rules, self.rules_path)}}
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
                    raise cs.SettingsError(refused)
                now = real.read_bytes().decode("utf-8") if real.exists() else ""
                if now != p["old"]:
                    p = self._plan(path, want, entry, python, script)
                    row.update(action=p["action"], diff=p["diff"], notes=p["notes"])
            except (cs.SettingsError, OSError, UnicodeDecodeError) as e:
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
                atomic_write_text(real, p["new"], mode, fsync=False, owner=_owner(before))
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
            atomic_write_text(self.rules_path, new_rules, 0o600, fsync=False)
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
            shutil.rmtree(base_dir() / "status", ignore_errors=True)
        self._cfg = cfg if cfg["rules"] else None
        return result

    def _backup(self, path: Path) -> str:
        """Copy ``path`` to the worker's backups folder; keep the newest few."""
        folder = base_dir() / "backups"
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

    @capability("status", risk="read", scope="local")
    def status(self) -> dict:
        """Which Claude Code installations here carry Rook's managed hooks, and
        the revision of the rules this worker holds."""
        cfg = self._cfg or {}
        rows = []
        for d in installations():
            path = d / "settings.json"
            row = {"dir": str(d), "path": str(path), "exists": path.exists(), "managed": False}
            try:
                data = cs.load(path.read_text(encoding="utf-8")) if path.exists() else {}
                hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
                row["events"] = sorted(ev for ev, groups in hooks.items() if isinstance(groups, list)
                                       and any(cs.is_ours(i) for g in groups if isinstance(g, dict)
                                               for i in (g.get("hooks") or [])))
                row["managed"] = bool(row["events"]) or cs.is_ours(data.get(cs.STATUS))
                row["disabled"] = data.get("disableAllHooks") is True
            except (cs.SettingsError, OSError) as e:
                row["error"] = str(e)
            rows.append(row)
        return {"rev": cfg.get("rev"), "sha": cfg.get("sha"), "rules": len(cfg.get("rules") or []),
                "installations": rows, "socket": bool(self._socket), "queued": len(self._events),
                "sessions": len(self._sessions), "boot": self._boot}

    @capability("events", risk="admin", tags=("sensitive",), scope="local")
    async def events(self, after: int = 0, wait: float = 0, boot: str = "") -> dict:
        """Hook events for the hub's remote actions, those after ``after``
        (from this ``boot``; a different boot starts over). With ``wait`` the
        call holds (up to 25 s) until there is one. Returns {boot, events,
        cursor}. Admin: the events carry prompt and notification excerpts,
        and only the hub's own poller needs them."""
        after = 0 if boot != self._boot else max(0, int(after))
        wait = max(0.0, min(float(wait), MAX_WAIT))

        def pending():
            return [e for e in self._events if e["seq"] > after]

        out = pending()
        if not out and wait:
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except asyncio.TimeoutError:
                pass
            out = pending()
        out = out[:100]
        return {"boot": self._boot, "events": out,
                "cursor": out[-1]["seq"] if out else min(after, self._seq)}


PLUGIN = ClaudeCodeRules
