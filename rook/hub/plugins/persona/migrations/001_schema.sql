-- Persona profiles, their scoped assignments and an attributed history.
CREATE TABLE IF NOT EXISTS persona_profiles (
    id      TEXT PRIMARY KEY,
    rev     INTEGER NOT NULL,
    doc     TEXT NOT NULL,          -- JSON profile document
    updated REAL NOT NULL,
    actor   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_assignments (
    scope   TEXT NOT NULL,          -- default | band | family | user
    target  TEXT NOT NULL,          -- '' for default; band id, family name or user id
    profile TEXT NOT NULL,
    updated REAL NOT NULL,
    actor   TEXT NOT NULL,
    PRIMARY KEY (scope, target)
);

CREATE TABLE IF NOT EXISTS persona_history (
    seq     INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,          -- profile | assign
    ref     TEXT NOT NULL,          -- profile id, or scope:target
    rev     INTEGER,
    doc     TEXT,                   -- JSON after the change; NULL = removed
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS persona_history_ref ON persona_history(ref, seq);
