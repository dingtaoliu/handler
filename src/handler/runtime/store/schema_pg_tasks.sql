CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at, id);
CREATE INDEX IF NOT EXISTS runs_thread_started ON runs(thread_key, started_at);
CREATE INDEX IF NOT EXISTS messages_run ON messages(run_id);
CREATE INDEX IF NOT EXISTS approvals_run ON approvals(run_id, status);
