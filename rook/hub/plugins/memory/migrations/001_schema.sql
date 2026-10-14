-- Home agent memory (rook/hub/plugins/memory; docs/design/memory.md).
-- One row per remembered statement. Corrections never edit a row in place:
-- a new row supersedes the old one, so the history stays readable.
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,              -- profile | fact | episode | procedure
    scope         TEXT NOT NULL,              -- shared | a person's identity (user:operator)
    text          TEXT NOT NULL,
    hash          TEXT NOT NULL,              -- normalised text, for exact dedupe
    slot          TEXT,                       -- "name", "favourite colour": a new value supersedes
    state         TEXT NOT NULL,              -- active | superseded | archived | forgotten
    confidence    REAL NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0, -- confirmed by a person: never decays or is evicted
    uses          INTEGER NOT NULL DEFAULT 0, -- times recalled into a prompt
    reinforced    INTEGER NOT NULL DEFAULT 0, -- times the same thing was said again
    last_used     REAL,
    created       REAL NOT NULL,
    updated       REAL NOT NULL,
    source        TEXT NOT NULL,              -- chat | ask | tool | page | agent | job | consolidation
    author        TEXT NOT NULL,              -- who the statement came from
    room          TEXT,                       -- chat room it came from, if any
    ref           TEXT,                       -- evidence: journal id, job run, message seq
    supersedes    TEXT NOT NULL DEFAULT '[]', -- JSON list of memory ids
    superseded_by TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    reason        TEXT                        -- why archived / forgotten / superseded
);
CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope, kind, state);
CREATE INDEX IF NOT EXISTS memories_hash ON memories(hash, scope, kind);
CREATE INDEX IF NOT EXISTS memories_slot ON memories(slot, scope, kind);
CREATE INDEX IF NOT EXISTS memories_state ON memories(state, updated);

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

-- One float32 unit vector per memory and embedding model.
CREATE TABLE IF NOT EXISTS memory_vectors (
    id     TEXT NOT NULL,
    model  TEXT NOT NULL,
    dim    INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (id, model)
);

-- Append-only history of every change, shown on the Memory page.
CREATE TABLE IF NOT EXISTS memory_history (
    seq    INTEGER PRIMARY KEY,
    id     TEXT NOT NULL,
    action TEXT NOT NULL,
    actor  TEXT NOT NULL,
    ts     REAL NOT NULL,
    data   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS memory_history_id ON memory_history(id, seq);

-- Conversation turns waiting to be summarised into an episode by the
-- consolidation job. Deleted once summarised: the chat store keeps the
-- transcript; memory keeps only the summary.
CREATE TABLE IF NOT EXISTS memory_turns (
    seq    INTEGER PRIMARY KEY,
    room   TEXT NOT NULL,
    scope  TEXT NOT NULL,
    role   TEXT NOT NULL,                     -- user | assistant
    sender TEXT NOT NULL,
    text   TEXT NOT NULL,
    ts     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_turns_room ON memory_turns(room, seq);

CREATE TABLE IF NOT EXISTS memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
