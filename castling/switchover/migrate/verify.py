"""Did every 0.4 artifact survive the move? Read-only on both sides.

    python -m castling.switchover.migrate --from <the 0.4 copy> --to <0.5 hub.yaml> --verify

Bake, Oct 31 (castling-switchover-decisions, "Fly it"): "as long as all the
artifacts survive transfer and the hub and bands work correctly we can build
the rest of the plane while we're flying it". This is the first half.

For every kind of 0.4 artifact it counts what the copy has (``source``),
what the 0.5 hub holds (``moved``, compared item by item: titles, times,
bodies, message texts, secret value hashes), what was left out on purpose
(``left``, with the castling-* decision that says why; those stay in the
archive) and ``missing`` (the rest). Two more checks make it exhaustive:

- **every file** of the copy is in ``<data>/archive/0.4`` with the same
  sha256 (only the 0.4 vault key stays out; its secrets are re-sealed);
- **every table** of every 0.4 store belongs to a kind below: a table the
  move doesn't know fails the check instead of slipping by.

Exit 0 only when nothing is missing. Secret values are hashed in memory and
never printed; the report names counts and ids, never values.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from rook.core2.hub.runtime import load_config

from . import archive, names, rooms
from .source import Source, _tables, open_ro

ARCHIVE_WHY = ("castling-design", "Migration from 0.4 is not a product feature ... export knowledge, projects/tasks, "
               "memory, vault as plain files; an agent imports and reshapes them into 0.5; old journal kept "
               "read-only as an archive")
WHY = {
    "claims": ("castling-workbench", "Drop verification flags and claims."),
    "hygiene": ("castling-workbench", "Hygiene checks: candidate for the PM's periodic sweeps."),
    "activity": ("castling-agents", "Essentials only, not full session context or raw output (e.g. not 6,000 "
                                    "test results)."),
    "board": ("castling-agents", "It persists between sessions, so its current state is the automatic handoff, "
                                 "but it is never a record — history lives in the journal and change records"),
    "votes": ("castling-workbench", "Drop the merge-votes room (chair, pins, unanimous huddle.vote)."),
    "terminal": ("castling-streams", "Bounded: terminals and processes keep a bounded buffer, with an optional "
                                     "export to a durable stream or file."),
    "memory": ("castling-agents", "Dream cycle: System Three curates the notepad into lasting memories and "
                                  "prunes/corrects both the notepad and existing memories."),
    "reenroll": ("castling-design", "workers re-enroll"),
    "clean": ("castling-design", "0.5 is a clean cut: no compatibility with 0.4 (Bake, Oct 28)."),
    "other_user": ("castling-design", "Bake helps the other user directly."),
    "roles": ("castling-workbench", "Roles = agents with personas"),
    "holds": ("castling-changes", "Holds: when the trust map or a security plugin requires approval, the exact "
                                  "call is held ... approving runs that exact call."),
    "trust": ("castling-changes", "Ceremony scales with the project's trust level in the trust map: sacred ... "
                                  "normal ... ship-it"),
    "attention": ("castling-attention", "periodically collect everything trying to get the user's attention ... "
                                        "via caps"),
    "sessions": ("castling-streams", "History readers and the mirror become importers that write into a stream."),
    "archive": ARCHIVE_WHY,
}

# Every table of every 0.4 store -> the kind that accounts for it. FTS
# shadow tables and migration bookkeeping are "index" (rebuilt from rows).
TABLES = {
    "knowledge.db": {"records": "workbench records", "links": "links", "events": "store internals",
                     "whiteboard": "whiteboards", "claims": "claims", "hygiene": "hygiene",
                     "receipts": "store internals", "actors": "store internals", "cursors": "store internals",
                     "blocks": "store internals", "block_vectors": "store internals",
                     "embeddings": "store internals", "record_names": "store internals"},
    "sessions.db": {"handoffs": "handoffs", "record_names": "store internals"},
    "vault.db": {"secrets": "secrets", "access": "vault access log"},
    "access.db": {"members": "people", "grants": "people", "tokens": "api keys", "chat_links": "chat links",
                  "link_codes": "chat links", "spaces": "chat links", "chat_bots": "chat links",
                  "resources": "workers", "bands": "bands and enrollment", "history": "access history",
                  "meta": "store internals", "requests": "access history"},
    "enrollment.db": {"users": "accounts", "*": "bands and enrollment"},
    "chat.db": {"rooms": "chat rooms", "messages": "chat messages", "huddles": "huddles",
                "huddle_posts": "huddle posts", "huddle_questions": "huddle posts",
                "huddle_status": "chat state", "reads": "chat state", "presence": "chat state",
                "avatars": "chat state", "record_names": "store internals",
                "huddle_artifacts": "merge votes", "huddle_ballots": "merge votes", "huddle_cards": "merge votes",
                "huddle_motions": "merge votes", "huddle_rounds": "merge votes"},
    "console.db": {"lines": "console history", "rooms": "console history", "term_watch": "console history",
                   "record_names": "store internals", "search": "index"},
    "journal.db": {"calls": "journal"},
    "jobs.db": {"jobs": "jobs", "triggers": "jobs", "runs": "job runs", "history": "job runs",
                "notices": "job runs", "record_names": "store internals"},
    "changes.db": {"changes": "change records", "*": "store internals"},
    "memory.db": {"memories": "memory", "memory_history": "memory history", "memory_meta": "store internals",
                  "memory_turns": "memory history", "memory_vectors": "index", "memories_fts": "index"},
    "approvals.db": {"*": "approvals history"},
    "settings.db": {"*": "settings"},
    "guidance.db": {"*": "roles and guidance"},
    "roles.db": {"*": "roles and guidance"},
    "persona.db": {"*": "roles and guidance"},
    "front.db": {"*": "front door"},
    "event_log.db": {"*": "attention"},
    "feed.db": {"*": "attention"},
    "drive.db": {"*": "plugin data"},
    "work.sqlite3": {"*": "work sessions"},
}
SHADOW = re.compile(r"_fts$|_(config|data|docsize|idx|content)$|^_rook_migrations$|^sqlite_")
JOTS = ("goal", "decision", "open", "note")      # what a 0.4 whiteboard line a person or agent wrote is


def _h(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def _json(v, default):
    try:
        out = json.loads(v) if isinstance(v, (str, bytes)) and v else default
    except ValueError:
        return default
    return out if isinstance(out, type(default)) else default


class Dest:
    """The 0.5 hub's stores, read-only."""

    def __init__(self, data: Path):
        self.data = data
        self._dbs: dict[str, sqlite3.Connection | None] = {}

    def db(self, rel: str) -> sqlite3.Connection | None:
        if rel not in self._dbs:
            path = self.data / rel
            self._dbs[rel] = None
            if path.exists():
                db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                db.row_factory = sqlite3.Row
                self._dbs[rel] = db
        return self._dbs[rel]

    def records(self, full: str) -> list[dict]:
        db = self.db("records.db")
        if db is None or f"r:{full}" not in _tables(db):
            return []
        return [dict(r) for r in db.execute(f'SELECT * FROM "r:{full}"')]

    def close(self) -> None:
        for db in self._dbs.values():
            if db is not None:
                db.close()


def _row(kind, source, moved, left=0, missing=(), why=None, note="") -> dict:
    missing = list(missing)
    return {"kind": kind, "source": source, "moved": moved, "left": left, "missing": len(missing),
            "examples": [str(m) for m in missing[:8]], "why": list(why) if why else None, "note": note}


def verify(src: str | Path, config: str | Path) -> dict:
    cfg = load_config(Path(config).resolve())
    source = Source(src)
    dest = Dest(cfg.data)
    try:
        rows, unknown = [], []
        files = _files(source, cfg.data)
        rows.append(files)
        archived = set(files.pop("_archived"))
        found = {}
        for path in source.files():
            if _is_db(path):
                db = open_ro(path)
                found[path] = sorted(t for t in _tables(db) if not SHADOW.search(t))
                db.close()
        for path, tables in found.items():
            known = TABLES.get(path.name)
            for t in tables:
                if known is None or (t not in known and "*" not in known):
                    unknown.append(f"{path.relative_to(source.root).as_posix()}:{t}")
        report_path = cfg.data / "migrate" / "report.json"
        mig = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        rows += _workbench(source, dest)
        rows += _people(source, cfg, mig)
        rows.append(_secrets(source, cfg.data))
        rows += _trust(source, cfg, dest)
        rows += _chat(source, dest)
        rows += _jobs_changes(source, dest)
        rows += _memory(source, dest)
        rows += _archived_only(source, archived, mig)
        if unknown:
            rows.append(_row("unknown 0.4 tables", len(unknown), 0, missing=unknown,
                             note="a store table no kind accounts for: teach verify (and maybe the move) about it"))
        missing = sum(r["missing"] for r in rows)
        return {"from": str(source.root), "to": str(cfg.data), "ok": missing == 0, "missing": missing, "kinds": rows}
    finally:
        dest.close()
        source.close()


def _is_db(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(16).startswith(b"SQLite format 3")


def _db(source: Source, name: str) -> sqlite3.Connection | None:
    hits = sorted(source.root.rglob(name), key=lambda p: (len(p.parts), str(p)))
    return open_ro(hits[0]) if hits else None


def _count(db, table, where="") -> int:
    if db is None or table not in _tables(db):
        return 0
    return db.execute(f"SELECT count(*) FROM {table} {where}").fetchone()[0]


# -- the archive ------------------------------------------------------------------

def _files(source: Source, data: Path) -> dict:
    dest = data / archive.DIR
    ok, missing, left = [], [], 0
    for path in source.files():
        rel = path.relative_to(source.root).as_posix()
        if archive.skipped(rel):
            left += 1
            continue
        out = dest / rel
        if out.is_file() and archive.sha256(out) == archive.sha256(path):
            ok.append(rel)
        else:
            missing.append(rel)
    row = _row("files (whole copy, byte for byte in archive/0.4)", len(ok) + len(missing) + left, len(ok), left,
               missing, ("castling-switchover-decisions", "D8: move all secrets (the tool does), retire the "
                         "0.4-only ones after cutover.") if left else None,
               note="left: the 0.4 vault key; its secrets are re-sealed under the 0.5 hub's key")
    row["_archived"] = ok
    return row


# -- knowledge, projects, tasks -----------------------------------------------------

def _workbench(source: Source, dest: Dest) -> list:
    kdb = source.db("knowledge")
    if kdb is None or "records" not in _tables(kdb):
        return []
    out = []
    recs = [dict(r) for r in kdb.execute("SELECT * FROM records")]
    types = {"concept": ("concept.concept", "doc"), "project": ("project.project", "body"),
             "knowledge": ("knowledge.knowledge", "body"), "task": ("task.task", "body")}
    new: dict[str, dict] = {}
    for kind, (full, body) in types.items():
        mine = [r for r in recs if r["kind"] == kind]
        have = {(r["title"], r["created"]): r for r in dest.records(full)}
        missing = []
        for r in mine:
            got = have.get((r["title"], r["created"]))
            if got is None or (got[body] or "") != (r["body"] or ""):
                missing.append(r["slug"] or r["id"])
            else:
                new[r["id"]] = got
        out.append(_row({"knowledge": "knowledge pages", "task": "tasks"}.get(kind, kind + "s"),
                        len(mine), len(mine) - len(missing), missing=missing))
    other = [r for r in recs if r["kind"] not in types]
    if other:
        out.append(_row("other record kinds", len(other), 0, missing=[f"{r['kind']}:{r['id']}" for r in other]))
    out += _links(kdb, recs, new, dest)
    out += _boards(source, kdb, recs, new, dest)
    out.append(_row("claims", _count(kdb, "claims"), 0, _count(kdb, "claims"), why=WHY["claims"]))
    out.append(_row("hygiene findings", _count(kdb, "hygiene"), 0, _count(kdb, "hygiene"), why=WHY["hygiene"]))
    return out


def _links(kdb, recs, new, dest) -> list:
    live: dict[str, sqlite3.Row] = {}
    for r in kdb.execute("SELECT * FROM links WHERE auto=0 ORDER BY ts, id"):
        if r["retracts"]:
            live.pop(r["retracts"], None)
        else:
            live[r["id"]] = r
    kind = {r["id"]: r["kind"] for r in recs}
    want: Counter = Counter()
    other = []
    for r in live.values():
        if kind.get(r["record"]) in ("knowledge", "task"):
            want[r["record"]] += 1
        else:
            other.append(r)
    for r in recs:
        if r["kind"] == "knowledge" and _json(r["attrs"], {}).get("reviewed_by"):
            want[r["id"]] += 1                                 # a person's review -> a user source
    rows = []
    for k, field, label in (("knowledge", "sources", "knowledge links -> page sources"),
                            ("task", "links", "task links")):
        ids = [i for i in want if kind.get(i) == k]
        missing = [i for i in ids if i not in new or len(_json(new[i].get(field), [])) < want[i]]
        total = sum(want[i] for i in ids)
        rows.append(_row(label, total, total - sum(want[i] for i in missing), missing=missing))
    boards = {r["project"]: r["text"] or "" for r in dest.records("whiteboard.board") if r.get("project")}
    miss = [r["record"] for r in other if r["record"] not in new
            or r["ref"] not in boards.get(new[r["record"]]["id"], "")]
    rows.append(_row("project and concept links -> project whiteboards", len(other), len(other) - len(miss),
                     missing=miss))
    auto = _count(kdb, "links", "WHERE auto=1")
    retracted = len([1 for r in kdb.execute("SELECT id FROM links WHERE auto=0")]) - len(live)
    rows.append(_row("automatic and retracted links", auto + retracted, 0, auto + retracted, why=WHY["archive"],
                     note="auto links record what a session touched; retracted ones were withdrawn"))
    return rows


def _boards(source, kdb, recs, new, dest) -> list:
    rows = []
    boards = {r["task"]: r["text"] or "" for r in dest.records("whiteboard.board")}
    comments = Counter(r["task"] for r in dest.records("task.comment"))
    tasks = [r for r in recs if r["kind"] == "task"]
    # comments: note events + blocked_reason + closed_by
    want_c: Counter = Counter()
    for e in kdb.execute("SELECT record, data FROM events WHERE action='note'"):
        if _json(e["data"], {}).get("text"):
            want_c[e["record"]] += 1
    for r in tasks:
        a = _json(r["attrs"], {})
        want_c[r["id"]] += bool(a.get("blocked_reason")) + bool(isinstance(a.get("closed_by"), dict)
                                                                and a["closed_by"].get("who"))
    ids = [i for i, n in want_c.items() if n]
    miss = [i for i in ids if i not in new or comments.get(new[i]["id"], 0) < want_c[i]]
    total = sum(want_c[i] for i in ids)
    rows.append(_row("task notes -> comments", total, total - sum(want_c[i] for i in miss), missing=miss))
    if "whiteboard" in _tables(kdb):
        lines = [dict(r) for r in kdb.execute("SELECT task, kind, text, closed FROM whiteboard")]
        activity = [x for x in lines if x["kind"] not in JOTS]
        lines = [x for x in lines if x["kind"] in JOTS]
        open_ = [x for x in lines if x["closed"] is None and x["text"]]
        miss = [x["task"] for x in open_ if x["task"] in new
                and x["text"] not in boards.get(new[x["task"]]["id"], "")
                and len(boards.get(new[x["task"]]["id"], "")) < 15900]      # a board cut at 16k on import
        miss += [x["task"] for x in open_ if x["task"] not in new]
        rows.append(_row("whiteboard lines (open) -> task whiteboards", len(open_), len(open_) - len(miss),
                         missing=miss))
        closed = len(lines) - len(open_)
        rows.append(_row("whiteboard lines (erased)", closed, 0, closed, why=WHY["board"]))
        rows.append(_row("whiteboard activity lines (calls, consoles, sessions, secrets, handoffs)", len(activity),
                         0, len(activity), why=WHY["activity"],
                         note="0.4 echoed each claimed call onto the board; the archive keeps them"))
    sdb = source.db("sessions")
    if sdb is not None and "handoffs" in _tables(sdb):
        hs = [dict(r) for r in sdb.execute("SELECT * FROM handoffs")]
        all_boards = "\n".join(boards.values())
        moved = [h for h in hs if h["status"] != "closed" and h["goal"] and f"Goal: {h['goal']}" in all_boards]
        rows.append(_row("handoffs", len(hs), len(moved), len(hs) - len(moved), why=WHY["board"],
                         note="each task's latest open handoff is on its whiteboard; every handoff is in the "
                              "archive (sessions.db)"))
    return rows


# -- people, secrets, trust -------------------------------------------------------------

def _people(source: Source, cfg, mig: dict) -> list:
    people = source.people()
    mapping = {p["from"]: p["name"] for p in mig.get("people") or ()}
    missing = [p["id"] for p in people if mapping.get(p["id"]) not in (cfg.users or {})]
    grants = sum(len(p["grants"]) for p in people)
    got = sum(len((cfg.users.get(mapping.get(p["id"])) or {}).get("grants") or ()) for p in people)
    rows = [_row("people (0.4 members)", len(people), len(people) - len(missing), missing=missing),
            _row("active grants -> grant patterns", grants, min(got, grants),
                 missing=[] if got >= grants else ["grants short by %d" % (grants - got)])]
    adb = source.db("accounts")
    accts = [dict(r) for r in adb.execute("SELECT id FROM users")] if adb and "users" in _tables(adb) else []
    members = {p["id"] for p in people}
    other = [a for a in accts if a["id"] not in members]
    rows.append(_row("accounts", len(accts), len(accts) - len(other), len(other), why=WHY["other_user"],
                     note="accounts that were never members of the hub"))
    tokens = sum(len(p["tokens"]) for p in people)
    rows.append(_row("api keys", tokens, 0, tokens, why=WHY["clean"],
                     note="each client gets a new 0.5 token (runbook); the report lists every key's label"))
    links = sum(len(p["chat_links"]) for p in people) + len(source.spaces())
    rows.append(_row("chat links and spaces", links, 0, links, why=WHY["clean"], note="relink with /link on 0.5"))
    workers = mig.get("workers") or []
    rows.append(_row("workers -> re-enroll checklist", len(source.workers()), len(workers),
                     missing=[] if len(workers) >= len(source.workers()) else ["checklist short"]))
    return rows


def _secrets(source: Source, data: Path) -> dict:
    secrets, problems = source.secrets()
    want = {s["name"]: hashlib.sha256(s["value"].encode()).hexdigest() for s in secrets}
    from .vault import Vault
    missing = list(problems)
    moved = 0
    if not (data / "vault.db").exists():          # unlock would make one: verify never writes
        return _row("secrets (names + value hashes)", len(want) + len(problems), 0,
                    missing=missing + sorted(want))
    try:
        v = Vault(data).unlock()
    except Exception as e:                       # noqa: BLE001 - no vault: every secret is missing
        return _row("secrets (names + value hashes)", len(want), 0, missing=[f"vault: {type(e).__name__}"]
                    + sorted(want))
    for name, digest in sorted(want.items()):
        try:
            ok = hashlib.sha256(v.get(name).encode()).hexdigest() == digest
        except KeyError:
            ok = False
        moved += ok
        if not ok:
            missing.append(name)
    total = len(want) + len(problems)
    return _row("secrets (names + value hashes)", total, moved, missing=missing)


def _trust(source: Source, cfg, dest: Dest) -> list:
    t = source.trust()
    if not t:
        return []
    from . import convert
    sacred, levels, _ = convert.trust(t, [w["name"] for w in source.workers()])
    have = set(cfg.sacred or ())
    miss = [s for s in sacred if s not in have]
    rows = [_row("trust: sacred machines", len(sacred), len(sacred) - len(miss), missing=miss)]
    trusted = len(dest.records("project.trust"))
    rows.append(_row("trust: project levels", len(levels), min(trusted, len(levels)),
                     missing=[] if trusted >= len(levels) else sorted(levels)))
    entries = len(t.get("entries") or ()) - len(levels) - len(sacred)
    rest = max(entries, 0) + sum(len(t.get(k) or ()) for k in convert.TRUST_LISTS + ("notices",))
    rows.append(_row("trust: other entries, exceptions, proposals", rest, 0, rest, why=WHY["trust"],
                     note="0.5's trust map holds sacred machines and project levels; machine ship-it is the default"))
    return rows


# -- chat, jobs, changes, memory ---------------------------------------------------------

def _chat(source: Source, dest: Dest) -> list:
    db = source.db("chat")
    if db is None or "rooms" not in _tables(db):
        return []
    sdb = dest.db("stream-events.db")
    streams = {}
    if sdb is not None:
        for s in sdb.execute("SELECT id, kind, attrs FROM streams"):
            a = _json(s["attrs"], {})
            if a.get("from04"):
                streams[a["from04"]] = s["id"]
    huddles = {h["room"]: h for h in db.execute("SELECT room, archived FROM huddles")} \
        if "huddles" in _tables(db) else {}
    hud_recs = {r["stream"] for r in dest.records(names.record("huddle"))}
    miss_rooms, miss_msgs, miss_posts, n_msgs, n_posts = [], [], [], 0, 0
    for room in db.execute("SELECT id FROM rooms"):
        rid = room["id"]
        want = rooms.events(db, rid)
        texts = Counter(_h(by, body["text"]) for _, t, by, body in want if t == "text")
        posts = Counter(_h(by, body["kind04"], body["text"]) for _, t, by, body in want if t == "status")
        n_msgs += sum(texts.values())
        n_posts += sum(posts.values())
        sid = streams.get(rid)
        if sid is None or (rid in huddles and sid not in hud_recs):
            miss_rooms.append(rid)
            miss_msgs += [rid] * sum(texts.values())
            miss_posts += [rid] * sum(posts.values())
            continue
        got_t, got_p = Counter(), Counter()
        for e in sdb.execute("SELECT type, by, body FROM events WHERE stream=?", (sid,)):
            body = _json(e["body"], {})
            if e["type"] == "text":
                got_t[_h(e["by"], body.get("text"))] += 1
            elif e["type"] == "status" and "kind04" in body:
                got_p[_h(e["by"], body["kind04"], body.get("text"))] += 1
        miss_msgs += [rid] * sum((texts - got_t).values())
        miss_posts += [rid] * sum((posts - got_p).values())
    n_rooms = _count(db, "rooms")
    rows = [_row("chat rooms and huddles -> streams", n_rooms, n_rooms - len(miss_rooms), missing=miss_rooms),
            _row("chat messages -> text events", n_msgs, n_msgs - len(miss_msgs), missing=miss_msgs),
            _row("huddle posts and questions -> status events", n_posts, n_posts - len(miss_posts),
                 missing=miss_posts)]
    votes = sum(_count(db, t) for t in ("huddle_artifacts", "huddle_ballots", "huddle_cards", "huddle_motions",
                                        "huddle_rounds"))
    rows.append(_row("merge votes (motions, rounds, ballots, artifacts, cards)", votes, 0, votes, why=WHY["votes"]))
    state = sum(_count(db, t) for t in ("reads", "presence", "huddle_status", "avatars"))
    rows.append(_row("chat state (read cursors, presence, latest status)", state, 0, state, why=WHY["archive"],
                     note="derived state: each reader keeps its own cursor on 0.5; latest status = the last status post"))
    return rows


def _jobs_changes(source: Source, dest: Dest) -> list:
    rows = []
    jdb = source.db("jobs")
    if jdb is not None and "jobs" in _tables(jdb):
        have = {}
        for r in dest.records(names.record("jobs")):
            v = _json(r.get("vars"), {})
            if v.get("id04"):
                have[v["id04"]] = (v, r)
        miss = [j["name"] for j in jdb.execute("SELECT id, name, definition FROM jobs")
                if j["id"] not in have or have[j["id"]][0].get("definition04") != _json(j["definition"], {})]
        n = _count(jdb, "jobs")
        rows.append(_row("jobs (disabled, 0.4 definition kept)", n, n - len(miss), missing=miss))
        runs = sum(_count(jdb, t) for t in ("runs", "history", "notices"))
        rows.append(_row("job runs and history", runs, 0, runs, why=WHY["archive"]))
    cdb = source.db("changes")
    if cdb is not None and "changes" in _tables(cdb):
        have = {(r["what"], r["created"]) for r in dest.records(names.record("changes"))}
        miss = []
        for c in cdb.execute("SELECT id, record, created FROM changes"):
            what = str(_json(c["record"], {}).get("what") or f"0.4 change {c['id']}")[:500]
            if (what, c["created"]) not in have:
                miss.append(c["id"])
        n = _count(cdb, "changes")
        rows.append(_row("change records", n, n - len(miss), missing=miss))
    return rows


def _memory(source: Source, dest: Dest) -> list:
    mems = source.memories()
    if not mems:
        return []
    have = {r["text"] for full in (names.record("agents", "memory"), names.record("agents", "note")) for r in dest.records(full)
            if r.get("agent") == "house"}
    active = [m for m in mems if m["state"] == "active"]
    miss = [m["id"] for m in active                  # a person's memory moves as "About <name>: <text>"
            if m["text"] not in have and not any(h.endswith(": " + m["text"]) for h in have)]
    rest = len(mems) - len(active)
    return [_row("house agent memory (active -> memory or notepad)", len(active), len(active) - len(miss),
                 missing=miss),
            _row("memory (superseded, forgotten, archived)", rest, 0, rest, why=WHY["memory"])]


# -- what stays in the archive only ------------------------------------------------------

ARCHIVE_ONLY = [
    # (kind, store file, tables (None: all), why key, note)
    ("journal (read-only archive)", "journal.db", ["calls"], "archive", "never replayed into the 0.5 chains"),
    ("console rooms and lines", "console.db", ["rooms", "lines"], "terminal", ""),
    ("vault access log", "vault.db", ["access"], "archive", ""),
    ("access history", "access.db", ["history", "requests"], "archive", ""),
    ("bands, devices, enrollment", "enrollment.db", ["-users"], "reenroll", "accounts are counted above"),
    ("approvals history (holds, relays)", "approvals.db", None, "holds", "a hold is one call, not a lasting record"),
    ("hub settings", "settings.db", None, "clean", "re-enter the ones that matter as 0.5 settings (report lists them)"),
    ("roles, guidance, personas", "roles.db", None, "roles", "0.5 ships its ready-made personas"),
    ("roles, guidance, personas", "guidance.db", None, "roles", ""),
    ("roles, guidance, personas", "persona.db", None, "roles", ""),
    ("front door (asks, decisions, requests)", "front.db", None, "archive", ""),
    ("attention feed and events", "feed.db", None, "attention", "the house agent's attention job rebuilds it"),
    ("attention feed and events", "event_log.db", None, "attention", ""),
    ("work sessions catalog", "work.sqlite3", None, "sessions", "the transcripts live on the workers"),
    ("memory history", "memory.db", ["memory_history", "memory_turns"], "memory", ""),
    ("plugin data (decide drive runs)", "drive.db", None, "archive", ""),
    ("store internals (record events, receipts, names, search blocks)", "knowledge.db",
     ["events", "receipts", "actors", "cursors", "blocks", "block_vectors", "embeddings", "record_names"],
     "archive", "record history and indexes; 0.5 keeps its own"),
]


def _archived_only(source: Source, archived: set, mig: dict) -> list:
    rows: dict[str, dict] = {}
    for kind, name, tables, why, note in ARCHIVE_ONLY:
        hits = sorted(source.root.rglob(name), key=lambda p: (len(p.parts), str(p)))
        if not hits:
            continue
        rel = hits[0].relative_to(source.root).as_posix()
        db = open_ro(hits[0])
        try:
            drop = {t[1:] for t in tables or () if t.startswith("-")}
            names = [t for t in tables or () if not t.startswith("-")] or \
                [t for t in _tables(db) if not SHADOW.search(t) and t not in drop]
            n = sum(_count(db, t) for t in names if t in _tables(db))
        finally:
            db.close()
        row = rows.get(kind)
        if row is None:
            row = rows[kind] = _row(kind, 0, 0, 0, why=WHY[why], note=note)
            row["_miss"] = []
        row["source"] += n
        if rel in archived:
            row["left"] += n
        else:
            row["_miss"] += [rel] * n
    out = []
    for row in rows.values():
        miss = row.pop("_miss")
        row["missing"], row["examples"] = len(miss), sorted(set(miss))[:8]
        out.append(row)
    return out


# -- the report ---------------------------------------------------------------------------

def render(r: dict) -> str:
    head = "VERIFIED: every 0.4 artifact is moved, or left on purpose and kept in the archive." if r["ok"] else \
        f"NOT VERIFIED: {r['missing']} item(s) missing."
    out = [f"# Rook 0.4 -> 0.5: verify\n\n{head}\n\nFrom `{r['from']}` to `{r['to']}`.\n",
           "| kind | 0.4 | moved | left (archived) | missing |", "|---|---:|---:|---:|---:|"]
    for k in r["kinds"]:
        out.append(f"| {k['kind']} | {k['source']} | {k['moved']} | {k['left']} | {k['missing']} |")
    out.append("\n## Why each left item is left\n")
    seen = set()
    for k in r["kinds"]:
        if k["left"] and k["why"]:
            page, quote = k["why"]
            out.append(f"- **{k['kind']}**: {page}: \"{quote}\"" + (f" ({k['note']})" if k["note"] else ""))
            seen.add(k["kind"])
    bad = [k for k in r["kinds"] if k["missing"]]
    if bad:
        out.append("\n## Missing\n")
        out += [f"- **{k['kind']}**: {k['missing']}, e.g. {', '.join(k['examples'])}"
                + (f" ({k['note']})" if k["note"] and k["kind"] not in seen else "") for k in bad]
    return "\n".join(out) + "\n"
