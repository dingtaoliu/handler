-- Memory topic metadata (docs/rebuild/memory-kinds.md), added to the existing
-- memory_topics table (schema_pg_domains.sql). Idempotent: applied at every
-- start, after schema_pg_domains.sql (files run in name order).
ALTER TABLE memory_topics ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE memory_topics ADD COLUMN IF NOT EXISTS links JSONB;
ALTER TABLE memory_topics ADD COLUMN IF NOT EXISTS source_untrusted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE memory_topics ADD COLUMN IF NOT EXISTS updated_thread_key TEXT;
CREATE INDEX IF NOT EXISTS memory_topics_updated ON memory_topics(updated_at);
CREATE INDEX IF NOT EXISTS memory_topics_kind ON memory_topics(kind, updated_at);
CREATE INDEX IF NOT EXISTS memory_topics_links ON memory_topics USING GIN (links jsonb_path_ops);
