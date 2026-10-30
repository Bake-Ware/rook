"""Import a 0.4 hub's knowledge, concepts, projects and tasks into the 0.5
workbench records.

    python rook/plugins/knowledge/scripts/import_04.py --from knowledge-copy.db \\
        --records hub-data/records.db --band <the 0.5 hub's band> [--from-band <0.4 band>] \\
        [--handoffs sessions-copy.db] [--dry-run]

``--from`` is a copy of the 0.4 hub's knowledge.db; it is opened read-only
and never changed. Run with the 0.5 hub stopped. Each 0.4 record keeps its
title, slug, body, creator and times; then:

- concepts -> concept records; a project's 0.4 parent concept becomes its tag;
- projects -> project records; tasks -> task records (subtasks keep their
  parent; a task's project is its nearest project ancestor);
- wiki pages -> knowledge records. Verification is gone: evidence links
  become the page's sources, a person's review becomes a ``user`` source;
  superseded pages are archived and the newer page lists them;
- task links (not auto-links) become task links, notes become comments,
  ``blocked_reason`` and ``closed_by`` become comments too;
- a task's open whiteboard lines (goal, decisions, open items, notes) and,
  with ``--handoffs`` (a copy of the 0.4 sessions db), its latest handoff
  become the task's whiteboard.

Claims are not imported: 0.4 agent names mean nothing on 0.5; whoever picks
a task up claims it again. Re-running skips what an earlier run imported
(same slug, title and creation time); a different record with a taken slug
gets ``-2``, ``-3``... and is listed under ``renamed``.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from rook.kit.ctx import Caller
from rook.kit.manifest import load
from rook.kit.records import RecordStore

PLUGINS = Path(__file__).resolve().parents[2]
FOLDERS = ("concepts", "projects", "knowledge", "tasks", "whiteboard")
TASK_STATES = {"todo", "in_progress", "blocked", "paused", "done", "cancelled"}
PAGE_KINDS = {"fact", "decision", "procedure", "observation", "question", "summary", "skill"}
JOTS = (("goal", "Goal"), ("decision", "Decisions"), ("open", "Open"), ("note", "Notes"))
BOARD_MAX = 16000


def open_ro(path: str) -> sqlite3.Connection:
    """Read-only and immutable: the copy is never written, not even a -wal."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _tables(db) -> set:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:72].strip("-") or "page"


class Importer:
    def __init__(self, src: sqlite3.Connection, store: RecordStore, band: str,
                 from_band: str | None = None, handoffs: sqlite3.Connection | None = None):
        self.src, self.store, self.band, self.handoffs = src, store, band, handoffs
        where, args = ("WHERE band=?", (from_band,)) if from_band else ("", ())
        self.rows = {r["id"]: r for r in src.execute(f"SELECT * FROM records {where} ORDER BY created, id", args)}
        self.attrs = {i: json.loads(r["attrs"] or "{}") for i, r in self.rows.items()}
        self.links = self._links()
        self.new: dict[str, str] = {}             # 0.4 id -> 0.5 id
        self.fresh: set[str] = set()              # 0.4 ids this run created
        self.times: list[tuple[str, str, float, float]] = []
        self.report = {"imported": {}, "skipped": {}, "renamed": []}

    def _links(self) -> dict[str, list]:
        """Each record's explicit links, retractions applied."""
        live: dict[str, sqlite3.Row] = {}
        for r in self.src.execute("SELECT * FROM links WHERE auto=0 ORDER BY ts, id"):
            if r["retracts"]:
                live.pop(r["retracts"], None)
            elif r["record"] in self.rows:
                live[r["id"]] = r
        out: dict[str, list] = {}
        for r in live.values():
            out.setdefault(r["record"], []).append(r)
        return out

    # -- writing ------------------------------------------------------------

    def _caller(self, row) -> Caller:
        return Caller(agent=row["creator"] or "import:0.4", band=self.band, why="import from 0.4")

    def _put(self, full: str, row, fields: dict, archived: bool = False) -> str | None:
        """Create one record (or find the one an earlier run made)."""
        rtype, caller, kind = self.store.type(full), self._caller(row), full.split(".")[0]
        fields = {k: v for k, v in fields.items() if v is not None}
        base = fields.get("slug")
        n = 1
        while base and (taken := self._by_slug(rtype, caller, fields["slug"])) is not None:
            if taken["title"] == fields["title"] and taken["created"] == row["created"]:
                self.new[row["id"]] = taken["id"]
                self.report["skipped"][kind] = self.report["skipped"].get(kind, 0) + 1
                return None
            n += 1
            fields["slug"] = f"{base[:75]}-{n}"
        if base and fields["slug"] != base:
            self.report["renamed"].append({"type": kind, "from": base, "to": fields["slug"]})
        self.store.clock = lambda: row["created"]
        rec = rtype.create(caller, **fields)
        if archived:
            rtype.archive(caller, rec["id"], revision=rec["revision"])
        self.new[row["id"]] = rec["id"]
        self.fresh.add(row["id"])
        self.times.append((full, rec["id"], row["created"], row["updated"]))
        self.report["imported"][kind] = self.report["imported"].get(kind, 0) + 1
        return rec["id"]

    @staticmethod
    def _by_slug(rtype, caller, slug):
        for archived in (False, True):
            rows = rtype.list(caller, where={"slug": slug}, archived=archived, limit=1)
            if rows:
                return rows[0]
        return None

    def _update(self, full: str, old_id: str, **fields) -> None:
        rtype, caller = self.store.type(full), self._caller(self.rows[old_id])
        rec = rtype.get(caller, self.new[old_id])
        self.store.clock = lambda: self.rows[old_id]["updated"]
        rtype.update(caller, rec["id"], revision=rec["revision"], **fields)

    def _fix_times(self) -> None:
        """Records keep their 0.4 times. The kit stamps its own clock on every
        write, so the import sets created/updated afterwards (a migration may
        reach under the kit; plugins never do)."""
        with self.store._tx() as db:
            for full, rid, created, updated in self.times:
                db.execute(f'UPDATE "r:{full}" SET created=?, updated=? WHERE id=?', (created, updated, rid))

    # -- the kinds ----------------------------------------------------------

    def _of(self, kind):
        return [r for r in self.rows.values() if r["kind"] == kind]

    def _parents_first(self, rows):
        """Rows ordered so a parent of the same kind comes before its children."""
        pending, done, out = list(rows), set(), []
        ids = {r["id"] for r in rows}
        while pending:
            ready = [r for r in pending if r["parent"] not in ids or r["parent"] in done]
            if not ready:                    # a cycle: break it where it is
                ready = pending[:1]
            for r in ready:
                done.add(r["id"])
                out.append(r)
            pending = [r for r in pending if r["id"] not in done]
        return out

    def concepts(self):
        for r in self._of("concept"):
            a = self.attrs[r["id"]]
            fields = {"title": r["title"], "slug": r["slug"] or _slugify(r["title"]), "doc": r["body"]}
            for key in ("roles", "skills"):
                if isinstance(a.get(key), list):
                    fields[key] = [str(x) for x in a[key]]
            self._put("concept.concept", r, fields, archived=r["state"] == "archived")

    def projects(self):
        for r in self._of("project"):
            a, parent = self.attrs[r["id"]], self.rows.get(r["parent"])
            fields = {"title": r["title"], "slug": r["slug"], "body": r["body"],
                      "state": r["state"] if r["state"] in ("active", "paused", "done") else "done"}
            if parent is not None and parent["kind"] == "concept":
                fields["concepts"] = [parent["slug"] or _slugify(parent["title"])]
            if _date(a.get("due")):
                fields["due"] = a["due"]
            self._put("project.project", r, fields, archived=r["state"] == "archived")

    def pages(self):
        for r in self._parents_first(self._of("knowledge")):
            a = self.attrs[r["id"]]
            parent = self.new.get(r["parent"]) if self.rows.get(r["parent"], {"kind": None})["kind"] == "knowledge" else None
            kind = a.get("knowledge_kind")
            self._put("knowledge.knowledge", r, {
                "title": r["title"], "slug": r["slug"] or _slugify(r["title"]), "body": r["body"],
                "kind": kind if kind in PAGE_KINDS else "observation", "parent": parent,
                "tags": [str(t) for t in a.get("tags") or ()]},
                archived=r["state"] in ("superseded", "archived"))
        for r in self._of("knowledge"):
            sources = [self._source(link) for link in self.links.get(r["id"], ())]
            a = self.attrs[r["id"]]
            if a.get("reviewed_by"):
                sources.append({"kind": "user", "ref": a["reviewed_by"],
                                "note": a.get("review_note") or "Checked in 0.4 (verified).",
                                "by": a["reviewed_by"], "when": a.get("reviewed_at") or r["updated"]})
            if sources and r["id"] in self.fresh:
                self._update("knowledge.knowledge", r["id"], sources=sources)

    def tasks(self):
        for r in self._parents_first(self._of("task")):
            a = self.attrs[r["id"]]
            parent = self.rows.get(r["parent"])
            state = r["state"] if r["state"] in TASK_STATES else \
                "done" if r["state"] == "closed" or a.get("outcome") else "cancelled"
            fields = {"title": r["title"], "slug": r["slug"], "body": r["body"], "state": state,
                      "project": self.new.get(self._project_of(r)),
                      "parent": self.new.get(r["parent"]) if parent is not None and parent["kind"] == "task" else None}
            for key in ("criteria", "tags"):
                if isinstance(a.get(key), list):
                    fields[key] = [str(x) for x in a[key]]
            if a.get("outcome"):
                fields["outcome"] = str(a["outcome"])
            if _date(a.get("due")):
                fields["due"] = a["due"]
            self._put("task.task", r, fields, archived=r["state"] == "archived")
        for r in self._of("task"):
            if r["id"] not in self.fresh:
                continue
            links = [self._task_link(link) for link in self.links.get(r["id"], ())]
            comments = self._comments(r)
            if links or comments:
                self._update("task.task", r["id"], **({"links": links} if links else {}),
                             **({"comments": comments} if comments else {}))
            board = self._board(r)
            if board and not self._has_board(r):
                self._put_board(r, board)

    def _project_of(self, r):
        seen = set()
        while r is not None and r["id"] not in seen:
            seen.add(r["id"])
            if r["kind"] == "project":
                return r["id"]
            r = self.rows.get(r["parent"])
        return None

    # -- links, comments, whiteboards ---------------------------------------

    def _ref(self, link) -> tuple[str, str]:
        """A 0.4 link's (kind, ref) in 0.5 terms: pages by slug, records by new id."""
        kind, ref = link["kind"], link["ref"]
        target = self.rows.get(ref) if kind == "record" else None
        if target is not None:
            if target["kind"] == "knowledge":
                return "page", target["slug"] or ref
            return target["kind"], self.new.get(ref, ref)
        return ("user" if kind == "human" else kind), ref

    def _source(self, link) -> dict:
        kind, ref = self._ref(link)
        note = link["relation"] + (f": {link['note']}" if link["note"] else "")
        return {"kind": kind, "ref": ref, "note": note, "by": link["actor"], "when": link["ts"]}

    def _task_link(self, link) -> dict:
        kind, ref = self._ref(link)
        return {"kind": kind, "ref": ref, "relation": link["relation"], "note": link["note"] or "",
                "by": link["actor"], "when": link["ts"]}

    def _comments(self, r) -> list:
        a, out = self.attrs[r["id"]], []
        for e in self.src.execute("SELECT actor, ts, data FROM events WHERE record=? AND action='note' "
                                  "ORDER BY seq", (r["id"],)):
            text = json.loads(e["data"] or "{}").get("text")
            if text:
                out.append({"by": e["actor"], "when": e["ts"], "text": text})
        if a.get("blocked_reason"):
            out.append({"by": r["creator"], "when": r["updated"], "text": f"Blocked: {a['blocked_reason']}"})
        closed = a.get("closed_by")
        if isinstance(closed, dict) and closed.get("who"):
            out.append({"by": closed["who"], "when": r["updated"],
                        "text": f"Closed by {closed['who']}: \"{closed.get('quote', '')}\""})
        return out

    def _board(self, r) -> str:
        parts = []
        h = self._handoff(r)
        if h:
            parts.append(h)
        if "whiteboard" in _tables(self.src):
            rows = self.src.execute("SELECT kind, text FROM whiteboard WHERE task=? AND closed IS NULL "
                                    "ORDER BY first, seq", (r["id"],)).fetchall()
            for kind, title in JOTS:
                lines = [x["text"] for x in rows if x["kind"] == kind]
                if kind == "goal" and lines:
                    parts.append(f"Goal: {lines[-1]}")
                elif lines:
                    parts.append(f"{title}:\n" + "\n".join(f"- {t}" for t in lines))
        text = "\n\n".join(parts)
        return text if len(text) <= BOARD_MAX else text[:BOARD_MAX - 20].rstrip() + "\n\n(cut on import)"

    def _handoff(self, r) -> str:
        if self.handoffs is None:
            return ""
        refs = [x["ref"] for x in self.links.get(r["id"], ()) if x["kind"] == "handoff"]
        if not refs:
            return ""
        marks = ",".join("?" * len(refs))
        h = self.handoffs.execute(f"SELECT * FROM handoffs WHERE (thread_id IN ({marks}) OR id IN ({marks})) "
                                  "AND status != 'closed' ORDER BY ts DESC, seq DESC LIMIT 1",
                                  (*refs, *refs)).fetchone()
        if h is None:
            return ""
        lines = [f"Handoff from {h['author']}:"]
        for key in ("goal", "state"):
            if h[key]:
                lines.append(f"{key.capitalize()}: {h[key]}")
        for key, title in (("decisions", "Decisions"), ("next_steps", "Next"), ("artifacts", "Artifacts")):
            items = _json_list(h[key])
            if items:
                lines.append(f"{title}:\n" + "\n".join(f"- {i}" for i in items))
        return "\n".join(lines)

    def _has_board(self, r) -> bool:
        return bool(self.store.type("whiteboard.board").list(self._caller(r), where={"task": self.new[r["id"]]}, limit=1))

    def _put_board(self, r, text) -> None:
        self.store.clock = lambda: r["updated"]
        self.store.type("whiteboard.board").create(self._caller(r), task=self.new[r["id"]], text=text,
                                                   by="import:0.4")
        self.report["imported"]["board"] = self.report["imported"].get("board", 0) + 1

    # -- all of it ------------------------------------------------------------

    def run(self) -> dict:
        self.concepts()
        self.projects()
        self.pages()
        self.tasks()
        self._fix_times()
        return self.report


def _date(v) -> bool:
    return isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) is not None


def _json_list(v) -> list:
    try:
        out = json.loads(v or "[]")
    except ValueError:
        return []
    return [i if isinstance(i, str) else json.dumps(i) for i in out] if isinstance(out, list) else []


def open_store(path: str, plugins: Path = PLUGINS) -> RecordStore:
    """The hub's record store with the workbench types registered."""
    store = RecordStore(path)
    for folder in FOLDERS:
        m = load(plugins / folder)
        store.register(m.data, plugin_dir=m.folder)
    return store


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Import 0.4 knowledge, concepts, projects and tasks into 0.5 records.")
    ap.add_argument("--from", dest="src", required=True, help="a copy of the 0.4 hub's knowledge.db (read-only)")
    ap.add_argument("--records", required=True, help="the 0.5 hub's records.db (hub stopped)")
    ap.add_argument("--band", required=True, help="the 0.5 hub's band id")
    ap.add_argument("--from-band", help="only this 0.4 band (default: all of them)")
    ap.add_argument("--handoffs", help="a copy of the 0.4 sessions db, for each task's latest handoff")
    ap.add_argument("--dry-run", action="store_true", help="import into memory and report; write nothing")
    a = ap.parse_args(argv)
    src = open_ro(a.src)
    missing = {"records", "links", "events"} - _tables(src)
    if missing:
        print(f"{a.src}: not a 0.4 knowledge db (no {', '.join(sorted(missing))})", file=sys.stderr)
        return 2
    handoffs = open_ro(a.handoffs) if a.handoffs else None
    store = open_store(":memory:" if a.dry_run else a.records)
    try:
        report = Importer(src, store, a.band, a.from_band, handoffs).run()
    finally:
        store.close()
    print(json.dumps({"dry_run": a.dry_run, **report}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
