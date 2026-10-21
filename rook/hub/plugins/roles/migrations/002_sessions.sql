-- Coding sessions roles start and the hub follows (docs/design/huddles.md 4.5).
CREATE TABLE IF NOT EXISTS role_sessions (
    id           TEXT PRIMARY KEY,      -- s_<hex>
    role         TEXT NOT NULL,
    room         TEXT,                  -- the huddle it reports to ('' or NULL: none)
    worker       TEXT NOT NULL,
    terminal     TEXT,                  -- work.stream terminal id of the newest run
    native_id    TEXT,                  -- the Claude Code session id
    repo         TEXT,
    branch       TEXT,
    worktree     TEXT,
    title        TEXT,
    instructions TEXT,
    state        TEXT NOT NULL,         -- starting|running|done|blocked|failed|stopped
    status       TEXT,                  -- the newest status line
    pr_url       TEXT,
    cursor       INTEGER NOT NULL DEFAULT 0,
    started      REAL,
    updated      REAL,
    ended        REAL,
    last_output  REAL,
    runs         INTEGER NOT NULL DEFAULT 1,
    data         TEXT                   -- JSON: denials, tail, cost, reports
);
CREATE INDEX IF NOT EXISTS role_sessions_role ON role_sessions(role, started);
