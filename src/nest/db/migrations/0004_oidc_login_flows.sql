-- 0004: Pending OAuth/OIDC sign-ins (B02).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- One row per sign-in started at the auth listener and not yet finished. A row
-- binds the OAuth `state` sent to the identity provider to the browser that
-- started the sign-in, and is deleted when the callback consumes it, so each
-- `state` works at most once. Only SHA-256 digests are stored: the browser's
-- cookie secret, from which the PKCE verifier and the OIDC nonce are derived, never
-- reaches the database.

CREATE TABLE oidc_login_flows (
    -- SHA-256 of the OAuth `state` parameter.
    state_hash    bytea PRIMARY KEY CHECK (length(state_hash) = 32),
    -- SHA-256 of the secret in the browser's sign-in cookie.
    binding_hash  bytea NOT NULL CHECK (length(binding_hash) = 32),
    -- Identity provider, such as 'google'.
    provider      text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_]{0,31}$'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);

CREATE INDEX oidc_login_flows_expires ON oidc_login_flows (expires_at);
