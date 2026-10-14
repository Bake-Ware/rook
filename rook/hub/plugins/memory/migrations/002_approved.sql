-- Procedures steer the home agent's tool use, so only the ones the operator
-- approved (or pinned) are read into a prompt. Learned ones start unapproved.
ALTER TABLE memories ADD COLUMN approved INTEGER NOT NULL DEFAULT 0;
