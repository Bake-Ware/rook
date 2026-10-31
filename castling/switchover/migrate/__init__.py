"""Move a 0.4 hub's state onto a 0.5 hub, once (castling-design: migration is
a one-off move, not a product feature). Repo tooling for the switchover:
it is not part of the shipped ``rook`` package; delete it after cutover.

    python -m castling.switchover.migrate --from <copy of the 0.4 data folder> --to <0.5 hub.yaml> [--apply]

It reads a **copy** of the 0.4 hub's data folder (:mod:`.source` refuses the
live one) and, by default, only reports what it would do. ``--apply`` writes,
with the 0.5 hub stopped (made with ``python -m rook.core2.hub --config
hub.yaml init``):

- **people**: 0.4 members and their active grants -> the config's ``users``
  (and ``member-<tier>`` presets); the config is backed up first
  (``hub.yaml.pre-migrate``). API keys and chat links don't move: the report
  lists who needs new ones;
- **trust map**: sacred machines -> the config's ``sacred``; project levels
  -> ``project.trust`` records; the rest is reported (:mod:`.convert`);
- **vault**: every secret re-sealed under the 0.5 hub's key in
  ``<data>/vault.db`` (:mod:`.vault`); values are never printed or logged;
- **memory**: the 0.4 home agent's active memories -> the house agent's
  memory (unapproved learned procedures -> its notepad);
- **knowledge, projects, tasks, concepts** -> workbench records, by the
  0.4 workbench importer beside this package (``castling/switchover/import_04.py``);
- **chat rooms and huddles** -> the hub's streams, full history
  (:mod:`.rooms`); **jobs** -> disabled 0.5 jobs holding their 0.4
  definition, **change records** -> ``change`` records (:mod:`.records`);
- **the whole copy** (every store and file, the journal too) -> ``<data>/archive/0.4/``,
  byte for byte and read-only (:mod:`.archive`); the journal is never
  replayed into the 0.5 chains;
- **workers**: a re-enrollment checklist (every machine enrolls again).

``python -m castling.switchover.migrate --verify`` then checks the move
(:mod:`.verify`): every 0.4 artifact moved, archived, or left out for a
reason a castling-* decision gives.

Every step skips what an earlier run wrote, so a re-run after a fix only adds
what is missing. The report (``<data>/migrate/report.md`` and ``.json`` after
``--apply``) names every secret, never its value.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import itertools
import json
import shutil
import socket
import sqlite3
from pathlib import Path

import yaml

from rook.core2.hub.runtime import _key, _write_private, home_band, load_config
from rook.kit.ctx import Caller
from rook.kit.manifest import load as load_manifest
from rook.kit.records import RecordStore

from . import archive, convert, records, rooms, vault
from .source import NotACopy, Source, _tables

IMPORTER = Path(__file__).resolve().parent.parent / "import_04.py"
PLUGINS = Path(__file__).resolve().parents[3] / "rook" / "plugins"
CARRIED = ("huddle", "jobs", "changes")            # plugin folders whose records the move writes
HOUSE = "house"                                   # the house agent (rook/plugins/agents)
AGENTS = Path(__file__).resolve().parents[3] / "rook" / "plugins" / "agents"   # its records: memory, note

__all__ = ["MigrateError", "NotACopy", "migrate", "render"]


class MigrateError(ValueError):
    """The migration can't run; the text says what to do."""


def migrate(src: str | Path, config: str | Path, apply: bool = False, rename: dict | None = None) -> dict:
    """Plan (and with ``apply``, write) the move; returns the report."""
    cfg_path = Path(config).resolve()
    if not cfg_path.exists():
        raise MigrateError(f"{cfg_path}: no 0.5 hub config. Make the hub first: "
                           f"python -m rook.core2.hub --config {cfg_path} init --owner <you> --relay <host:port>")
    try:
        cfg = load_config(cfg_path)
        band = home_band(cfg)
    except (OSError, ValueError) as e:
        raise MigrateError(f"{cfg_path}: {e}") from None
    if apply:
        _check_stopped(cfg)
    source = Source(src)
    if not any(source.paths.values()):
        raise MigrateError(f"{source.root}: no 0.4 hub stores in it (access.db, trust.json, vault.db, ...)")
    try:
        return _run(source, cfg, cfg_path, band, apply, rename or {})
    finally:
        source.close()


def _run(source: Source, cfg, cfg_path: Path, band: str, apply: bool, rename: dict) -> dict:
    notes: list[str] = []
    report: dict = {"from": str(source.root), "to": str(cfg_path), "applied": apply, "found": source.found(),
                    "notes": notes}
    workers = source.workers()
    names = {**{w["name"].casefold(): w["name"] for w in workers}, **source.worker_names()}
    users, presets, mapping, more = convert.people(source.people(), cfg.users, names, len(source.bands()) > 1,
                                                    rename)
    notes += more
    for s in source.spaces():
        notes.append(f"chat space {s['platform']}:{s['space_id']} ({s['name']}) isn't carried over: "
                     "share it again once the 0.5 chat plugin runs")
    sacred, levels, more = convert.trust(source.trust(), [w["name"] for w in workers])
    sacred = sorted(set(cfg.sacred) | set(sacred), key=str.casefold)
    notes += more
    secrets, more = source.secrets()
    notes += more
    mems, jots, skipped = convert.memory(source.memories(), {m["from"]: m["name"] for m in mapping})
    hub_url = (getattr(cfg, "enroll", None) or {}).get("url") or \
        f"http://{(cfg.doors.get('http') or {}).get('bind', '127.0.0.1:8740')}"
    report.update(
        people=mapping, presets={k: v for k, v in presets.items() if k not in cfg.presets}, sacred=sacred,
        secrets=[{"name": s["name"], "description": s["description"]} for s in secrets],
        memory={"memory": len(mems), "notepad": len(jots), "skipped": skipped},
        journal=source.journal_stats(), workers=convert.checklist(workers, sacred, hub_url))
    if apply:
        cfg.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        report["config_backup"] = _write_config(cfg_path, users, report["presets"], sacred)
    report["workbench"], new_ids = _workbench(source, cfg, band, apply, levels, notes)
    owner = next((p["name"] for p in mapping if p["preset"] == "owner"),
                 next((n for n, u in cfg.users.items() if (u or {}).get("preset") == "owner"), "owner"))
    _carry(source, cfg, band, apply, new_ids, owner, report)
    if apply:
        (cfg.data / "keys").mkdir(exist_ok=True, mode=0o700)
        _key(cfg.data / "keys" / "root.key")      # as the hub's first start would: the vault's key is derived from it
        report["secrets_written"] = vault.put(cfg.data, secrets)
        report["memory"]["written"] = _write_memory(cfg.data, band, mems, jots)
        report["archive"] = archive.put(source.root, source.files(), cfg.data)
        if report["journal"]:
            rel = source.paths["journal"].relative_to(source.root).as_posix()
            report["journal"]["archive"] = {"path": str(cfg.data / archive.DIR / rel),
                                            "sha256": archive.sha256(cfg.data / archive.DIR / rel)}
        out = cfg.data / "migrate"
        out.mkdir(exist_ok=True, mode=0o700)
        _write_private(out / "report.json", json.dumps(report, indent=2) + "\n")
        _write_private(out / "report.md", render(report))
    return report


# -- writing -----------------------------------------------------------------

def _check_stopped(cfg) -> None:
    """--apply writes the hub's stores: refuse while one of its doors answers."""
    for name, opts in (cfg.doors or {}).items():
        host, _, port = str((opts or {}).get("bind") or "").rpartition(":")
        if not port.isdigit():
            continue
        host = "127.0.0.1" if host in ("", "0.0.0.0", "::", "[::]") else host.strip("[]")
        try:
            socket.create_connection((host, int(port)), timeout=1).close()
        except OSError:
            continue
        raise MigrateError(f"the 0.5 hub answers on {host}:{port} ({name} door): stop it first")


def _write_config(path: Path, users: dict, presets: dict, sacred: list) -> str | None:
    """Merge people, presets and sacred machines into the hub's YAML; the old
    file is kept beside it. Returns the backup's path (None: nothing changed)."""
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    new = {**raw, "users": users, "presets": {**presets, **(raw.get("presets") or {})}}
    if sacred:
        new["sacred"] = sacred
    if not new["presets"]:
        new.pop("presets")
    if new == raw:
        return None
    backup = path.with_name(path.name + ".pre-migrate")
    if backup.exists():
        backup = path.with_name(f"{path.name}.pre-migrate-{_dt.datetime.now():%Y%m%d-%H%M%S}")
    _write_private(backup, text)
    head = "".join(itertools.takewhile(lambda line: line.startswith("#"), text.splitlines(keepends=True)))
    _write_private(path, head + yaml.safe_dump(new, sort_keys=False))
    try:
        load_config(path)
    except ValueError as e:
        shutil.copyfile(backup, path)
        raise MigrateError(f"the merged config doesn't load ({e}); {path} is as it was") from None
    return str(backup)


def _write_memory(data: Path, band: str, mems: list, jots: list) -> dict:
    """The house agent's memory and notepad as the agents plugin's kit
    records (``agents.memory``, ``agents.note``) in the hub's records.db.
    A text already there is not written twice."""
    store = RecordStore(str(data / "records.db"))
    try:
        m = load_manifest(AGENTS)
        store.register(m.data, plugin_dir=m.folder)
        who = Caller(agent="import:0.4", band=band, why="import from 0.4")
        every = Caller(band=band, owner=True, why="import from 0.4")      # memory is private: see all of it
        memory, note = store.type("agents.memory"), store.type("agents.note")
        have = {r["text"] for t in (memory, note) for gone in (False, True)
                for r in t.list(every, where={"agent": HOUSE}, archived=gone, limit=10000)}
        mems = [x for x in mems if x["text"] not in have]
        jots = [x for x in jots if x["text"] not in have]
        for x in mems:          # oldest first, as 0.4 had them: the newest stay in the prompt
            memory.create(who, agent=HOUSE, text=x["text"])
        for x in jots:
            note.create(who, agent=HOUSE, text=x["text"])
    finally:
        store.close()
    return {"memory": len(mems), "notepad": len(jots)}


# -- workbench ------------------------------------------------------------------

def _importer():
    """The workbench's 0.4 importer (a script, loaded from its file)."""
    if not IMPORTER.exists():
        return None
    spec = importlib.util.spec_from_file_location("rook_workbench_import_04", IMPORTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _workbench(source: Source, cfg, band: str, apply: bool, levels: dict, notes: list) -> dict:
    """Knowledge, concepts, projects and tasks -> workbench records (into
    memory on a dry run, to count them), then the projects' trust levels."""
    if source.db("knowledge") is None or {"records", "links", "events"} - _tables(source.db("knowledge")):
        if levels:
            notes.append(f"trust: no 0.4 knowledge.db in the copy, so project levels ({', '.join(levels)}) "
                         "have no project to go on")
        return {"not_imported": "no 0.4 knowledge.db in the copy"}, {}
    mod = _importer()
    if mod is None:
        notes.append("knowledge, projects and tasks: the workbench importer isn't in this checkout: not imported")
        return {"not_imported": "no workbench importer in this checkout"}, {}
    sessions = source.db("sessions")
    if sessions is not None and "handoffs" not in _tables(sessions):
        sessions = None
    store = mod.open_store(str(cfg.data / "records.db") if apply else ":memory:")
    try:
        imp = mod.Importer(source.db("knowledge"), store, band, None, sessions)
        out = imp.run()
        out["trust"] = _project_trust(mod, imp, store, band, levels, notes)
        new_ids = dict(imp.new)
        new_ids.update({r["slug"]: imp.new[i] for i, r in imp.rows.items() if r["slug"] and i in imp.new})
    finally:
        store.close()
    return out, new_ids


def _carry(source: Source, cfg, band: str, apply: bool, new_ids: dict, owner: str, report: dict) -> None:
    """Chat rooms and huddles -> streams, jobs and changes -> records (into
    memory on a dry run, to count them)."""
    store = RecordStore(str(cfg.data / "records.db") if apply else ":memory:")
    try:
        for folder in CARRIED:
            m = load_manifest(PLUGINS / folder)
            store.register(m.data, plugin_dir=m.folder)
        report["rooms"] = rooms.put(source.db("chat"), str(cfg.data / "stream-events.db") if apply else ":memory:",
                                    store, band, new_ids)
        report["jobs"] = records.jobs(source.db("jobs"), store, band, owner)
        report["changes"] = records.changes(source.db("changes"), store, band, new_ids)
    finally:
        store.close()


def _project_trust(mod, imp, store, band: str, levels: dict, notes: list) -> list:
    caller = mod.Caller(agent="import:0.4", band=band, why="import from 0.4")
    rtype, done = store.type("project.trust"), []
    for slug, level in levels.items():
        old = next((r["id"] for r in imp.rows.values() if r["kind"] == "project" and r["slug"] == slug), None)
        if old not in imp.new:
            notes.append(f"trust: project {slug} isn't in the 0.4 knowledge: its {level} level isn't carried over")
            continue
        have = rtype.list(caller, where={"project": imp.new[old]}, limit=1)
        if have:
            if have[0]["level"] != level:
                notes.append(f"trust: project {slug} is already {have[0]['level']} here; kept that (0.4: {level})")
            continue
        rtype.create(caller, project=imp.new[old], level=level, note="Carried over from the 0.4 trust map.")
        done.append({"project": slug, "level": level})
    return done


# -- the report -------------------------------------------------------------------

def render(r: dict) -> str:
    """The report as Markdown for a person. Secret names only, never values."""
    when = "Applied" if r["applied"] else "Dry run (nothing written; --apply writes)"
    out = [f"# Rook 0.4 -> 0.5 migration\n\n{when}. From `{r['from']}` to `{r['to']}`.\n",
           "## Found in the copy\n"]
    out += [f"- {k}: `{v}`" if v else f"- {k}: not found" for k, v in r["found"].items()]
    out.append("\n## People\n")
    for p in r["people"]:
        grants = ", ".join(f"`{g}`" for g in p["grants"]) or "none of their own"
        out.append(f"- **{p['name']}** (0.4 {p['role']} {p['from']}): preset `{p['preset']}`, grants {grants}")
    if not r["people"]:
        out.append("- nobody in access.db")
    for name, body in r["presets"].items():
        out.append(f"- new preset `{name}`: {json.dumps(body)}")
    if r.get("config_backup"):
        out.append(f"- config written; the old one is `{r['config_backup']}`")
    out.append("\n## Trust\n")
    out.append(f"- sacred machines (changes are hard stops): {', '.join(r['sacred']) or 'none'}")
    wb = r.get("workbench") or {}
    for t in wb.get("trust") or ():
        out.append(f"- project {t['project']}: {t['level']}")
    out.append("\n## Knowledge, projects and tasks\n")
    if wb.get("not_imported"):
        out.append(f"- not imported: {wb['not_imported']}")
    else:
        out.append(f"- imported: {json.dumps(wb.get('imported') or {})}; already there: "
                   f"{json.dumps(wb.get('skipped') or {})}")
        for x in wb.get("renamed") or ():
            out.append(f"- {x['type']} slug `{x['from']}` was taken: now `{x['to']}`")
    out.append("\n## Vault\n")
    out.append(f"- {len(r['secrets'])} secrets re-sealed under the 0.5 hub's key"
               + (f" ({len(r.get('secrets_written', []))} new)" if r["applied"] else "") + ":")
    out += [f"  - `{s['name']}`" + (f": {s['description']}" if s["description"] else "") for s in r["secrets"]]
    m = r["memory"]
    out.append(f"\n## Memory\n\n- house agent: memories {m['memory']}, notepad items {m['notepad']}"
               + (f"; not moved: {json.dumps(m['skipped'])}" if m["skipped"] else ""))
    rm, jb, ch = r.get("rooms") or {}, r.get("jobs") or {}, r.get("changes") or {}
    out.append("\n## Chat, jobs and change records\n")
    out.append(f"- rooms {rm.get('rooms', 0)} and huddles {rm.get('huddles', 0)} as streams: {rm.get('messages', 0)} "
               f"messages, {rm.get('posts', 0)} huddle posts; already there: {rm.get('skipped', 0)}")
    out.append(f"- jobs {jb.get('jobs', 0)} (disabled; each keeps its 0.4 definition in vars.definition04); "
               f"already there: {jb.get('skipped', 0)}")
    out += [f"  - job `{x['from']}` is now `{x['to']}`" for x in jb.get("renamed") or ()]
    out.append(f"- change records {ch.get('changes', 0)}; already there: {ch.get('skipped', 0)}")
    a = r.get("archive")
    if a:
        out.append(f"\n## Archive\n\n- every file of the copy but the 0.4 vault key, read-only: `{a['path']}` "
                   f"({a['files']} files, {a['written']} new; MANIFEST.json has each sha256)")
        out += [f"- {c}: a different file is already archived under this name; kept it" for c in a["clashes"]]
    out.append("\n## Journal\n")
    j = r["journal"]
    if j:
        out.append(f"- {j['rows']} calls" + (f", {_day(j['first'])} to {_day(j['last'])}" if j["rows"] else "")
                   + "; kept read-only as an archive, not replayed into the 0.5 chains")
        if j.get("archive"):
            out.append(f"- archive: `{j['archive']['path']}` (sha256 {j['archive']['sha256']})")
    else:
        out.append("- no journal.db in the copy")
    out.append("\n## Re-enroll these machines\n\nOn each machine, with the 0.5 worker installed:\n")
    for w in r["workers"]:
        flag = " (sacred)" if w["sacred"] else ""
        flag += f" (0.5 name `{w['name05']}`)" if w["name05"] != w["name"] else ""
        seen = f", last seen {w['last_seen']}" if w["last_seen"] else ""
        out.append(f"- [ ] **{w['name']}**{flag}: calls in the 0.4 journal: {w['calls']}{seen}\n"
                   f"      `{w['enroll']}`")
    if not r["workers"]:
        out.append("- none found")
    out.append("\n## For a person to look at\n")
    out += [f"- {n}" for n in r["notes"]] or ["- nothing"]
    return "\n".join(out) + "\n"


def _day(ts) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d") if ts else "?"
