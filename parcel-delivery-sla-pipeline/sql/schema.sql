PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shipments (
    parcel_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    promised_at TEXT NOT NULL,
    region TEXT NOT NULL,
    carrier TEXT NOT NULL,
    service_level TEXT NOT NULL CHECK (service_level IN ('standard', 'express'))
);

CREATE TABLE IF NOT EXISTS scan_events (
    event_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES shipments(parcel_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('accepted', 'sorted', 'out_for_delivery', 'exception', 'delivered')),
    event_time TEXT NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scans_parcel_event_time
ON scan_events(parcel_id, event_time DESC);

CREATE TABLE IF NOT EXISTS rejected_rows (
    source_file TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    rejection_reason TEXT NOT NULL,
    raw_record TEXT NOT NULL,
    PRIMARY KEY (source_file, row_number, entity_type)
);

CREATE TABLE IF NOT EXISTS ingestion_audit (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    processed_at TEXT NOT NULL,
    source_file TEXT NOT NULL,
    input_rows INTEGER NOT NULL,
    inserted_rows INTEGER NOT NULL,
    duplicate_rows INTEGER NOT NULL,
    rejected_rows INTEGER NOT NULL
);
