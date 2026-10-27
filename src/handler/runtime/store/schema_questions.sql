CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    asked_by TEXT,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    allow_text INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    answer TEXT,
    answered_by TEXT,
    answered_at TEXT,
    expires_at TEXT,
    untrusted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS questions_status ON questions (status, created_at);
CREATE INDEX IF NOT EXISTS questions_thread ON questions (thread_key, created_at);
CREATE INDEX IF NOT EXISTS questions_run ON questions (run_id);
