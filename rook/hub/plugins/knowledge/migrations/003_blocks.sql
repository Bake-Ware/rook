-- Block-level embeddings (rook/hub/plugins/knowledge/chunk.py): a record body
-- splits into blocks at markdown headings; each block's vector is keyed by a
-- digest of its text, so an edit re-embeds only the blocks that changed.
-- ``blocks`` holds the current chunking of each record (at ``revision``);
-- ``block_vectors`` holds unit-length vectors per (digest, model). New tables
-- only: user_version stays 2 and an older release keeps using ``embeddings``.
-- Released: never edit this file.
CREATE TABLE IF NOT EXISTS blocks(
    record TEXT NOT NULL REFERENCES records(id), ord INTEGER NOT NULL,
    revision INTEGER NOT NULL, heading TEXT NOT NULL, start INTEGER NOT NULL,
    "end" INTEGER NOT NULL, hash TEXT NOT NULL, PRIMARY KEY(record, ord));
CREATE INDEX IF NOT EXISTS blocks_hash ON blocks(hash);
CREATE TABLE IF NOT EXISTS block_vectors(
    hash TEXT NOT NULL, model TEXT NOT NULL, vector TEXT NOT NULL, PRIMARY KEY(hash, model));
