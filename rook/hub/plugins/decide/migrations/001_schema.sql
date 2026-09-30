-- decide.drive journal: one row per run, one row per frame.
CREATE TABLE IF NOT EXISTS drive_runs (
    id            TEXT PRIMARY KEY,
    created       REAL NOT NULL,
    identity      TEXT NOT NULL DEFAULT '',
    goal          TEXT NOT NULL,
    screen_worker TEXT NOT NULL,
    input_worker  TEXT NOT NULL,
    dry_run       INTEGER NOT NULL,
    state         TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    steps         INTEGER NOT NULL DEFAULT 0,
    backend       TEXT NOT NULL DEFAULT '',
    config        TEXT NOT NULL DEFAULT '{}',
    finished      REAL
);
CREATE INDEX IF NOT EXISTS drive_runs_created ON drive_runs(created);

CREATE TABLE IF NOT EXISTS drive_steps (
    run_id   TEXT NOT NULL,
    n        INTEGER NOT NULL,
    ts       REAL NOT NULL,
    outcome  TEXT NOT NULL DEFAULT '',
    data     TEXT NOT NULL,
    PRIMARY KEY (run_id, n)
);
