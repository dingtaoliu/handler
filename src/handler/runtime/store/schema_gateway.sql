CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    tool TEXT NOT NULL,
    action TEXT NOT NULL,
    args TEXT NOT NULL,
    requested_by TEXT,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    result TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals (status, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
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
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_run ON audit_log (run_id, at);
