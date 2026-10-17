CREATE TABLE IF NOT EXISTS schedules (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    spec TEXT NOT NULL,
    created_by TEXT,
    timezone TEXT NOT NULL,
    notify JSONB NOT NULL,
    harness TEXT,
    model TEXT,
    enabled BOOLEAN NOT NULL,
    one_shot BOOLEAN NOT NULL,
    parent_thread_key TEXT,
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules(next_run_at, id) WHERE enabled;

CREATE TABLE IF NOT EXISTS approvals (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    tool TEXT NOT NULL,
    action TEXT NOT NULL,
    args JSONB NOT NULL,
    requested_by TEXT,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    result JSONB,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    run_id TEXT,
    thread_key TEXT,
    actor TEXT,
    tool TEXT NOT NULL,
    action TEXT NOT NULL,
    decision TEXT NOT NULL,
    status TEXT NOT NULL,
    args_digest TEXT NOT NULL DEFAULT '',
    args_preview TEXT NOT NULL DEFAULT '',
    result_size INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    approval_id TEXT,
    at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_run ON audit_log(run_id, at);

CREATE TABLE IF NOT EXISTS memory_topics (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_versions (
    section TEXT NOT NULL,
    version INTEGER NOT NULL,
    value JSONB NOT NULL,
    schema_version INTEGER NOT NULL,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (section, version)
);

CREATE TABLE IF NOT EXISTS notifications (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    run_id TEXT,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    read_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, created_at);

CREATE TABLE IF NOT EXISTS connections (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    connector TEXT NOT NULL,
    owner TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_ref TEXT,
    secret_ref TEXT,
    scopes JSONB NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS connections_lookup ON connections(connector, owner, status);
CREATE INDEX IF NOT EXISTS connections_owner ON connections(owner, created_at);
