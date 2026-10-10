-- Job history (docs/design/jobs.md 7): one row per change to a job, so an
-- identity reset (a non-owner edited the job) or a pause for a revoked
-- identity is on record with the revision it produced.

CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    revision  INTEGER,
    at        REAL NOT NULL,
    actor     TEXT,
    action    TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS history_job ON history(job_id, id);
