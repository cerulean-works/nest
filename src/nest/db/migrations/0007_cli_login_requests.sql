-- 0007: browser-approved CLI sign-in (B05).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- A CLI sign-in follows the OAuth 2.0 device authorization grant (RFC 8628): the
-- CLI starts a request and shows a short user code; the User opens Nest in a
-- browser where they are signed in with Google, checks the code and approves; the
-- CLI, polling with its device code, then receives a CLI session. Only SHA-256
-- digests of the device code and the user code are stored. A request is approved
-- at most once and turned into a session at most once.

CREATE TABLE cli_login_requests (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- SHA-256 of the secret device code the CLI polls with.
    device_code_hash        bytea NOT NULL UNIQUE CHECK (length(device_code_hash) = 32),
    -- SHA-256 of the normalized user code shown to the User.
    user_code_hash          bytea NOT NULL UNIQUE CHECK (length(user_code_hash) = 32),
    -- Space-separated scopes the CLI asked for, granted as asked on approval.
    scope                   text NOT NULL
        CHECK (scope ~ '^[a-z][a-z0-9_.:]{0,63}( [a-z][a-z0-9_.:]{0,63}){0,15}$'),
    -- Label the CLI reported, such as its host name. Shown to the approving User;
    -- unauthenticated, so never trusted.
    client_name             text NOT NULL CHECK (length(client_name) BETWEEN 1 AND 64),
    status                  text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'consumed')),
    -- The User who approved or denied it.
    user_id                 uuid REFERENCES users (id) ON DELETE CASCADE,
    -- The browser session the decision was made in.
    decided_by_session_id   uuid REFERENCES user_sessions (id) ON DELETE SET NULL,
    -- The CLI session issued for it.
    session_id              uuid REFERENCES user_sessions (id) ON DELETE SET NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    expires_at              timestamptz NOT NULL,
    decided_at              timestamptz,
    last_polled_at          timestamptz,
    CHECK (expires_at > created_at),
    CHECK ((status = 'pending') = (decided_at IS NULL)),
    CHECK (status = 'pending' OR user_id IS NOT NULL)
);

CREATE INDEX cli_login_requests_expires ON cli_login_requests (expires_at);

CREATE TRIGGER cli_login_requests_immutable BEFORE UPDATE ON cli_login_requests
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'device_code_hash', 'user_code_hash', 'scope', 'client_name', 'created_at',
        'expires_at');

-- A decided request is never reopened, and is consumed only once after approval.
CREATE FUNCTION nest_cli_login_request_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'pending' AND NEW.status IN ('approved', 'denied'))
        OR (OLD.status = 'approved' AND NEW.status = 'consumed')
    ) THEN
        RAISE EXCEPTION 'A CLI sign-in cannot go from % to %', OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'cli_login_requests_transition';
    END IF;
    IF OLD.status <> 'pending' AND (
        NEW.user_id IS DISTINCT FROM OLD.user_id
        OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
    ) THEN
        RAISE EXCEPTION 'A decided CLI sign-in cannot change its decision'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'cli_login_requests_transition';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER cli_login_requests_transition BEFORE UPDATE ON cli_login_requests
    FOR EACH ROW EXECUTE FUNCTION nest_cli_login_request_transition();

-- What a CLI session may do. Browser sessions have none: they are not scoped
-- tokens and are accepted only by the browser routes.
ALTER TABLE user_sessions
    ADD COLUMN scope text
        CHECK (scope ~ '^[a-z][a-z0-9_.:]{0,63}( [a-z][a-z0-9_.:]{0,63}){0,15}$'),
    ADD CONSTRAINT user_sessions_scope_only_cli CHECK (kind = 'cli' OR scope IS NULL);

CREATE TRIGGER user_sessions_scope_immutable BEFORE UPDATE ON user_sessions
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('scope');
