CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    connector TEXT NOT NULL,
    owner TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_ref TEXT,
    secret_ref TEXT,
    scopes TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS connections_lookup ON connections(connector, owner, status);
CREATE INDEX IF NOT EXISTS connections_owner ON connections(owner, created_at);
