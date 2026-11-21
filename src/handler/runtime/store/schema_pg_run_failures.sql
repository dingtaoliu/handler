-- Runs that failed for good; see schema_run_failures.sql. Idempotent: applied
-- at every start.
CREATE TABLE IF NOT EXISTS run_failures (
    run_id TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL,
    thread_type TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL,
    event_ids JSONB NOT NULL,
    consumed_by TEXT NOT NULL,
    failed_at TIMESTAMPTZ NOT NULL,
    replayed_at TIMESTAMPTZ,
    replayed_by TEXT
);
CREATE INDEX IF NOT EXISTS run_failures_failed ON run_failures(failed_at, run_id);
