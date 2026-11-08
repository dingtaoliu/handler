-- Records a chat was opened from (docs/rebuild/memory-kinds.md, "Chat context
-- links"), added to the existing threads table (schema_pg_core.sql).
-- Idempotent: applied at every start (files run in name order).
ALTER TABLE threads ADD COLUMN IF NOT EXISTS context_links JSONB;
