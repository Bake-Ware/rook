-- Chaired huddles (docs/design/huddles.md 5): a huddle the house agent chairs
-- with a set of roles. ``roles`` is the JSON list of role ids (NULL: a
-- status huddle as before, never chaired; '[]': a 1:1 with the chair).
-- ``archived`` hides a huddle from the list; nothing is deleted.
ALTER TABLE huddles ADD COLUMN roles TEXT;
ALTER TABLE huddles ADD COLUMN archived REAL;
-- One round: a person's post, the chair's routing, the roles' replies and
-- the chair's close. ``plan`` is what the chair decided (calls, answered),
-- ``replies`` each called role's progress ({role, ask, state, seq, error}).
CREATE TABLE IF NOT EXISTS huddle_rounds(
    id TEXT PRIMARY KEY, room TEXT NOT NULL, trigger_seq INTEGER NOT NULL,
    asker TEXT NOT NULL, state TEXT NOT NULL, plan TEXT NOT NULL DEFAULT '{}',
    replies TEXT NOT NULL DEFAULT '[]', close_seq INTEGER, error TEXT,
    started REAL NOT NULL, ended REAL);
CREATE INDEX IF NOT EXISTS huddle_rounds_room ON huddle_rounds(room, started);
-- Action cards: a concrete proposal (task, job, session, note, question)
-- that a person approves or dismisses. ``seq`` is the message it belongs to.
CREATE TABLE IF NOT EXISTS huddle_cards(
    id TEXT PRIMARY KEY, room TEXT NOT NULL, round TEXT, seq INTEGER,
    kind TEXT NOT NULL, by TEXT NOT NULL, summary TEXT NOT NULL, call TEXT,
    state TEXT NOT NULL, result TEXT, created REAL NOT NULL,
    decided_by TEXT, decided REAL);
CREATE INDEX IF NOT EXISTS huddle_cards_room ON huddle_cards(room, created);
