-- Agent memory (rook/hub/plugins/memory). Separate from the curated wiki:
-- its own database file, scoped per user / band / agent family.
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    scope_kind    TEXT NOT NULL,              -- user | band | agent
    scope_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- profile | preference | fact | episode | procedure
    text          TEXT NOT NULL,
    hash          TEXT NOT NULL,              -- sha256 of the normalized text (exact dedupe)
    state         TEXT NOT NULL,              -- pending | active | superseded | archived | retracted | rejected
    confidence    REAL NOT NULL,
    reinforced    INTEGER NOT NULL DEFAULT 0, -- times a duplicate proposal confirmed it
    recalls       INTEGER NOT NULL DEFAULT 0,
    last_used     REAL,                       -- last recall or reinforcement
    created       REAL NOT NULL,
    updated       REAL NOT NULL,
    author        TEXT NOT NULL,              -- identity of the writer
    actor         TEXT,                       -- compound actor (token.client.host@dir)
    session       TEXT,                       -- MCP session or transcript session id
    journal       TEXT,                       -- journal call id(s) given as evidence
    task          TEXT,                       -- claimed task at write time
    source        TEXT,                       -- agent | transcript:<...> | vault:<...>
    supersedes    TEXT NOT NULL DEFAULT '[]', -- JSON list of memory ids
    superseded_by TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    reason        TEXT,                       -- why it is pending / rejected / archived
    warnings      TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope_kind, scope_id, state, kind);
CREATE INDEX IF NOT EXISTS memories_hash ON memories(hash, scope_kind, scope_id);
CREATE INDEX IF NOT EXISTS memories_source ON memories(source);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TABLE IF NOT EXISTS vectors (
    id     TEXT PRIMARY KEY,
    model  TEXT NOT NULL,
    dim    INTEGER NOT NULL,
    vector BLOB NOT NULL                     -- float32, unit length
);
-- Idempotent transcript ingest: one row per (worker, agent, session).
CREATE TABLE IF NOT EXISTS ingested (
    key        TEXT PRIMARY KEY,
    last_index INTEGER NOT NULL,
    episode    TEXT,
    updated    REAL NOT NULL
);
-- Append-only history of every state change.
CREATE TABLE IF NOT EXISTS history (
    seq    INTEGER PRIMARY KEY,
    id     TEXT NOT NULL,
    action TEXT NOT NULL,
    actor  TEXT NOT NULL,
    ts     REAL NOT NULL,
    data   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS history_id ON history(id, seq);
