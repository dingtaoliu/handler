CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    spec TEXT NOT NULL,
    created_by TEXT,
    timezone TEXT NOT NULL,
    notify TEXT NOT NULL,
    harness TEXT,
    model TEXT,
    enabled INTEGER NOT NULL,
    one_shot INTEGER NOT NULL,
    parent_thread_key TEXT,
    next_run_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS schedules_due
    ON schedules(next_run_at, id) WHERE enabled = 1;
