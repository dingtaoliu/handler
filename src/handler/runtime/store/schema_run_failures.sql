-- Runs that failed for good (docs/rebuild/role-split-design.md, "Durability"). The
-- run's events are marked consumed (events.consumed_by_run = consumed_by) so they
-- never run again by themselves, and the thread stays claimable. One row per
-- failure, for the operator: listed by GET /api/runs/failed and made to run
-- once more by POST /api/runs/{run_id}/replay (replayed_at is set once).
CREATE TABLE IF NOT EXISTS run_failures (
    run_id TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL,
    thread_type TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL,
    event_ids TEXT NOT NULL,
    consumed_by TEXT NOT NULL,
    failed_at TEXT NOT NULL,
    replayed_at TEXT,
    replayed_by TEXT
);
CREATE INDEX IF NOT EXISTS run_failures_failed ON run_failures(failed_at, run_id);
