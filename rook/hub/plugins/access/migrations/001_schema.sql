-- User access grants: fine-grained permissions per user.
-- Design: [[users-access-trust-map-and-agent-sessions-bake-s-design-decisions-2026-10-26-voi]]
-- Grants are stored in the enrollment DB alongside accounts (AccountStore).

-- Grants: a user holds permission for a resource (band, machine, cap, knowledge area)
-- up to an optional tier ceiling (read/write/exec/admin).
CREATE TABLE IF NOT EXISTS user_access_grants (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('band', 'machine', 'cap', 'knowledge_area')),
    resource_id TEXT NOT NULL,  -- band_id, machine id, cap name glob, or knowledge area slug
    tier_ceiling TEXT,          -- read|write|exec|admin, or NULL for full access
    created REAL NOT NULL,
    created_by TEXT NOT NULL,   -- user_id or principal id of who granted it
    quoted_words TEXT NOT NULL DEFAULT '',  -- why it was granted
    revoked REAL,               -- timestamp when revoked, or NULL if active
    revoked_by TEXT,            -- who revoked it
    UNIQUE(user_id, resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS user_access_grants_history (
    seq INTEGER PRIMARY KEY,
    ref TEXT NOT NULL,          -- grant id
    doc TEXT,                   -- JSON after the change; NULL = deleted
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS user_access_grants_history_ref ON user_access_grants_history(ref, seq);
