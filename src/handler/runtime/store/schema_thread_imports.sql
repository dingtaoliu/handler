-- Conversations imported from another system (store/thread_imports.py).
CREATE TABLE IF NOT EXISTS thread_imports (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    thread_key TEXT NOT NULL UNIQUE REFERENCES threads(key),
    content_hash TEXT NOT NULL,
    messages INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
