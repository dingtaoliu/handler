-- Parked threads (docs/rebuild/role-split-design.md, "Durability"): `dead` is the
-- parked state. park_noticed is set once someone has been told that new work is
-- waiting on the parked thread, so a burst of events sends one notice; it is
-- cleared each time the thread parks. Idempotent: applied at every start.
ALTER TABLE threads ADD COLUMN IF NOT EXISTS park_noticed BOOLEAN NOT NULL DEFAULT FALSE;
