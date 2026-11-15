-- Records a chat was opened from (docs/rebuild/memory-kinds.md, "Chat context
-- links"), added to the existing threads table (schema_pg_core.sql).
-- Idempotent: applied at every start (files run in name order).
ALTER TABLE threads ADD COLUMN IF NOT EXISTS context_links JSONB;

-- The Handler context block last injected into the thread's harness session,
-- "<session_id>:<sha256>" (docs/rebuild/context-reuse.md).
ALTER TABLE threads ADD COLUMN IF NOT EXISTS context_hash TEXT;
