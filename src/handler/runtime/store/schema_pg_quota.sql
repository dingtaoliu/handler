-- Plan limits (docs/rebuild/plan-limits.md). Only registry-hosted tenants write
-- here. Idempotent: applied at every start (files run in name order).
CREATE TABLE IF NOT EXISTS input_usage (
    period TEXT PRIMARY KEY,
    used INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS held_inputs (
    seq BIGSERIAL,
    event_id TEXT PRIMARY KEY REFERENCES events(id),
    thread_spec JSONB NOT NULL,
    held_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS held_inputs_order ON held_inputs(seq);
CREATE TABLE IF NOT EXISTS quota_notices (
    period TEXT NOT NULL,
    subject TEXT NOT NULL,
    PRIMARY KEY (period, subject)
);
