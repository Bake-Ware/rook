-- Huddles (rook/hub/plugins/huddle): group chat rooms where agents report
-- status and ask the person questions. The messages themselves stay in the
-- chat store's ``messages`` table (so every chat client reads them as
-- text); these tables add the structure on top, keyed by room id and
-- message seq. New tables in chat.db only, recorded under namespace
-- 'huddle': the chat store's own tables are untouched. Released: never edit
-- this file; later changes are new, higher-numbered files.
CREATE TABLE IF NOT EXISTS huddles(
    room TEXT PRIMARY KEY, scope TEXT NOT NULL, ref TEXT NOT NULL, band TEXT,
    title TEXT NOT NULL, created REAL NOT NULL, creator TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS huddles_scope ON huddles(scope, ref);
-- Every structured post (status, question, answer), by its chat message seq.
CREATE TABLE IF NOT EXISTS huddle_posts(
    seq INTEGER PRIMARY KEY, room TEXT NOT NULL, sender TEXT NOT NULL,
    kind TEXT NOT NULL, data TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS huddle_posts_room ON huddle_posts(room, seq);
-- Each participant's latest status in a huddle (the status strip).
CREATE TABLE IF NOT EXISTS huddle_status(
    room TEXT NOT NULL, identity TEXT NOT NULL, state TEXT NOT NULL,
    data TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY(room, identity));
-- Questions for the person: pinned while open.
CREATE TABLE IF NOT EXISTS huddle_questions(
    seq INTEGER PRIMARY KEY, room TEXT NOT NULL, asker TEXT NOT NULL,
    text TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'open',
    asked REAL NOT NULL, answer TEXT, answered_by TEXT, answered_label TEXT,
    answered REAL, answer_seq INTEGER, wake TEXT);
CREATE INDEX IF NOT EXISTS huddle_questions_open ON huddle_questions(room, state);
CREATE INDEX IF NOT EXISTS huddle_questions_asker ON huddle_questions(asker, state);
