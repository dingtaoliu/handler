CREATE TABLE IF NOT EXISTS threads (
    key TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    lane TEXT NOT NULL,
    owner_user_id TEXT,
    harness TEXT NOT NULL,
    model TEXT,
    priority INTEGER NOT NULL,
    session_id TEXT,
    pending_since TEXT,
    available_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dead INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    lease_version INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS threads_pending
    ON threads(lane, priority, pending_since) WHERE pending_since IS NOT NULL;

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    user_id TEXT,
    thread_key TEXT,
    status TEXT NOT NULL,
    consumed_by_run TEXT,
    received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_pending ON events(thread_key, consumed_by_run);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL REFERENCES threads(key),
    lease_version INTEGER NOT NULL,
    trigger_event_ids TEXT NOT NULL,
    status TEXT NOT NULL,
    harness TEXT NOT NULL,
    model TEXT,
    parent_run_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    usage TEXT NOT NULL,
    cost_usd REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL REFERENCES threads(key),
    role TEXT NOT NULL,
    blocks TEXT NOT NULL,
    run_id TEXT REFERENCES runs(id),
    raw TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    model TEXT,
    usage TEXT,
    cost_usd REAL,
    data TEXT NOT NULL,
    raw_ref TEXT,
    parent_seq INTEGER,
    PRIMARY KEY (run_id, seq)
);
