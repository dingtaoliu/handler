CREATE TABLE IF NOT EXISTS memory_topics (
    name TEXT PRIMARY KEY, description TEXT NOT NULL, content TEXT NOT NULL,
    version INTEGER NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings_versions (
    section TEXT NOT NULL, version INTEGER NOT NULL, value TEXT NOT NULL,
    schema_version INTEGER NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (section, version)
);
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_key TEXT NOT NULL,
    run_id TEXT, message TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, created_at);
