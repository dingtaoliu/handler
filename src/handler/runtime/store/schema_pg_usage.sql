-- Append-only provider ledger and immutable attribution snapshots. No historical backfill.
CREATE TABLE IF NOT EXISTS run_attributions (
 run_id TEXT PRIMARY KEY, thread_key TEXT NOT NULL, instance_name TEXT NOT NULL,
 initiating_user_id TEXT, attributed_user_id TEXT, thread_owner_user_id TEXT,
 attribution_reason TEXT NOT NULL, trigger_type TEXT NOT NULL,
 trigger_event_ids JSONB NOT NULL, captured_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_records (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, thread_key TEXT NOT NULL,
 request_id TEXT, tool_call_id TEXT, tool_name TEXT, record_kind TEXT NOT NULL,
 model TEXT, requested_model TEXT, service_tier TEXT, harness TEXT,
 metering_quality TEXT NOT NULL, status TEXT NOT NULL,
 started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ, occurred_at TIMESTAMPTZ NOT NULL,
 usage JSONB NOT NULL, cost_usd DOUBLE PRECISION, pricing JSONB NOT NULL,
 metadata JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_call_usage (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, thread_key TEXT NOT NULL,
 tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, request_id TEXT,
 started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ, status TEXT NOT NULL,
 input_tokens_estimate INTEGER, output_tokens_estimate INTEGER,
 output_bytes INTEGER NOT NULL, output_images INTEGER NOT NULL,
 single_read_context_cost_estimate_usd DOUBLE PRECISION, estimate_method TEXT NOT NULL,
 metadata JSONB NOT NULL, UNIQUE(run_id, tool_call_id)
);
CREATE INDEX IF NOT EXISTS attribution_user ON run_attributions(attributed_user_id, run_id);
CREATE INDEX IF NOT EXISTS attribution_thread ON run_attributions(thread_key, captured_at);
CREATE INDEX IF NOT EXISTS attribution_instance ON run_attributions(instance_name, captured_at);
CREATE INDEX IF NOT EXISTS usage_date ON usage_records(occurred_at, id);
CREATE INDEX IF NOT EXISTS usage_run ON usage_records(run_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS usage_thread ON usage_records(thread_key, occurred_at, id);
CREATE INDEX IF NOT EXISTS usage_model ON usage_records(model, occurred_at, id);
CREATE INDEX IF NOT EXISTS usage_tool ON usage_records(tool_name, occurred_at, id);
CREATE INDEX IF NOT EXISTS tool_usage_date ON tool_call_usage(started_at, id);
CREATE INDEX IF NOT EXISTS tool_usage_run ON tool_call_usage(run_id, started_at, id);
CREATE INDEX IF NOT EXISTS tool_usage_thread ON tool_call_usage(thread_key, started_at, id);
CREATE INDEX IF NOT EXISTS tool_usage_name ON tool_call_usage(tool_name, started_at, id);
