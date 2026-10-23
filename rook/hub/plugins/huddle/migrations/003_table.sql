-- The conference table and votes (docs/design/huddles.md 10, B4). New
-- tables in chat.db under namespace 'huddle'. Released: never edit this
-- file; later changes are new, higher-numbered files.
-- An artifact pinned on a huddle's table, at an exact version (a commit
-- SHA, or a content hash). ``ref`` is what it points at (repo, number,
-- sha, path, url); ``content`` holds a pinned document's text.
CREATE TABLE IF NOT EXISTS huddle_artifacts(
    id TEXT PRIMARY KEY, room TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '{}', version TEXT NOT NULL, pinned_by TEXT NOT NULL,
    pinned REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pinned', note TEXT,
    content TEXT, superseded_by TEXT);
CREATE INDEX IF NOT EXISTS huddle_artifacts_room ON huddle_artifacts(room, pinned);
-- A vote on one action (merge, deploy, qa, ship) of one pinned artifact at
-- one version. ``seats`` are the required voters, ``gates`` the hard checks
-- (not voted on), ``decision`` the trust map's ruling and what a pass does.
CREATE TABLE IF NOT EXISTS huddle_motions(
    id TEXT PRIMARY KEY, room TEXT NOT NULL, kind TEXT NOT NULL, artifact TEXT NOT NULL,
    version TEXT NOT NULL, target TEXT NOT NULL DEFAULT '{}', proposer TEXT NOT NULL,
    reason TEXT, seats TEXT NOT NULL DEFAULT '[]', excluded TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL DEFAULT '{}', gates TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 1, result TEXT NOT NULL DEFAULT '{}',
    ask TEXT, created REAL NOT NULL, updated REAL NOT NULL, ended REAL);
CREATE INDEX IF NOT EXISTS huddle_motions_room ON huddle_motions(room, created);
-- One sealed ballot: committed (``commitment``) when cast, shown to nobody
-- until every required voter has voted (``revealed``).
CREATE TABLE IF NOT EXISTS huddle_ballots(
    motion TEXT NOT NULL, round INTEGER NOT NULL, voter TEXT NOT NULL, seat TEXT NOT NULL,
    vote TEXT NOT NULL, reasons TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]',
    risk TEXT, version TEXT NOT NULL, reads TEXT NOT NULL DEFAULT '[]', note TEXT,
    commitment TEXT NOT NULL, salt TEXT NOT NULL, cast_at REAL NOT NULL,
    revealed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(motion, round, voter));
