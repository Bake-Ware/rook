-- Hygiene findings (rook/hub/plugins/knowledge/hygiene.py): one row per
-- (record, kind, actor) while the condition holds. A new table and new
-- indexes only, so the layout of existing tables is unchanged and
-- user_version stays 2 (an older release ignores the table and keeps the
-- indexes up to date like any other). Released: never edit this file.
CREATE TABLE IF NOT EXISTS hygiene(
    id TEXT PRIMARY KEY, band TEXT NOT NULL, record TEXT NOT NULL, kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL, delivered REAL, deliveries INTEGER NOT NULL DEFAULT 0, resolved REAL);
CREATE UNIQUE INDEX IF NOT EXISTS hygiene_open ON hygiene(record, kind, actor) WHERE resolved IS NULL;
CREATE INDEX IF NOT EXISTS hygiene_actor ON hygiene(actor, resolved);
CREATE INDEX IF NOT EXISTS hygiene_key ON hygiene(record, kind, actor, created);
-- "Did this claimant write a page while working on the task?" (the
-- done_without_knowledge check) looks events up by actor and time.
CREATE INDEX IF NOT EXISTS events_actor ON events(actor, ts);
