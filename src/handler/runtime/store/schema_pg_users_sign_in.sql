-- One-click sign-in (e.g. from Crontal) and expiring session tokens.
-- Tickets are single use, short lived and stored only as SHA-256 hashes.
CREATE TABLE IF NOT EXISTS sign_in_tickets (
    ticket_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sign_in_tickets_expiry ON sign_in_tickets(expires_at);

-- Optional expiry for a user token; tokens without a row never expire.
CREATE TABLE IF NOT EXISTS user_token_expiry (
    token_id TEXT PRIMARY KEY REFERENCES user_tokens(id),
    expires_at TIMESTAMPTZ NOT NULL
);
