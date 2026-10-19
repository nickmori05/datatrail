CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    key_column TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id),
    version INTEGER NOT NULL,
    source_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    source BLOB NOT NULL,
    UNIQUE(dataset_id, version),
    UNIQUE(dataset_id, sha256)
);

CREATE TABLE IF NOT EXISTS records (
    import_id INTEGER NOT NULL REFERENCES imports(id),
    number INTEGER NOT NULL,
    source_line INTEGER NOT NULL,
    entity_key TEXT,
    raw_json TEXT NOT NULL,
    data_json TEXT,
    issues_json TEXT NOT NULL,
    issue_count INTEGER NOT NULL,
    PRIMARY KEY(import_id, number)
);

CREATE UNIQUE INDEX IF NOT EXISTS clean_record_keys
ON records(import_id, entity_key) WHERE issue_count = 0;

CREATE INDEX IF NOT EXISTS record_entity ON records(entity_key, import_id);
