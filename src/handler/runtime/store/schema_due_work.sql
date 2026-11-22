-- Due work for the host timer (store/due_work.py); tenant is always 'local' in a
-- SQLite file. Postgres keeps these rows in handler_control.due_work instead.
CREATE TABLE IF NOT EXISTS due_work (
    tenant TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    due_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant, kind, ref)
);
CREATE INDEX IF NOT EXISTS due_work_due ON due_work(due_at);
