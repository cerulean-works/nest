-- 0006: rotating refresh sessions and short-lived access tokens (B04).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- A user_sessions row is now one refresh session. Each refresh replaces the row
-- with a new one in the same family and a new secret, and marks the old row
-- revoked with reason 'rotated'. Presenting a rotated secret again is treated as
-- theft and revokes the whole family. A family never outlives family_expires_at,
-- however often it is refreshed.
--
-- Access tokens are short-lived secrets tied to one refresh session. Only their
-- SHA-256 is stored. An access token is usable only while its own expiry has not
-- passed and its session is unrevoked, unexpired and belongs to an active User, so
-- logout and revocation end it immediately.

ALTER TABLE user_sessions
    ADD COLUMN family_id uuid,
    ADD COLUMN family_expires_at timestamptz,
    -- The session that replaced this one when it was rotated.
    ADD COLUMN replaced_by_session_id uuid REFERENCES user_sessions (id) ON DELETE SET NULL;

-- Each existing session starts its own family, with its current expiry as the cap.
UPDATE user_sessions SET family_id = id, family_expires_at = expires_at;

ALTER TABLE user_sessions
    ALTER COLUMN family_id SET NOT NULL,
    ALTER COLUMN family_expires_at SET NOT NULL,
    ADD CONSTRAINT user_sessions_within_family CHECK (expires_at <= family_expires_at),
    ADD CONSTRAINT user_sessions_replaced_when_rotated
        CHECK (replaced_by_session_id IS NULL OR revoked_reason = 'rotated');

CREATE INDEX user_sessions_family ON user_sessions (family_id);

-- An INSERT without a family starts a new one, capped at the session's own expiry.
CREATE FUNCTION nest_session_family_defaults() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.family_id := COALESCE(NEW.family_id, NEW.id);
    NEW.family_expires_at := COALESCE(NEW.family_expires_at, NEW.expires_at);
    RETURN NEW;
END;
$$;

CREATE TRIGGER user_sessions_family_defaults BEFORE INSERT ON user_sessions
    FOR EACH ROW EXECUTE FUNCTION nest_session_family_defaults();

CREATE TRIGGER user_sessions_immutable BEFORE UPDATE ON user_sessions
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'user_id', 'kind', 'token_hash', 'family_id');

-- A revoked session stays revoked: its revocation time and reason never change.
CREATE FUNCTION nest_session_stays_revoked() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.revoked_at IS NOT NULL AND (
        NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
        OR NEW.revoked_reason IS DISTINCT FROM OLD.revoked_reason
    ) THEN
        RAISE EXCEPTION 'A revoked session cannot be restored or changed'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'user_sessions_stay_revoked';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER user_sessions_stay_revoked BEFORE UPDATE ON user_sessions
    FOR EACH ROW EXECUTE FUNCTION nest_session_stays_revoked();

CREATE TABLE session_access_tokens (
    -- SHA-256 of the access token.
    token_hash  bytea PRIMARY KEY CHECK (length(token_hash) = 32),
    session_id  uuid NOT NULL REFERENCES user_sessions (id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);

CREATE INDEX session_access_tokens_session ON session_access_tokens (session_id);
CREATE INDEX session_access_tokens_expires ON session_access_tokens (expires_at);
