CREATE TABLE IF NOT EXISTS questions (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    asked_by TEXT,
    question TEXT NOT NULL,
    options JSONB NOT NULL,
    allow_text BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL,
    answer JSONB,
    answered_by TEXT,
    answered_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    untrusted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS questions_status ON questions(status, created_at);
CREATE INDEX IF NOT EXISTS questions_thread ON questions(thread_key, created_at);
CREATE INDEX IF NOT EXISTS questions_run ON questions(run_id);
