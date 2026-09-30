-- Knowledge + work records, schema layout 2 (the pre-plugin store's
-- PRAGMA user_version=2). Everything is IF NOT EXISTS: on a database written
-- by the pre-plugin code (already at layout 2 after store._upgrade_legacy) it
-- changes nothing and just records the baseline. Released: never edit this
-- file; later changes are new, higher-numbered files.
CREATE TABLE IF NOT EXISTS actors(id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
    updated REAL NOT NULL, info TEXT);
CREATE TABLE IF NOT EXISTS records(
    id TEXT PRIMARY KEY, band TEXT NOT NULL, kind TEXT NOT NULL,
    parent TEXT REFERENCES records(id), title TEXT NOT NULL, body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
    scope_revision INTEGER NOT NULL DEFAULT 1, attrs TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL, creator TEXT NOT NULL, slug TEXT);
CREATE INDEX IF NOT EXISTS records_band_kind ON records(band,kind,state,updated);
CREATE INDEX IF NOT EXISTS records_parent ON records(parent);
CREATE UNIQUE INDEX IF NOT EXISTS records_slug ON records(band,slug);
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY, band TEXT NOT NULL, record TEXT NOT NULL,
    actor TEXT NOT NULL, action TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_record ON events(band,record,seq);
CREATE TABLE IF NOT EXISTS receipts(
    band TEXT NOT NULL, actor TEXT NOT NULL, request TEXT NOT NULL,
    digest TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(band,actor,request));
CREATE TABLE IF NOT EXISTS embeddings(
    record TEXT PRIMARY KEY REFERENCES records(id), revision INTEGER NOT NULL,
    model TEXT NOT NULL, vector TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursors(name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED,title,body);
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(id,title,body) VALUES(new.id,new.title,new.body); END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
    DELETE FROM records_fts WHERE id=old.id;
    INSERT INTO records_fts(id,title,body) VALUES(new.id,new.title,new.body); END;
CREATE TABLE IF NOT EXISTS links(
    id TEXT PRIMARY KEY, band TEXT NOT NULL, record TEXT NOT NULL REFERENCES records(id),
    kind TEXT NOT NULL, ref TEXT NOT NULL, relation TEXT NOT NULL, note TEXT NOT NULL,
    actor TEXT NOT NULL, ts REAL NOT NULL, auto INTEGER NOT NULL DEFAULT 0,
    retracts TEXT);
CREATE INDEX IF NOT EXISTS links_record ON links(record,ts);
CREATE INDEX IF NOT EXISTS links_ref ON links(kind,ref);
CREATE TABLE IF NOT EXISTS claims(
    id TEXT PRIMARY KEY, band TEXT NOT NULL, task TEXT NOT NULL REFERENCES records(id),
    actor TEXT NOT NULL, host TEXT, client TEXT, dir TEXT, provider_session TEXT,
    started REAL NOT NULL, last_active REAL NOT NULL, released REAL,
    nudged REAL, dirty REAL);
CREATE INDEX IF NOT EXISTS claims_active ON claims(actor,released,started);
CREATE INDEX IF NOT EXISTS claims_task ON claims(task,released);
