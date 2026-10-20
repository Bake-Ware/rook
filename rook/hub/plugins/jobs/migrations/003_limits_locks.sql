-- Limits and locks (docs/design/jobs.md 4.1, 7.1).
--
-- run_count / fail_streak drive limits.max_runs and limits.max_failures;
-- both restart from zero when the job is enabled again. A locked job can be
-- changed only by a person signed in to the dashboard. notices remembers what
-- each job last sent to each target, so the same text is not sent twice
-- inside limits.notify_window (only a digest of the text is kept).

ALTER TABLE jobs ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN locked_by TEXT;
ALTER TABLE jobs ADD COLUMN locked_at REAL;
ALTER TABLE jobs ADD COLUMN run_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS notices (
    job_id  TEXT NOT NULL,
    target  TEXT NOT NULL,
    digest  TEXT NOT NULL,
    at      REAL NOT NULL,
    run_id  TEXT,
    PRIMARY KEY (job_id, target, digest)
);
