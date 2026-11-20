-- 0009: structured, tamper-evident security audit log (B07).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- One append-only table records security-relevant events from every interface:
-- authorization decisions on the gRPC execution service and the Projects API
-- (written by the Controller, see nest.audit), and identity, session, CLI
-- sign-in, invitation, Invite Link and Provider Profile changes (written by the
-- triggers below, whoever made the change). It complements, and does not
-- replace, platform_audit_events (0005) and project_audit_events (0008).
--
-- Nothing secret is stored: no token, token hash, device or user code, email
-- address, message body, SMTP setting, SSH key or HPC credential. IDs are not
-- foreign keys, so the history outlives the rows it names.
--
-- Rows are sealed into a SHA-256 hash chain by nest_seal_security_audit(): each
-- sealed row's row_hash covers its content and the previous row's hash, so a
-- later change, deletion or reordering of a sealed row is detected by
-- `nest-db audit verify`. Sealing runs in its own short transaction, so business
-- transactions never wait on the chain.

CREATE TABLE security_audit_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- Dotted lowercase name, such as 'run.submit' or 'session.revoked'. Policy
    -- decisions use the authz.Action value as the event name.
    event           text NOT NULL
                    CHECK (event ~ '^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}$'),
    outcome         text NOT NULL CHECK (outcome IN ('succeeded', 'denied', 'failed')),
    -- Where the event came from: 'grpc' and 'http' for requests, 'controller'
    -- for the Controller itself, 'database' for changes recorded by triggers.
    interface       text NOT NULL
                    CHECK (interface IN ('grpc', 'http', 'cli', 'controller', 'database')),
    -- The Nest User who acted, and the session they acted through.
    actor_user_id   uuid,
    session_id      uuid,
    -- The operator name, when an operator with database credentials acted.
    actor_operator  text CHECK (length(actor_operator) BETWEEN 1 AND 200),
    database_role   text NOT NULL DEFAULT session_user,
    project_id      uuid,
    -- The User the event is about, such as the one whose session was revoked.
    target_user_id  uuid,
    -- What was acted on, such as ('job', <execution ID>) or ('invitation', '42').
    resource_type   text CHECK (resource_type ~ '^[a-z][a-z0-9_]{0,31}$'),
    resource_id     text CHECK (resource_id ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    -- Correlation ID from the Controller log.
    request_id      text CHECK (request_id ~ '^[0-9a-f]{32}$'),
    -- Machine-readable reason, such as the policy's 'not_a_member'.
    reason          text CHECK (reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    -- Free-text reason an operator gave (nest.audit_reason).
    note            text CHECK (length(note) BETWEEN 1 AND 1000),
    -- Redacted scalar details, such as {"rpc": "Execute", "gpu": 1}.
    details         jsonb NOT NULL DEFAULT '{}'
                    CHECK (jsonb_typeof(details) = 'object' AND length(details::text) <= 4000),
    -- Hash chain, set once by nest_seal_security_audit().
    chain_seq       bigint UNIQUE,
    prev_hash       bytea CHECK (length(prev_hash) = 32),
    row_hash        bytea CHECK (length(row_hash) = 32),
    CHECK ((chain_seq IS NULL) = (row_hash IS NULL)),
    CHECK (prev_hash IS NULL OR chain_seq IS NOT NULL)
);

CREATE INDEX security_audit_events_occurred ON security_audit_events (occurred_at);
CREATE INDEX security_audit_events_actor ON security_audit_events (actor_user_id, id);
CREATE INDEX security_audit_events_project ON security_audit_events (project_id, id);
CREATE INDEX security_audit_events_unsealed ON security_audit_events (id) WHERE chain_seq IS NULL;

-- The hash of one row: SHA-256 over a JSON array of every column except row_hash.
-- jsonb's text form is deterministic, and occurred_at is hashed as epoch
-- microseconds so the session's TimeZone cannot change it.
CREATE FUNCTION nest_security_audit_hash(e security_audit_events) RETURNS bytea
LANGUAGE sql STABLE AS $$
    SELECT sha256(convert_to(jsonb_build_array(
        e.chain_seq, encode(e.prev_hash, 'hex'), e.id,
        (extract(epoch FROM e.occurred_at) * 1000000)::bigint,
        e.event, e.outcome, e.interface, e.actor_user_id, e.session_id, e.actor_operator,
        e.database_role, e.project_id, e.target_user_id, e.resource_type, e.resource_id,
        e.request_id, e.reason, e.note, e.details)::text, 'UTF8'))
$$;

-- Rows are never deleted, and the only permitted update is sealing an unsealed
-- row, which sets the three chain columns and nothing else.
CREATE FUNCTION nest_security_audit_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.chain_seq IS NULL AND NEW.chain_seq IS NOT NULL
       AND (to_jsonb(NEW) - 'chain_seq' - 'prev_hash' - 'row_hash')
           = (to_jsonb(OLD) - 'chain_seq' - 'prev_hash' - 'row_hash') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'security_audit_events is append-only'
        USING ERRCODE = 'integrity_constraint_violation',
              CONSTRAINT = 'security_audit_events_append_only';
END;
$$;

CREATE TRIGGER security_audit_events_append_only BEFORE UPDATE OR DELETE ON security_audit_events
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_guard();
CREATE TRIGGER security_audit_events_no_truncate BEFORE TRUNCATE ON security_audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- Seals every committed, unsealed row, in ID order, onto the end of the chain.
-- Concurrent sealers serialize on one transaction-level advisory lock. Returns
-- how many rows it sealed.
CREATE FUNCTION nest_seal_security_audit() RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    last_seq bigint;
    last_hash bytea;
    r security_audit_events;
    sealed integer := 0;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('nest.security_audit_events', 0));
    SELECT chain_seq, row_hash INTO last_seq, last_hash FROM security_audit_events
        WHERE chain_seq IS NOT NULL ORDER BY chain_seq DESC LIMIT 1;
    FOR r IN SELECT * FROM security_audit_events WHERE chain_seq IS NULL ORDER BY id LOOP
        r.chain_seq := COALESCE(last_seq, 0) + 1;
        r.prev_hash := last_hash;
        r.row_hash := nest_security_audit_hash(r);
        UPDATE security_audit_events
            SET chain_seq = r.chain_seq, prev_hash = r.prev_hash, row_hash = r.row_hash
            WHERE id = r.id;
        last_seq := r.chain_seq;
        last_hash := r.row_hash;
        sealed := sealed + 1;
    END LOOP;
    RETURN sealed;
END;
$$;

-- Records one event from a trigger, with the actor named by the transaction's
-- audit settings (0005) unless the trigger knows it.
CREATE FUNCTION nest_security_audit(
    p_event text,
    p_actor uuid DEFAULT NULL,
    p_target uuid DEFAULT NULL,
    p_project uuid DEFAULT NULL,
    p_resource_type text DEFAULT NULL,
    p_resource_id text DEFAULT NULL,
    p_reason text DEFAULT NULL,
    p_details jsonb DEFAULT '{}',
    p_session uuid DEFAULT NULL
) RETURNS void
LANGUAGE sql AS $$
    INSERT INTO security_audit_events
        (event, outcome, interface, actor_user_id, session_id, actor_operator, project_id,
         target_user_id, resource_type, resource_id, reason, note, details)
    VALUES (
        p_event, 'succeeded', 'database',
        COALESCE(nest_audit_setting('nest.audit_actor_user_id')::uuid, p_actor),
        p_session,
        nest_audit_setting('nest.audit_operator'),
        p_project, p_target, p_resource_type, p_resource_id, p_reason,
        nest_audit_setting('nest.audit_reason'),
        jsonb_strip_nulls(p_details))
$$;

-- Users: enabling and disabling.
CREATE FUNCTION nest_security_audit_user() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM nest_security_audit(
            CASE WHEN NEW.status = 'disabled' THEN 'user.disabled' ELSE 'user.enabled' END,
            p_target => NEW.id, p_resource_type => 'user', p_resource_id => NEW.id::text);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER users_security_audit AFTER UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_user();

-- External identities linked to a User. The issuer only; never the email address.
CREATE FUNCTION nest_security_audit_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM nest_security_audit(
        'identity.linked', p_actor => NEW.user_id, p_target => NEW.user_id,
        p_resource_type => 'identity', p_resource_id => NEW.id::text,
        p_details => jsonb_build_object('issuer', left(NEW.issuer, 200)));
    RETURN NULL;
END;
$$;

CREATE TRIGGER user_identities_security_audit AFTER INSERT ON user_identities
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_identity();

-- Sessions: a new sign-in (the first session of a family) and every revocation
-- other than routine rotation. Refreshes are not recorded.
CREATE FUNCTION nest_security_audit_session() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.id = NEW.family_id THEN
            PERFORM nest_security_audit(
                'session.created', p_actor => NEW.user_id, p_target => NEW.user_id,
                p_resource_type => 'session', p_resource_id => NEW.id::text,
                p_details => jsonb_build_object('kind', NEW.kind, 'scope', NEW.scope),
                p_session => NEW.id);
        END IF;
    ELSIF NEW.revoked_at IS NOT NULL AND OLD.revoked_at IS NULL
          AND NEW.revoked_reason IS DISTINCT FROM 'rotated' THEN
        PERFORM nest_security_audit(
            'session.revoked', p_target => NEW.user_id,
            p_resource_type => 'session', p_resource_id => NEW.id::text,
            p_reason => NEW.revoked_reason,
            p_details => jsonb_build_object('kind', NEW.kind, 'family_id', NEW.family_id),
            p_session => NEW.id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER user_sessions_security_audit AFTER INSERT OR UPDATE ON user_sessions
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_session();

-- CLI sign-in decisions. Neither code nor the CLI-reported client name is
-- recorded: the name is unauthenticated text.
CREATE FUNCTION nest_security_audit_cli_login() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NEW.status IN ('approved', 'denied') THEN
        PERFORM nest_security_audit(
            'cli_login.' || NEW.status, p_actor => NEW.user_id, p_target => NEW.user_id,
            p_resource_type => 'cli_login', p_resource_id => NEW.id::text,
            p_details => jsonb_build_object('scope', NEW.scope),
            p_session => NEW.decided_by_session_id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER cli_login_requests_security_audit AFTER UPDATE ON cli_login_requests
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_cli_login();

-- Email invitations: creation, a reissued link token, and every completion. The
-- invited address and the token hash are never recorded.
CREATE FUNCTION nest_security_audit_invitation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM nest_security_audit(
            'invitation.created', p_actor => NEW.invited_by_user_id, p_project => NEW.project_id,
            p_resource_type => 'invitation', p_resource_id => NEW.id::text,
            p_details => jsonb_build_object('role', NEW.role));
        RETURN NULL;
    END IF;
    IF NEW.token_hash IS DISTINCT FROM OLD.token_hash AND NEW.token_hash IS NOT NULL THEN
        PERFORM nest_security_audit(
            'invitation.token_issued', p_project => NEW.project_id,
            p_resource_type => 'invitation', p_resource_id => NEW.id::text);
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM nest_security_audit(
            'invitation.' || NEW.status,
            p_actor => CASE NEW.status WHEN 'accepted' THEN NEW.accepted_by_user_id
                                       WHEN 'revoked' THEN NEW.revoked_by_user_id END,
            p_target => NEW.accepted_by_user_id, p_project => NEW.project_id,
            p_resource_type => 'invitation', p_resource_id => NEW.id::text,
            p_details => jsonb_build_object('role', NEW.role));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER project_email_invitations_security_audit
    AFTER INSERT OR UPDATE ON project_email_invitations
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_invitation();

-- Invitation emails queued, including every resend. Delivery state, the
-- recipient and the body stay in email_outbox.
CREATE FUNCTION nest_security_audit_invitation_email() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    invitation bigint;
    project uuid;
BEGIN
    IF NEW.subject_ref ~ '^invitation:[0-9]{1,18}$' THEN
        invitation := split_part(NEW.subject_ref, ':', 2)::bigint;
        SELECT project_id INTO project FROM project_email_invitations WHERE id = invitation;
        PERFORM nest_security_audit(
            CASE WHEN TG_OP = 'INSERT' THEN 'invitation.email_queued'
                 ELSE 'invitation.email_requeued' END,
            p_project => project, p_resource_type => 'invitation',
            p_resource_id => invitation::text,
            p_details => jsonb_build_object('message_key', NEW.message_key, 'kind', NEW.kind));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER email_outbox_invitation_security_audit AFTER INSERT ON email_outbox
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_invitation_email();
-- A failed message queued again explicitly is a resend.
CREATE TRIGGER email_outbox_invitation_requeue_security_audit AFTER UPDATE ON email_outbox
    FOR EACH ROW WHEN (OLD.status = 'failed' AND NEW.status = 'pending')
    EXECUTE FUNCTION nest_security_audit_invitation_email();

-- Invite Links: creation, revocation and each redemption.
CREATE FUNCTION nest_security_audit_invite_link() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM nest_security_audit(
            'invite_link.created', p_actor => NEW.created_by_user_id, p_project => NEW.project_id,
            p_resource_type => 'invite_link', p_resource_id => NEW.id::text,
            p_details => jsonb_build_object('role', NEW.role, 'max_uses', NEW.max_uses));
    ELSIF NEW.revoked_at IS NOT NULL AND OLD.revoked_at IS NULL THEN
        PERFORM nest_security_audit(
            'invite_link.revoked', p_actor => NEW.revoked_by_user_id, p_project => NEW.project_id,
            p_resource_type => 'invite_link', p_resource_id => NEW.id::text);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER project_invite_links_security_audit AFTER INSERT OR UPDATE ON project_invite_links
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_invite_link();

CREATE FUNCTION nest_security_audit_redemption() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    link project_invite_links;
BEGIN
    SELECT * INTO link FROM project_invite_links WHERE id = NEW.invite_link_id;
    PERFORM nest_security_audit(
        'invite_link.redeemed', p_actor => NEW.user_id, p_target => NEW.user_id,
        p_project => link.project_id, p_resource_type => 'invite_link',
        p_resource_id => NEW.invite_link_id::text,
        p_details => jsonb_build_object('role', link.role));
    RETURN NULL;
END;
$$;

CREATE TRIGGER project_invitation_redemptions_security_audit
    AFTER INSERT ON project_invitation_redemptions
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_redemption();

-- Provider Profiles: the HPC access configuration. Only the names of changed
-- columns are recorded; the table holds no credentials. is_default is left out:
-- the Controller clears and sets it at every start, and records the default
-- Provider in its 'controller.started' event.
CREATE FUNCTION nest_security_audit_provider() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    changed text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        changed := 'created';
    ELSE
        SELECT string_agg(key, ',' ORDER BY key) INTO changed
        FROM jsonb_each(to_jsonb(NEW)) n
        WHERE key NOT IN ('updated_at', 'created_at', 'is_default')
          AND n.value IS DISTINCT FROM to_jsonb(OLD) -> key;
    END IF;
    IF changed IS NOT NULL THEN
        PERFORM nest_security_audit(
            'provider.profile_changed', p_resource_type => 'provider', p_resource_id => NEW.id,
            p_details => jsonb_build_object('changed', changed, 'ssh_mode', NEW.ssh_mode,
                                            'active', NEW.active));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER provider_profiles_security_audit AFTER INSERT OR UPDATE ON provider_profiles
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_provider();
