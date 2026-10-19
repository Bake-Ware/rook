-- Huddle roles (rook/hub/plugins/roles) and an attributed history.
CREATE TABLE IF NOT EXISTS roles (
    id      TEXT PRIMARY KEY,
    rev     INTEGER NOT NULL,
    doc     TEXT NOT NULL,          -- JSON role document
    updated REAL NOT NULL,
    actor   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_history (
    seq     INTEGER PRIMARY KEY,
    ref     TEXT NOT NULL,          -- role id
    rev     INTEGER,
    doc     TEXT,                   -- JSON after the change; NULL = deleted
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS role_history_ref ON role_history(ref, seq);
