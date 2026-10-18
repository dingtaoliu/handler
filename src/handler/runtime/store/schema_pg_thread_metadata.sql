CREATE TABLE IF NOT EXISTS thread_names (
    thread_key TEXT PRIMARY KEY REFERENCES threads(key),
    title TEXT NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS thread_views (
    thread_key TEXT NOT NULL REFERENCES threads(key),
    user_id TEXT NOT NULL,
    read_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(thread_key, user_id)
);
CREATE INDEX IF NOT EXISTS messages_thread_activity ON messages(thread_key, created_at, role);
CREATE INDEX IF NOT EXISTS events_thread_received ON events(thread_key, received_at);
