-- 0019: Server-enforced execution authority and verified HPC account bindings
-- (D01, D02).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- Execution authority says whose HPC identity runs a job's Slurm commands:
--   'controller_owned' (Mode A, Controller SSH): the operator-configured identity
--       of the Controller. Trusted deployments only.
--   'user_owned' (Mode B, User SSH Proxy): the submitting User's own HPC account,
--       through a session the User owns. It never falls back to the Controller's
--       credentials.
-- It is a separate dimension from the login-node transport modes of 0012 ('A'
-- and 'B'), whose meaning is unchanged: transport 'B' is still the Controller's
-- own managed-key SSH and is not reinterpreted as a User's session.
--
-- Which authorities a job may use is resolved on the server from three layers,
-- and a job may use only one all three permit: the Controller's deployment
-- setting (NEST_EXECUTION_AUTHORITIES, not stored here), the Platform's policy,
-- and the Project's policy on that Platform. Policies are operator managed and
-- versioned: a change appends the next version and never edits an earlier one. A
-- Platform or Project without a policy row has the implicit version 0, which
-- permits only 'controller_owned', so existing deployments keep Mode A and
-- 'user_owned' needs an explicit operator opt-in on both layers.
--
-- An HPC account binding maps one Nest User to one UNIX account on one Platform,
-- keyed by the immutable platform_id and never by a login node, so adding,
-- removing or failing over login nodes changes no binding. A User's claim
-- ('claimed') records only a username: it is not evidence of ownership and never
-- authorizes a job. Only an operator can verify a binding, by recording an
-- independent attestation (such as an NCHC or operator confirmation) as a
-- reference. Verifying, replacing and revoking are audited, and each binding
-- version is a separate row that is never deleted. Nothing here stores a
-- password, OTP, MFA secret, SSH key or SSH session: the tables hold none.
--
-- Every attempt recorded from now on pins its execution authority and the
-- Platform and Project policy versions it was admitted under; a user_owned
-- attempt also pins its verified binding and version (hpc_binding_id and
-- hpc_binding_version of 0014). Attempts recorded before this migration keep
-- NULL: they were admitted before authority was recorded, and are not
-- reinterpreted.

CREATE FUNCTION nest_check_authorities(authorities text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT authorities <@ ARRAY['controller_owned', 'user_owned']::text[]
       AND array_position(authorities, NULL) IS NULL
       AND cardinality(authorities) = cardinality(ARRAY(SELECT DISTINCT unnest(authorities)))
$$;

-- Platform policies ------------------------------------------------------------

CREATE TABLE hpc_platform_authority_policies (
    platform_id  uuid NOT NULL REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    -- 1, 2, ...: assigned on insert. The highest version is in force.
    version      integer NOT NULL CHECK (version >= 1),
    -- Permitted authorities; empty permits none.
    authorities  text[] NOT NULL CHECK (nest_check_authorities(authorities)),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform_id, version)
);

-- Project policies on one Platform -----------------------------------------------

-- Not tied to the grant row: a revoked grant keeps its policy history, and the
-- grant is checked separately whenever a job is admitted.
CREATE TABLE project_platform_authority_policies (
    project_id   uuid NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
    platform_id  uuid NOT NULL REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    version      integer NOT NULL CHECK (version >= 1),
    authorities  text[] NOT NULL CHECK (nest_check_authorities(authorities)),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, platform_id, version)
);

CREATE INDEX project_platform_authority_policies_platform
    ON project_platform_authority_policies (platform_id);

-- Assigns the next version under the Platform's authority lock. Admission takes
-- the same lock in shared mode, so a policy change either commits before an
-- attempt is recorded (and the attempt sees it) or waits for the attempt.
CREATE FUNCTION nest_authority_policy_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('nest.hpc_authority:' || NEW.platform_id, 0));
    IF TG_TABLE_NAME = 'hpc_platform_authority_policies' THEN
        SELECT COALESCE(max(version), 0) + 1 INTO NEW.version
        FROM hpc_platform_authority_policies WHERE platform_id = NEW.platform_id;
    ELSE
        SELECT COALESCE(max(version), 0) + 1 INTO NEW.version
        FROM project_platform_authority_policies
        WHERE project_id = NEW.project_id AND platform_id = NEW.platform_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER hpc_platform_authority_policies_operator_managed
    BEFORE INSERT ON hpc_platform_authority_policies
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();
CREATE TRIGGER hpc_platform_authority_policies_version
    BEFORE INSERT ON hpc_platform_authority_policies
    FOR EACH ROW EXECUTE FUNCTION nest_authority_policy_version();
CREATE TRIGGER hpc_platform_authority_policies_append_only
    BEFORE UPDATE OR DELETE ON hpc_platform_authority_policies
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER hpc_platform_authority_policies_no_truncate
    BEFORE TRUNCATE ON hpc_platform_authority_policies
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

CREATE TRIGGER project_platform_authority_policies_operator_managed
    BEFORE INSERT ON project_platform_authority_policies
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();
CREATE TRIGGER project_platform_authority_policies_version
    BEFORE INSERT ON project_platform_authority_policies
    FOR EACH ROW EXECUTE FUNCTION nest_authority_policy_version();
CREATE TRIGGER project_platform_authority_policies_append_only
    BEFORE UPDATE OR DELETE ON project_platform_authority_policies
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER project_platform_authority_policies_no_truncate
    BEFORE TRUNCATE ON project_platform_authority_policies
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

CREATE FUNCTION nest_security_audit_authority_policy() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'hpc_platform_authority_policies' THEN
        PERFORM nest_security_audit(
            'platform.authority_policy.set', p_resource_type => 'platform',
            p_resource_id => NEW.platform_id::text,
            p_details => jsonb_build_object(
                'version', NEW.version,
                'authorities', array_to_string(NEW.authorities, ',')));
    ELSE
        PERFORM nest_security_audit(
            'project.authority_policy.set', p_project => NEW.project_id,
            p_resource_type => 'platform', p_resource_id => NEW.platform_id::text,
            p_details => jsonb_build_object(
                'version', NEW.version,
                'authorities', array_to_string(NEW.authorities, ',')));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER hpc_platform_authority_policies_security_audit
    AFTER INSERT ON hpc_platform_authority_policies
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_authority_policy();
CREATE TRIGGER project_platform_authority_policies_security_audit
    AFTER INSERT ON project_platform_authority_policies
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_authority_policy();

-- The authorities in force for a Project on a Platform, with both versions.
-- Without a policy row a layer is version 0 and permits only 'controller_owned'.
CREATE FUNCTION nest_effective_authority_policy(p_project uuid, p_platform uuid)
RETURNS TABLE (authorities text[], platform_version integer, project_version integer)
LANGUAGE sql STABLE AS $$
    WITH platform_policy AS (
        SELECT COALESCE(
                   (SELECT authorities FROM hpc_platform_authority_policies
                    WHERE platform_id = p_platform ORDER BY version DESC LIMIT 1),
                   ARRAY['controller_owned']::text[]) AS authorities,
               COALESCE(
                   (SELECT max(version) FROM hpc_platform_authority_policies
                    WHERE platform_id = p_platform), 0) AS version
    ), project_policy AS (
        SELECT COALESCE(
                   (SELECT authorities FROM project_platform_authority_policies
                    WHERE project_id = p_project AND platform_id = p_platform
                    ORDER BY version DESC LIMIT 1),
                   ARRAY['controller_owned']::text[]) AS authorities,
               COALESCE(
                   (SELECT max(version) FROM project_platform_authority_policies
                    WHERE project_id = p_project AND platform_id = p_platform), 0) AS version
    )
    SELECT ARRAY(
               SELECT a FROM unnest(pl.authorities) a
               WHERE a = ANY (pr.authorities) ORDER BY a),
           pl.version, pr.version
    FROM platform_policy pl, project_policy pr
$$;

-- HPC account bindings ---------------------------------------------------------

CREATE TABLE hpc_account_bindings (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                   uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    -- The immutable Platform, never a login node or host name.
    platform_id               uuid NOT NULL REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    -- 1, 2, ... per (user_id, platform_id): assigned on insert. A replacement is
    -- a new version; the earlier row is kept as 'replaced'.
    version                   integer NOT NULL CHECK (version >= 1),
    -- The UNIX user name on the Platform: a name, never a credential.
    unix_user                 text NOT NULL CHECK (unix_user ~ '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$'),
    -- The UNIX user ID, when an attestation states it.
    unix_uid                  integer CHECK (unix_uid >= 0),
    -- 'user_claim': the User typed the name (onboarding); not evidence.
    -- 'operator': an operator recorded the binding.
    claim_source              text NOT NULL CHECK (claim_source IN ('user_claim', 'operator')),
    -- 'claimed': unverified, never authorizes a job. 'verified': an operator
    -- recorded an independent attestation. 'replaced', 'revoked': final.
    status                    text NOT NULL DEFAULT 'claimed'
                              CHECK (status IN ('claimed', 'verified', 'replaced', 'revoked')),
    -- How ownership was attested, by whom, and a reference to the evidence (such
    -- as a ticket or attestation ID). The reference cannot hold whitespace, so no
    -- key, password or multi-line document fits in it.
    verification_method       text CHECK (verification_method IN (
                                  'operator_attestation', 'nchc_attestation')),
    verification_evidence_ref text CHECK (verification_evidence_ref ~ '^[A-Za-z0-9_.:/#@+=-]{1,200}$'),
    verified_by               text CHECK (length(verified_by) BETWEEN 1 AND 200),
    verified_at               timestamptz,
    -- Checked at commit, so a replacement can mark the old version replaced
    -- before it inserts the new one (see hpc_account_bindings_replacement below).
    replaced_by_binding_id    uuid REFERENCES hpc_account_bindings (id)
                              DEFERRABLE INITIALLY DEFERRED,
    revoked_reason            text CHECK (revoked_reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    ended_at                  timestamptz,
    CONSTRAINT hpc_account_bindings_version_unique UNIQUE (user_id, platform_id, version),
    CONSTRAINT hpc_account_bindings_verification_complete CHECK (
        (verified_at IS NULL) = (verification_method IS NULL)
        AND (verified_at IS NULL) = (verification_evidence_ref IS NULL)
        AND (verified_at IS NULL) = (verified_by IS NULL)),
    CONSTRAINT hpc_account_bindings_verified_has_evidence
        CHECK (status <> 'verified' OR verified_at IS NOT NULL),
    CONSTRAINT hpc_account_bindings_claim_unverified
        CHECK (status <> 'claimed' OR verified_at IS NULL),
    CHECK ((status IN ('replaced', 'revoked')) = (ended_at IS NOT NULL)),
    CHECK ((status = 'replaced') = (replaced_by_binding_id IS NOT NULL)),
    CHECK ((status = 'revoked') = (revoked_reason IS NOT NULL))
);

-- At most one current binding per User and Platform.
CREATE UNIQUE INDEX hpc_account_bindings_one_current ON hpc_account_bindings (user_id, platform_id)
    WHERE status IN ('claimed', 'verified');
-- A UNIX account on a Platform is verified for at most one Nest User: a shared
-- UNIX account would not isolate its Users from each other.
CREATE UNIQUE INDEX hpc_account_bindings_one_owner ON hpc_account_bindings (platform_id, unix_user)
    WHERE status = 'verified';
-- File and process permissions follow the numeric UID, not the name: a known UID
-- is verified for at most one Nest User per Platform too, whatever it is called.
-- A binding without an attested UID relies on the operator's attestation alone.
CREATE UNIQUE INDEX hpc_account_bindings_one_owner_uid ON hpc_account_bindings (platform_id, unix_uid)
    WHERE status = 'verified' AND unix_uid IS NOT NULL;
CREATE INDEX hpc_account_bindings_platform ON hpc_account_bindings (platform_id, status);

CREATE FUNCTION nest_hpc_binding_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.claim_source = 'operator' OR NEW.status <> 'claimed')
       AND nest_audit_setting('nest.audit_operator') IS NULL THEN
        RAISE EXCEPTION 'only an operator can record or verify an HPC account binding'
            USING ERRCODE = 'insufficient_privilege',
                  CONSTRAINT = 'hpc_account_bindings_operator_managed';
    END IF;
    IF NEW.status NOT IN ('claimed', 'verified') THEN
        RAISE EXCEPTION 'a new HPC account binding is claimed or verified'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'hpc_account_bindings_status_transition';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('nest.hpc_binding:' || NEW.user_id || ':' || NEW.platform_id, 0));
    SELECT COALESCE(max(version), 0) + 1 INTO NEW.version FROM hpc_account_bindings
    WHERE user_id = NEW.user_id AND platform_id = NEW.platform_id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER hpc_account_bindings_insert BEFORE INSERT ON hpc_account_bindings
    FOR EACH ROW EXECUTE FUNCTION nest_hpc_binding_insert();
CREATE TRIGGER hpc_account_bindings_immutable BEFORE UPDATE ON hpc_account_bindings
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'id', 'user_id', 'platform_id', 'version', 'unix_user', 'claim_source', 'created_at');

-- Allowed changes. A binding's verification is never edited once recorded, a
-- final binding never changes, and changing a verified binding, or verifying
-- one, needs an operator. The User may only withdraw their own claim.
CREATE FUNCTION nest_hpc_binding_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('replaced', 'revoked') THEN
        RAISE EXCEPTION 'a % HPC account binding never changes', OLD.status
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'hpc_account_bindings_status_transition';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'claimed' AND NEW.status IN ('verified', 'replaced', 'revoked'))
        OR (OLD.status = 'verified' AND NEW.status IN ('replaced', 'revoked'))
    ) THEN
        RAISE EXCEPTION 'an HPC account binding cannot move from % to %', OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'hpc_account_bindings_status_transition';
    END IF;
    IF OLD.verified_at IS NOT NULL AND (
        NEW.verified_at IS DISTINCT FROM OLD.verified_at
        OR NEW.verification_method IS DISTINCT FROM OLD.verification_method
        OR NEW.verification_evidence_ref IS DISTINCT FROM OLD.verification_evidence_ref
        OR NEW.verified_by IS DISTINCT FROM OLD.verified_by
        OR NEW.unix_uid IS DISTINCT FROM OLD.unix_uid
    ) THEN
        RAISE EXCEPTION 'a recorded HPC account verification is immutable'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'hpc_account_bindings_verification_immutable';
    END IF;
    IF NEW.unix_uid IS DISTINCT FROM OLD.unix_uid
       AND NOT (OLD.unix_uid IS NULL AND NEW.status = 'verified' AND OLD.status = 'claimed') THEN
        RAISE EXCEPTION 'the UNIX user ID is recorded only with the verification'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'hpc_account_bindings_unix_uid_immutable';
    END IF;
    IF (OLD.status = 'verified' OR NEW.status = 'verified')
       AND nest_audit_setting('nest.audit_operator') IS NULL THEN
        RAISE EXCEPTION 'only an operator can verify or change a verified HPC account binding'
            USING ERRCODE = 'insufficient_privilege',
                  CONSTRAINT = 'hpc_account_bindings_operator_managed';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER hpc_account_bindings_transition BEFORE UPDATE ON hpc_account_bindings
    FOR EACH ROW EXECUTE FUNCTION nest_hpc_binding_transition();

-- A binding is replaced only by a later version of the same User and Platform.
CREATE FUNCTION nest_check_hpc_binding_replacement() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.replaced_by_binding_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM hpc_account_bindings r
        WHERE r.id = NEW.replaced_by_binding_id AND r.user_id = NEW.user_id
          AND r.platform_id = NEW.platform_id AND r.version > NEW.version
    ) THEN
        RAISE EXCEPTION 'a binding is replaced only by a later version of the same User and Platform'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'hpc_account_bindings_replacement';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER hpc_account_bindings_replacement
    AFTER UPDATE ON hpc_account_bindings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nest_check_hpc_binding_replacement();
CREATE TRIGGER hpc_account_bindings_no_delete BEFORE DELETE ON hpc_account_bindings
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER hpc_account_bindings_no_truncate BEFORE TRUNCATE ON hpc_account_bindings
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

CREATE FUNCTION nest_security_audit_hpc_binding() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    event text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        event := CASE NEW.status WHEN 'verified' THEN 'hpc_binding.verified'
                                 ELSE 'hpc_binding.claimed' END;
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        event := 'hpc_binding.' || NEW.status;
    ELSE
        RETURN NULL;
    END IF;
    PERFORM nest_security_audit(
        event, p_target => NEW.user_id, p_resource_type => 'hpc_binding',
        p_resource_id => NEW.id::text, p_reason => NEW.revoked_reason,
        p_details => jsonb_build_object(
            'platform_id', NEW.platform_id, 'version', NEW.version,
            'unix_user', NEW.unix_user, 'claim_source', NEW.claim_source,
            'verification_method', NEW.verification_method,
            'verification_evidence_ref', NEW.verification_evidence_ref,
            'replaced_by_binding_id', NEW.replaced_by_binding_id));
    RETURN NULL;
END;
$$;

CREATE TRIGGER hpc_account_bindings_security_audit AFTER INSERT OR UPDATE ON hpc_account_bindings
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_hpc_binding();

-- Attempts pin their authority ------------------------------------------------

ALTER TABLE execution_attempts
    ADD COLUMN execution_authority text
        CHECK (execution_authority IN ('controller_owned', 'user_owned')),
    ADD COLUMN platform_authority_policy_version integer
        CHECK (platform_authority_policy_version >= 0),
    ADD COLUMN project_authority_policy_version integer
        CHECK (project_authority_policy_version >= 0),
    ADD CONSTRAINT execution_attempts_authority_complete CHECK (
        (execution_authority IS NULL) = (platform_authority_policy_version IS NULL)
        AND (execution_authority IS NULL) = (project_authority_policy_version IS NULL)),
    -- A user_owned attempt runs as the User's verified binding; a controller_owned
    -- attempt runs as the Controller and names no User binding.
    ADD CONSTRAINT execution_attempts_authority_binding CHECK (
        execution_authority IS NULL
        OR (execution_authority = 'user_owned') = (hpc_binding_id IS NOT NULL));

CREATE TRIGGER execution_attempts_authority_immutable BEFORE UPDATE ON execution_attempts
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'execution_authority', 'platform_authority_policy_version',
        'project_authority_policy_version');

-- For every writer: a new attempt names its authority, the authority is one the
-- policies in force permit, the pinned versions are the ones in force, and a
-- user_owned attempt runs as the execution owner's verified binding on the
-- attempt's Platform. The deployment layer (NEST_EXECUTION_AUTHORITIES) is
-- checked by the Controller before it records the attempt.
CREATE FUNCTION nest_check_attempt_authority() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    owner uuid;
    project uuid;
    policy record;
    binding hpc_account_bindings;
BEGIN
    IF NEW.execution_authority IS NULL THEN
        RAISE EXCEPTION 'a new execution attempt must pin its execution authority'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'execution_attempts_authority_required';
    END IF;
    SELECT owner_user_id, project_id INTO owner, project
    FROM executions WHERE execution_id = NEW.execution_id;
    PERFORM pg_advisory_xact_lock_shared(
        hashtextextended('nest.hpc_authority:' || NEW.platform_id, 0));
    SELECT * INTO policy FROM nest_effective_authority_policy(project, NEW.platform_id);
    IF policy.platform_version <> NEW.platform_authority_policy_version
       OR policy.project_version <> NEW.project_authority_policy_version THEN
        RAISE EXCEPTION 'the execution authority policy changed since the attempt was resolved'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'execution_attempts_authority_policy_current';
    END IF;
    IF NOT (NEW.execution_authority = ANY (policy.authorities)) THEN
        RAISE EXCEPTION 'execution authority % is not permitted for this Project on the Platform',
            NEW.execution_authority
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'execution_attempts_authority_permitted';
    END IF;
    IF NEW.execution_authority = 'user_owned' THEN
        SELECT * INTO binding FROM hpc_account_bindings
        WHERE id::text = NEW.hpc_binding_id FOR SHARE;
        IF binding.id IS NULL OR binding.status <> 'verified' OR binding.user_id <> owner
           OR binding.platform_id <> NEW.platform_id
           OR binding.version <> NEW.hpc_binding_version
           OR binding.unix_user <> NEW.unix_user
           OR binding.unix_uid IS DISTINCT FROM COALESCE(NEW.unix_uid, binding.unix_uid) THEN
            RAISE EXCEPTION 'a user_owned attempt must run as its owner''s verified HPC binding'
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'execution_attempts_authority_binding_verified';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_attempts_authority BEFORE INSERT ON execution_attempts
    FOR EACH ROW EXECUTE FUNCTION nest_check_attempt_authority();
