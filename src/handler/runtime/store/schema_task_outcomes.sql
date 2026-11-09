CREATE TABLE IF NOT EXISTS task_outcomes (
 id TEXT PRIMARY KEY,
 thread_key TEXT NOT NULL,
 report TEXT NOT NULL DEFAULT '{}',
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_run_links (
 run_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES task_outcomes(id)
);
CREATE INDEX IF NOT EXISTS task_run_links_task ON task_run_links(task_id);
CREATE TABLE IF NOT EXISTS task_receipts (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES task_outcomes(id),
 run_id TEXT NOT NULL,
 tool TEXT NOT NULL,
 operation TEXT NOT NULL,
 status TEXT NOT NULL,
 facts TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_receipts_task ON task_receipts(task_id, created_at, id);
CREATE TABLE IF NOT EXISTS task_job_links (
 task_id TEXT NOT NULL REFERENCES task_outcomes(id),
 resource_key TEXT NOT NULL,
 state TEXT NOT NULL,
 receipt_id TEXT NOT NULL,
 PRIMARY KEY(task_id, resource_key)
);
CREATE INDEX IF NOT EXISTS task_job_links_resource ON task_job_links(resource_key);
