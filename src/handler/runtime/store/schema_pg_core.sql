CREATE TABLE IF NOT EXISTS threads (
    seq BIGSERIAL,
    key TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    lane TEXT NOT NULL,
    owner_user_id TEXT,
    harness TEXT NOT NULL,
    model TEXT,
    priority INTEGER NOT NULL,
    session_id TEXT,
    pending_since TIMESTAMPTZ,
    available_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dead BOOLEAN NOT NULL DEFAULT FALSE,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    lease_version INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS threads_pending
    ON threads(lane, priority, pending_since) WHERE pending_since IS NOT NULL;

CREATE TABLE IF NOT EXISTS events (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload JSONB NOT NULL,
    user_id TEXT,
    thread_key TEXT,
    status TEXT NOT NULL,
    consumed_by_run TEXT,
    received_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS events_pending ON events(thread_key, consumed_by_run);

CREATE TABLE IF NOT EXISTS runs (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL REFERENCES threads(key),
    lease_version INTEGER NOT NULL,
    trigger_event_ids JSONB NOT NULL,
    status TEXT NOT NULL,
    harness TEXT NOT NULL,
    model TEXT,
    parent_run_id TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    error TEXT,
    usage JSONB NOT NULL,
    cost_usd DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS messages (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL REFERENCES threads(key),
    role TEXT NOT NULL,
    blocks JSONB NOT NULL,
    run_id TEXT REFERENCES runs(id),
    raw JSONB,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_key, created_at, seq);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    model TEXT,
    usage JSONB,
    cost_usd DOUBLE PRECISION,
    data JSONB NOT NULL,
    raw_ref TEXT,
    parent_seq INTEGER,
    PRIMARY KEY (run_id, seq)
);
