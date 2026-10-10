-- Jobs (docs/design/jobs.md). All instants are epoch seconds (UTC).

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    owner         TEXT NOT NULL,
    owner_info    TEXT NOT NULL DEFAULT '{}',
    enabled       INTEGER NOT NULL DEFAULT 1,
    paused_reason TEXT,
    definition    TEXT NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1,
    created       REAL NOT NULL,
    updated       REAL NOT NULL,
    updated_by    TEXT
);

-- Scheduling state per time trigger (cron / at / after). next_at is the
-- next unprocessed fire; a scheduler moves it with a compare-and-set, so two
-- hub processes never fire the same occurrence. anchor: the run an `after`
-- trigger last scheduled from.
CREATE TABLE IF NOT EXISTS triggers (
    job_id   TEXT NOT NULL,
    idx      INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    next_at  REAL,
    done     INTEGER NOT NULL DEFAULT 0,
    anchor   TEXT,
    PRIMARY KEY (job_id, idx)
);

-- Runs. state: queued | due | running | success | failure | hang |
-- interrupted | dropped | blocked | cancelled. A scheduler claims a due run
-- with one UPDATE ... WHERE state='due' AND lease_until < now, and renews
-- lease_until while it runs.
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    job_name      TEXT NOT NULL,
    job_revision  INTEGER,
    trigger       TEXT NOT NULL,
    missed        INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL,
    scheduled     REAL,
    created       REAL NOT NULL,
    started       REAL,
    finished      REAL,
    identity_used TEXT,
    vars          TEXT NOT NULL DEFAULT '{}',
    steps         TEXT NOT NULL DEFAULT '{}',
    alerts        TEXT,
    error         TEXT,
    executions    INTEGER NOT NULL DEFAULT 0,
    lease_owner   TEXT,
    lease_until   REAL NOT NULL DEFAULT 0,
    cancel        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job_id, created);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state, lease_until);
CREATE INDEX IF NOT EXISTS runs_finished ON runs(finished);
