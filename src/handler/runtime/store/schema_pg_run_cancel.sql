-- Cancel as a stored flag (role-split-design.md, step 2): the API sets it, the run
-- checks it between steps. Additive: older releases ignore the columns.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS cancel_requested_by TEXT;
