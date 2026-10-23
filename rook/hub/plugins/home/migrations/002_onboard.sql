-- Project onboarding through conversation and the borrowed-state offers
-- (rook/hub/plugins/home/onboard.py, docs/design/onboarding.md): one thread
-- per conversation (brainstorm notes, the one-time offer, the intake
-- answers in the person's words, the plan read back, what setup did), the
-- projects the house agent set up (for check-ins), and which proactive
-- offers it already made (restore borrowed state, "still a toy?") so each is
-- made once. Released: never edit this file; later changes are new,
-- higher-numbered files.
CREATE TABLE IF NOT EXISTS onboard_threads(
    id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL, key TEXT NOT NULL,
    asker TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', room TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', slug TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', answers TEXT NOT NULL DEFAULT '{}',
    plan TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}', offered REAL);
CREATE INDEX IF NOT EXISTS onboard_threads_key ON onboard_threads(key, updated);
CREATE TABLE IF NOT EXISTS onboard_projects(
    slug TEXT PRIMARY KEY, created REAL NOT NULL, thread TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, level TEXT NOT NULL,
    words TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '',
    exceptions TEXT NOT NULL DEFAULT '[]', review_at REAL, state TEXT NOT NULL DEFAULT 'active',
    data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS onboard_offers(
    key TEXT PRIMARY KEY, created REAL NOT NULL, ask TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '');
