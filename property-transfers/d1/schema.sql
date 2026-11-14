CREATE TABLE IF NOT EXISTS batches (
 id TEXT PRIMARY KEY, source_month TEXT NOT NULL UNIQUE, source_name TEXT NOT NULL,
 source_sha TEXT NOT NULL, record_hash TEXT NOT NULL, expected_count INTEGER NOT NULL,
 metadata_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'staging' CHECK(status IN ('staging','active')),
 public_metadata TEXT, activated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES('public','{"revision":"empty","record_count":0,"source_count":0,"oldest_recorded_date":null,"latest_recorded_date":null,"generated_at":null,"options":{"county":[],"municipality":[],"conveyance_type":[],"property_use":[]}}');
CREATE TABLE IF NOT EXISTS transfers (
 rowid INTEGER PRIMARY KEY, transfer_id TEXT NOT NULL UNIQUE, batch_id TEXT NOT NULL REFERENCES batches(id),
 county TEXT NOT NULL, municipality TEXT NOT NULL, recorded_date TEXT NOT NULL,
 conveyance_type TEXT NOT NULL, property_use TEXT NOT NULL, parcel_number TEXT NOT NULL,
 sale_price_cents INTEGER, record_json TEXT NOT NULL, record_hash TEXT NOT NULL, search_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transfers_batch ON transfers(batch_id,transfer_id);
CREATE INDEX IF NOT EXISTS transfers_date ON transfers(recorded_date DESC,transfer_id DESC);
CREATE INDEX IF NOT EXISTS transfers_county ON transfers(county,recorded_date DESC,transfer_id DESC);
CREATE INDEX IF NOT EXISTS transfers_municipality ON transfers(municipality,recorded_date DESC,transfer_id DESC);
CREATE INDEX IF NOT EXISTS transfers_price ON transfers(sale_price_cents DESC,recorded_date DESC,transfer_id DESC);
CREATE INDEX IF NOT EXISTS transfers_parcel ON transfers(county,parcel_number);
CREATE INDEX IF NOT EXISTS transfers_type ON transfers(conveyance_type,recorded_date DESC,transfer_id DESC);
CREATE INDEX IF NOT EXISTS transfers_use ON transfers(property_use,recorded_date DESC,transfer_id DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS transfer_search USING fts5(search_text,content='transfers',content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS transfers_insert AFTER INSERT ON transfers BEGIN
 INSERT INTO transfer_search(rowid,search_text) VALUES(new.rowid,new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS transfers_delete AFTER DELETE ON transfers BEGIN
 INSERT INTO transfer_search(transfer_search,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
END;
-- Activation and the public summary change in the same SQL statement.
CREATE TRIGGER IF NOT EXISTS batch_activate AFTER UPDATE OF status ON batches
WHEN new.status='active' AND old.status='staging' BEGIN
 UPDATE settings SET value=new.public_metadata WHERE name='public';
END;
