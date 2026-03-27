"""Claude.ai cloud sync — API → SQLite + files, zero tokens consumed.

Syncs conversations, projects, and project documents from claude.ai
into local storage. Delta sync via updated_at comparison.

Storage layout:
    ~/.rook/cloud/
    ├── cloud.db                    # metadata, turns, FTS index
    └── docs/
        ├── LISA/
        │   └── lisa_v3_5_spec.md   # actual file content
        └── C971/
            └── Styles.xaml

The sync is a pure API → storage pipeline. No LLM involved.
MCP tools query the local DB/files and return only relevant snippets.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import logging
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("rook.cloud-sync")

CLAUDE_DATA = Path.home() / "AppData" / "Roaming" / "Claude"
LOCAL_STATE = CLAUDE_DATA / "Local State"
COOKIES_DB = CLAUDE_DATA / "Network" / "Cookies"

CLOUD_DIR = Path.home() / ".rook" / "cloud"
SYNC_DB = CLOUD_DIR / "cloud.db"
DOCS_DIR = CLOUD_DIR / "docs"


# ── Cookie extraction (Windows DPAPI) ───────────────────────────────────────

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_decrypt(encrypted: bytes) -> bytes | None:
    blob_in = _DATA_BLOB(len(encrypted), ctypes.create_string_buffer(encrypted, len(encrypted)))
    blob_out = _DATA_BLOB()
    if ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return data
    return None


def _get_chrome_key() -> bytes | None:
    if not LOCAL_STATE.exists():
        return None
    with open(LOCAL_STATE, "r") as f:
        ls = json.load(f)
    encrypted_key = base64.b64decode(ls["os_crypt"]["encrypted_key"])[5:]
    return _dpapi_decrypt(encrypted_key)


def _decrypt_cookie(enc: bytes, key: bytes) -> bytes | None:
    if not enc or enc[:3] not in (b"v10", b"v20"):
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key).decrypt(enc[3:15], enc[15:], None)


def _read_cookies() -> dict[str, bytes]:
    """Read all claude.ai cookies, returning raw decrypted bytes."""
    if not COOKIES_DB.exists():
        return {}
    key = _get_chrome_key()
    if not key:
        return {}

    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(str(COOKIES_DB), tmp)
    try:
        db = sqlite3.connect(tmp)
        rows = db.execute('SELECT name, encrypted_value FROM cookies WHERE host_key LIKE "%claude.ai%"').fetchall()
        db.close()
    finally:
        Path(tmp).unlink(missing_ok=True)

    cookies = {}
    for name, enc in rows:
        raw = _decrypt_cookie(enc, key)
        if raw:
            cookies[name] = raw
    return cookies


def get_session_key() -> str | None:
    raw = _read_cookies().get("sessionKey")
    if not raw:
        return None
    match = re.search(rb"sk-ant-[A-Za-z0-9_-]+", raw)
    return match.group().decode("ascii") if match else None


def get_org_id() -> str | None:
    raw = _read_cookies().get("lastActiveOrg")
    if not raw:
        return None
    match = re.search(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw)
    return match.group().decode("ascii") if match else None


# ── API client ───────────────────────────────────────────────────────────────

def _api_get(path: str, session_key: str, org_id: str) -> dict | list | None:
    url = f"https://claude.ai/api/organizations/{org_id}/{path}"
    req = urllib.request.Request(url, headers={
        "Cookie": f"sessionKey={session_key}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error("API error (%s): %s", path[:60], e)
        return None


# ── Database ─────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(SYNC_DB))
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            uuid TEXT PRIMARY KEY,
            name TEXT,
            model TEXT,
            created_at TEXT,
            updated_at TEXT,
            project_uuid TEXT,
            is_starred INTEGER DEFAULT 0,
            synced_at REAL
        );

        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_uuid TEXT NOT NULL,
            turn_index INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT,
            message_uuid TEXT,
            FOREIGN KEY (conversation_uuid) REFERENCES conversations(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_convo_idx
            ON turns(conversation_uuid, turn_index);

        CREATE TABLE IF NOT EXISTS projects (
            uuid TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            created_at TEXT,
            updated_at TEXT,
            synced_at REAL
        );

        CREATE TABLE IF NOT EXISTS docs (
            uuid TEXT PRIMARY KEY,
            project_uuid TEXT NOT NULL,
            file_name TEXT NOT NULL,
            content TEXT,
            estimated_tokens INTEGER,
            created_at TEXT,
            updated_at TEXT,
            local_path TEXT,
            synced_at REAL,
            FOREIGN KEY (project_uuid) REFERENCES projects(uuid)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
            content, conversation_uuid UNINDEXED, role UNINDEXED,
            content_rowid='id'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            content, file_name, project_uuid UNINDEXED,
            content='docs', content_rowid='rowid'
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    db.commit()
    return db


def _safe_dirname(name: str) -> str:
    """Sanitize a project name for use as a directory name."""
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip('. ')


# ── Sync: Conversations ─────────────────────────────────────────────────────

def sync_conversations(db: sqlite3.Connection, session_key: str, org_id: str,
                       full: bool = False) -> dict:
    stats = {"new": 0, "updated": 0, "unchanged": 0, "errors": 0}

    # Fetch metadata list (cheap — no message content)
    remote = _api_get("chat_conversations?limit=500", session_key, org_id)
    if not remote or not isinstance(remote, list):
        stats["errors"] = 1
        return stats

    for convo in remote:
        uuid = convo["uuid"]
        remote_updated = convo.get("updated_at", "")

        local = db.execute("SELECT updated_at FROM conversations WHERE uuid=?", (uuid,)).fetchone()
        if local and local["updated_at"] == remote_updated and not full:
            stats["unchanged"] += 1
            continue

        is_new = local is None

        # Fetch full conversation with messages
        full_convo = _api_get(f"chat_conversations/{uuid}", session_key, org_id)
        if not full_convo:
            stats["errors"] += 1
            continue

        db.execute("""
            INSERT OR REPLACE INTO conversations (uuid, name, model, created_at, updated_at, is_starred, synced_at)
            VALUES (?,?,?,?,?,?,?)
        """, (uuid, convo.get("name", ""), convo.get("model", ""),
              convo.get("created_at", ""), remote_updated,
              1 if convo.get("is_starred") else 0, time.time()))

        # Replace turns
        db.execute("DELETE FROM turns WHERE conversation_uuid=?", (uuid,))
        db.execute("DELETE FROM turns_fts WHERE conversation_uuid=?", (uuid,))

        for msg in full_convo.get("chat_messages", []):
            role = msg.get("sender", "")
            text = msg.get("text", "")
            if not role or not text:
                continue
            db.execute("""
                INSERT INTO turns (conversation_uuid, turn_index, role, content, created_at, message_uuid)
                VALUES (?,?,?,?,?,?)
            """, (uuid, msg.get("index", 0), role, text, msg.get("created_at", ""), msg.get("uuid", "")))
            row_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute("INSERT INTO turns_fts (rowid, content, conversation_uuid, role) VALUES (?,?,?,?)",
                       (row_id, text, uuid, role))

        stats["new" if is_new else "updated"] += 1

    return stats


# ── Sync: Projects + Docs ───────────────────────────────────────────────────

def sync_projects(db: sqlite3.Connection, session_key: str, org_id: str,
                  full: bool = False) -> dict:
    stats = {"projects": 0, "docs_new": 0, "docs_updated": 0, "docs_unchanged": 0, "errors": 0}

    remote_projects = _api_get("projects", session_key, org_id)
    if not remote_projects or not isinstance(remote_projects, list):
        return stats

    for proj in remote_projects:
        proj_uuid = proj["uuid"]
        proj_name = proj.get("name", "unnamed")

        db.execute("""
            INSERT OR REPLACE INTO projects (uuid, name, description, created_at, updated_at, synced_at)
            VALUES (?,?,?,?,?,?)
        """, (proj_uuid, proj_name, proj.get("description", ""),
              proj.get("created_at", ""), proj.get("updated_at", ""), time.time()))
        stats["projects"] += 1

        # Fetch docs for this project
        remote_docs = _api_get(f"projects/{proj_uuid}/docs", session_key, org_id)
        if not remote_docs or not isinstance(remote_docs, list):
            continue

        # Create project directory
        proj_dir = DOCS_DIR / _safe_dirname(proj_name)
        proj_dir.mkdir(parents=True, exist_ok=True)

        for doc in remote_docs:
            doc_uuid = doc.get("uuid", "")
            file_name = doc.get("file_name", "untitled.txt")
            content = doc.get("content", "")
            doc_created = doc.get("created_at", "")

            # Delta check — compare content hash since docs don't have updated_at
            local_doc = db.execute("SELECT content FROM docs WHERE uuid=?", (doc_uuid,)).fetchone()
            if local_doc and local_doc["content"] == content and not full:
                stats["docs_unchanged"] += 1
                continue

            is_new = local_doc is None

            # Write file to disk
            file_path = proj_dir / file_name
            file_path.write_text(content, encoding="utf-8")

            # Store in DB (content for FTS, path for direct access)
            db.execute("""
                INSERT OR REPLACE INTO docs (uuid, project_uuid, file_name, content, estimated_tokens,
                                             created_at, local_path, synced_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (doc_uuid, proj_uuid, file_name, content,
                  doc.get("estimated_token_count", 0), doc_created,
                  str(file_path), time.time()))

            stats["docs_new" if is_new else "docs_updated"] += 1

        # Also sync project conversations
        proj_convos = _api_get(f"projects/{proj_uuid}/conversations", session_key, org_id)
        if proj_convos and isinstance(proj_convos, list):
            for pc in proj_convos:
                db.execute("UPDATE conversations SET project_uuid=? WHERE uuid=?",
                           (proj_uuid, pc["uuid"]))

    # Rebuild docs FTS
    try:
        db.execute("DELETE FROM docs_fts")
        db.execute("INSERT INTO docs_fts (rowid, content, file_name, project_uuid) SELECT rowid, content, file_name, project_uuid FROM docs")
    except Exception as e:
        log.warning("FTS rebuild: %s", e)

    return stats


# ── Main sync entry point ───────────────────────────────────────────────────

def sync(full: bool = False) -> dict:
    """Run a full sync cycle. Returns stats dict.

    Pure API → storage pipeline. No LLM involved.
    """
    session_key = get_session_key()
    if not session_key:
        return {"error": "Could not extract session key. Is Claude Desktop installed and logged in?"}

    org_id = get_org_id()
    if not org_id:
        return {"error": "Could not determine organization ID."}

    db = init_db()

    log.info("Syncing conversations...")
    convo_stats = sync_conversations(db, session_key, org_id, full=full)

    log.info("Syncing projects + docs...")
    proj_stats = sync_projects(db, session_key, org_id, full=full)

    db.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync', ?)",
               (str(time.time()),))
    db.commit()
    db.close()

    return {"conversations": convo_stats, "projects": proj_stats}


# ── Query functions (for MCP tools) ─────────────────────────────────────────

def search(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across conversations AND docs."""
    if not SYNC_DB.exists():
        return []
    db = sqlite3.connect(str(SYNC_DB))
    db.row_factory = sqlite3.Row

    results = []

    # Search conversation turns
    rows = db.execute("""
        SELECT t.content, t.role, t.conversation_uuid, t.turn_index,
               c.name as convo_name, c.model, c.updated_at,
               snippet(turns_fts, 0, '>>>', '<<<', '...', 40) as snippet
        FROM turns_fts
        JOIN turns t ON turns_fts.rowid = t.id
        JOIN conversations c ON t.conversation_uuid = c.uuid
        WHERE turns_fts MATCH ?
        ORDER BY rank LIMIT ?
    """, (query, limit)).fetchall()
    for r in rows:
        results.append({**dict(r), "source": "conversation"})

    # Search docs
    rows = db.execute("""
        SELECT d.file_name, d.project_uuid, d.local_path, d.estimated_tokens,
               p.name as project_name,
               snippet(docs_fts, 0, '>>>', '<<<', '...', 40) as snippet
        FROM docs_fts
        JOIN docs d ON docs_fts.rowid = d.rowid
        LEFT JOIN projects p ON d.project_uuid = p.uuid
        WHERE docs_fts MATCH ?
        ORDER BY rank LIMIT ?
    """, (query, limit)).fetchall()
    for r in rows:
        results.append({**dict(r), "source": "document"})

    db.close()
    return results


def list_conversations_local(limit: int = 50, query: str = "") -> list[dict]:
    if not SYNC_DB.exists():
        return []
    db = sqlite3.connect(str(SYNC_DB))
    db.row_factory = sqlite3.Row
    if query:
        rows = db.execute("""
            SELECT uuid, name, model, created_at, updated_at, project_uuid, is_starred,
                   (SELECT COUNT(*) FROM turns WHERE conversation_uuid=uuid) as turn_count
            FROM conversations WHERE name LIKE ? ORDER BY updated_at DESC LIMIT ?
        """, (f"%{query}%", limit)).fetchall()
    else:
        rows = db.execute("""
            SELECT uuid, name, model, created_at, updated_at, project_uuid, is_starred,
                   (SELECT COUNT(*) FROM turns WHERE conversation_uuid=uuid) as turn_count
            FROM conversations ORDER BY updated_at DESC LIMIT ?
        """, (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def list_projects_local() -> list[dict]:
    if not SYNC_DB.exists():
        return []
    db = sqlite3.connect(str(SYNC_DB))
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT p.uuid, p.name, p.description,
               (SELECT COUNT(*) FROM docs WHERE project_uuid=p.uuid) as doc_count,
               (SELECT COUNT(*) FROM conversations WHERE project_uuid=p.uuid) as convo_count
        FROM projects p ORDER BY p.name
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def list_docs_local(project_uuid: str = "") -> list[dict]:
    if not SYNC_DB.exists():
        return []
    db = sqlite3.connect(str(SYNC_DB))
    db.row_factory = sqlite3.Row
    if project_uuid:
        rows = db.execute("""
            SELECT d.uuid, d.file_name, d.estimated_tokens, d.local_path, d.created_at, p.name as project_name
            FROM docs d LEFT JOIN projects p ON d.project_uuid=p.uuid
            WHERE d.project_uuid LIKE ? ORDER BY d.file_name
        """, (f"{project_uuid}%",)).fetchall()
    else:
        rows = db.execute("""
            SELECT d.uuid, d.file_name, d.estimated_tokens, d.local_path, d.created_at, p.name as project_name
            FROM docs d LEFT JOIN projects p ON d.project_uuid=p.uuid
            ORDER BY p.name, d.file_name
        """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def read_conversation_local(uuid_prefix: str, limit: int = 100) -> dict | None:
    if not SYNC_DB.exists():
        return None
    db = sqlite3.connect(str(SYNC_DB))
    db.row_factory = sqlite3.Row
    convo = db.execute("SELECT * FROM conversations WHERE uuid LIKE ?", (f"{uuid_prefix}%",)).fetchone()
    if not convo:
        db.close()
        return None
    turns = db.execute("""
        SELECT role, content, created_at, turn_index FROM turns
        WHERE conversation_uuid=? ORDER BY turn_index LIMIT ?
    """, (convo["uuid"], limit)).fetchall()
    db.close()
    return {"conversation": dict(convo), "turns": [dict(t) for t in turns]}


def get_sync_status() -> dict:
    if not SYNC_DB.exists():
        return {"synced": False}
    db = sqlite3.connect(str(SYNC_DB))
    db.row_factory = sqlite3.Row
    result = {
        "synced": True,
        "conversations": db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        "turns": db.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
        "projects": db.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
        "docs": db.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
        "last_sync": None,
        "db_path": str(SYNC_DB),
        "docs_path": str(DOCS_DIR),
    }
    row = db.execute("SELECT value FROM sync_state WHERE key='last_sync'").fetchone()
    if row:
        result["last_sync"] = float(row["value"])
    db.close()
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    parser = argparse.ArgumentParser(description="Sync Claude.ai conversations + docs")
    parser.add_argument("command", nargs="?", default="sync",
                        choices=["sync", "status", "search", "list", "projects", "docs"])
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--full", action="store_true", help="Force full re-sync")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if args.command == "sync":
        print("Syncing Claude.ai...")
        stats = sync(full=args.full)
        print(json.dumps(stats, indent=2))
    elif args.command == "status":
        print(json.dumps(get_sync_status(), indent=2))
    elif args.command == "search":
        for r in search(args.query or "", limit=args.limit):
            src = r["source"]
            if src == "conversation":
                print(f"\n[conv] {r['convo_name']} ({r['role']})")
            else:
                print(f"\n[doc] {r['project_name']}/{r['file_name']}")
            print(f"  {r['snippet']}")
    elif args.command == "list":
        for c in list_conversations_local(limit=args.limit, query=args.query or ""):
            print(f"  [{c['uuid'][:8]}] {c['updated_at'][:10]} {c['turn_count']:>3}t  {c['name']}")
    elif args.command == "projects":
        for p in list_projects_local():
            print(f"  [{p['uuid'][:8]}] {p['name']} — {p['doc_count']} docs, {p['convo_count']} convos")
    elif args.command == "docs":
        for d in list_docs_local():
            print(f"  [{d['project_name']}] {d['file_name']} ({d['estimated_tokens']}t) → {d['local_path']}")


if __name__ == "__main__":
    main()
