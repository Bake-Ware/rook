-- Whiteboard (whiteboard.py): a living page per claim, one agent session on
-- one task, kept by the hub from the session's calls (``call`` rows carry the
-- call's why) plus what the agent jots (goal, decision, open, done, note).
-- ``n`` counts a run of the same call collapsed into one row; ``first`` is when
-- that run began and ``ts`` its latest call. ``closed`` marks an open item done.
-- New table only: an older release ignores it.
-- Released: never edit this file.
CREATE TABLE IF NOT EXISTS whiteboard(
    seq INTEGER PRIMARY KEY, band TEXT NOT NULL, task TEXT NOT NULL REFERENCES records(id),
    claim TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    worker TEXT, cap TEXT, ref TEXT, ok INTEGER, n INTEGER NOT NULL DEFAULT 1,
    first REAL NOT NULL, ts REAL NOT NULL, closed REAL);
CREATE INDEX IF NOT EXISTS whiteboard_claim ON whiteboard(claim, seq);
CREATE INDEX IF NOT EXISTS whiteboard_task ON whiteboard(task, seq);
