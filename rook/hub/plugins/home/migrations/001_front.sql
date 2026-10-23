-- The house agent as front door (rook/hub/plugins/home/front.py,
-- docs/design/front-door.md): every request it routed, the one-line asks it
-- put to the person (Yes / No / Explain, listed with the relayed approvals),
-- and the decisions it made for the person (the digest). Released: never
-- edit this file; later changes are new, higher-numbered files.
CREATE TABLE IF NOT EXISTS front_requests(
    id TEXT PRIMARY KEY, created REAL NOT NULL, source TEXT NOT NULL, asker TEXT NOT NULL,
    text TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', reply TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS front_requests_created ON front_requests(created);
CREATE TABLE IF NOT EXISTS front_asks(
    id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL, kind TEXT NOT NULL,
    state TEXT NOT NULL, line TEXT NOT NULL, explain TEXT NOT NULL, more TEXT NOT NULL,
    cite TEXT NOT NULL DEFAULT '{}', action TEXT NOT NULL DEFAULT '{}',
    change TEXT NOT NULL DEFAULT '{}', options TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '', room TEXT NOT NULL DEFAULT '',
    request TEXT NOT NULL DEFAULT '', asker TEXT NOT NULL DEFAULT '',
    decided REAL, decided_by TEXT, quote TEXT, answer TEXT, result TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS front_asks_state ON front_asks(state, created);
CREATE TABLE IF NOT EXISTS front_decisions(
    id TEXT PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
    cite TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT '',
    room TEXT NOT NULL DEFAULT '', request TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}', overturned TEXT);
CREATE INDEX IF NOT EXISTS front_decisions_created ON front_decisions(created);
