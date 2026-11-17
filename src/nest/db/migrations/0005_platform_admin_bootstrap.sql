-- 0005: one-time platform administrator bootstrap and the platform audit log (B03).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- The bootstrap is a single row that can be written once and never changed. It is
-- written either when the Google account named by NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL
-- first signs in with a verified address, or, whichever comes first, when any
-- platform_admin is granted another way. After that, no setting can make anyone an
-- administrator: only an existing administrator or an operator with database
-- credentials can.

-- Rejects every UPDATE, DELETE or TRUNCATE: the table is append-only.
CREATE FUNCTION nest_forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation',
              CONSTRAINT = TG_TABLE_NAME || '_append_only';
END;
$$;

-- Every change to platform roles and to the bootstrap, whoever made it. Rows are
-- never updated or deleted. User IDs are not foreign keys, so the history outlives
-- the Users it names. No email address is stored here.
CREATE TABLE platform_audit_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    event           text NOT NULL CHECK (event IN (
                        'bootstrap_completed',
                        'bootstrap_refused',
                        'platform_role_granted',
                        'platform_role_revoked')),
    role            text CHECK (role IN ('platform_admin')),
    -- The User the event is about, such as the one granted the role.
    target_user_id  uuid,
    -- The Nest User who made the change, when a signed-in User did.
    actor_user_id   uuid,
    -- The operator name given to `nest-db admin`, when an operator did.
    actor_operator  text CHECK (length(actor_operator) BETWEEN 1 AND 200),
    -- The PostgreSQL role of the session that made the change. Always recorded,
    -- and the authenticated part of an operator's identity.
    database_role   text NOT NULL DEFAULT session_user,
    reason          text CHECK (length(reason) BETWEEN 1 AND 1000),
    details         jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details) = 'object')
);

CREATE INDEX platform_audit_events_target ON platform_audit_events (target_user_id);

CREATE TRIGGER platform_audit_events_append_only BEFORE UPDATE OR DELETE ON platform_audit_events
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER platform_audit_events_no_truncate BEFORE TRUNCATE ON platform_audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- The one-time bootstrap. At most one row (the primary key is the constant true),
-- so concurrent claims serialize on it and exactly one wins.
CREATE TABLE platform_bootstrap (
    singleton     boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    -- 'google_first_login': the configured account claimed it at sign-in.
    -- 'direct_grant': a platform_admin was granted another way first, which
    --   closes the bootstrap for good.
    -- 'preexisting_admin': an administrator already existed when 0005 was applied.
    method        text NOT NULL
                  CHECK (method IN ('google_first_login', 'direct_grant', 'preexisting_admin')),
    -- The first administrator. Not a foreign key: this is history.
    user_id       uuid NOT NULL,
    -- The verified Google identity that claimed it ('google_first_login' only).
    issuer        text,
    subject       text,
    completed_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((method = 'google_first_login') = (issuer IS NOT NULL)),
    CHECK ((issuer IS NULL) = (subject IS NULL))
);

CREATE TRIGGER platform_bootstrap_append_only BEFORE UPDATE OR DELETE ON platform_bootstrap
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER platform_bootstrap_no_truncate BEFORE TRUNCATE ON platform_bootstrap
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- Who is making a change, set by the application for the current transaction with
-- set_config('nest.audit_actor_user_id' | 'nest.audit_operator' | 'nest.audit_reason',
-- value, true). Unset values are recorded as NULL; database_role is always recorded.
CREATE FUNCTION nest_audit_setting(name text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting(name, true), '')
$$;

CREATE FUNCTION nest_audit_platform_role() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Any administrator grant closes the bootstrap. A bootstrap claim has
        -- already written its own row, so this does nothing then.
        INSERT INTO platform_bootstrap (method, user_id)
            VALUES ('direct_grant', NEW.user_id)
            ON CONFLICT (singleton) DO NOTHING;
        INSERT INTO platform_audit_events
            (event, role, target_user_id, actor_user_id, actor_operator, reason)
        VALUES (
            'platform_role_granted', NEW.role, NEW.user_id,
            COALESCE(NEW.granted_by_user_id, nest_audit_setting('nest.audit_actor_user_id')::uuid),
            nest_audit_setting('nest.audit_operator'),
            nest_audit_setting('nest.audit_reason'));
        RETURN NEW;
    END IF;
    INSERT INTO platform_audit_events
        (event, role, target_user_id, actor_user_id, actor_operator, reason)
    VALUES (
        'platform_role_revoked', OLD.role, OLD.user_id,
        nest_audit_setting('nest.audit_actor_user_id')::uuid,
        nest_audit_setting('nest.audit_operator'),
        nest_audit_setting('nest.audit_reason'));
    RETURN OLD;
END;
$$;

CREATE TRIGGER platform_role_assignments_audit AFTER INSERT OR DELETE ON platform_role_assignments
    FOR EACH ROW EXECUTE FUNCTION nest_audit_platform_role();

-- A role assignment is granted or revoked, never rewritten, so every change is
-- one of the audited events above.
CREATE TRIGGER platform_role_assignments_immutable BEFORE UPDATE ON platform_role_assignments
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('user_id', 'role', 'granted_at');

-- The grantor may only be cleared, as its ON DELETE SET NULL foreign key (0003)
-- does when the granting User is deleted. Who granted the role stays in
-- platform_audit_events (actor_user_id), which has no foreign keys.
CREATE FUNCTION nest_platform_role_grantor_only_cleared() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.granted_by_user_id IS NOT NULL
       AND NEW.granted_by_user_id IS DISTINCT FROM OLD.granted_by_user_id THEN
        RAISE EXCEPTION 'platform_role_assignments.granted_by_user_id can only be cleared'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'platform_role_assignments_granted_by_user_id_immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER platform_role_assignments_grantor BEFORE UPDATE ON platform_role_assignments
    FOR EACH ROW EXECUTE FUNCTION nest_platform_role_grantor_only_cleared();
CREATE TRIGGER platform_role_assignments_no_truncate BEFORE TRUNCATE ON platform_role_assignments
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

CREATE FUNCTION nest_audit_bootstrap() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO platform_audit_events
        (event, role, target_user_id, actor_user_id, actor_operator, reason, details)
    VALUES (
        'bootstrap_completed', 'platform_admin', NEW.user_id,
        nest_audit_setting('nest.audit_actor_user_id')::uuid,
        nest_audit_setting('nest.audit_operator'),
        nest_audit_setting('nest.audit_reason'),
        jsonb_strip_nulls(jsonb_build_object(
            'method', NEW.method, 'issuer', NEW.issuer, 'subject', NEW.subject)));
    RETURN NULL;
END;
$$;

CREATE TRIGGER platform_bootstrap_audit AFTER INSERT ON platform_bootstrap
    FOR EACH ROW EXECUTE FUNCTION nest_audit_bootstrap();

-- A deployment that already has an administrator is already bootstrapped.
INSERT INTO platform_bootstrap (method, user_id, completed_at)
SELECT 'preexisting_admin', user_id, granted_at
FROM platform_role_assignments
WHERE role = 'platform_admin'
ORDER BY granted_at, user_id
LIMIT 1;
