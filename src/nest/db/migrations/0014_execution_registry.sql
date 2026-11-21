-- 0014: Platform-aware durable execution registry (C01).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- A logical execution has a stable Nest execution_id and one row per submission
-- attempt. Every row is written, and committed, before the irreversible step it
-- describes (sbatch), so a Controller that restarts finds each execution and its
-- in-flight attempt again.
--
-- Identities are kept apart. An execution belongs to a Nest User and a Project.
-- An attempt names the Platform it ran on and, within that Platform, the login
-- node, transport, HPC binding, effective UNIX identity and Slurm account. A
-- Slurm job ID or account name is only meaningful together with its platform_id,
-- so neither is unique on its own and every lookup by one is scoped by the other.
--
-- platform_id refers to the HPC Platform Catalog (0012) and slurm_account_id to
-- the Platform-scoped Slurm Account Registry (0013). login_node_id names the
-- hpc_login_nodes row the server selected, but is not a foreign key: an operator
-- may remove a login node, and the attempt keeps a snapshot of the endpoint it
-- used (host, port, transport mode and host-key pin reference), so its history
-- stays correct whatever later happens to the registry.

CREATE TABLE executions (
    execution_id     text PRIMARY KEY CHECK (execution_id ~ '^[0-9a-f]{32}$'),
    project_id       uuid NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
    -- The Nest User who owns (submitted) the execution.
    owner_user_id    uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    kind             text NOT NULL CHECK (kind IN ('execution', 'batch')),
    -- Client-chosen key that makes registration idempotent within the owner's
    -- Project. A repeated registration with the same key returns this row.
    idempotency_key  text CHECK (idempotency_key ~ '^[\x21-\x7e]{1,200}$'),
    -- SHA-256 of the canonical request (kind and requested options). A repeated
    -- key with a different request is refused rather than silently merged.
    request_digest   bytea NOT NULL CHECK (length(request_digest) = 32),
    -- Options exactly as the caller requested them, before any default applied.
    requested_options jsonb NOT NULL CHECK (jsonb_typeof(requested_options) = 'object'),
    -- 'pending': no attempt is in flight; a new one may start.
    -- 'active': one attempt is in flight.
    -- 'failed': the last attempt failed; a retry may start a new one.
    -- 'succeeded', 'cancelled': final.
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'active', 'succeeded', 'failed', 'cancelled')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    CHECK ((status IN ('succeeded', 'cancelled')) = (finished_at IS NOT NULL))
);

CREATE UNIQUE INDEX executions_idempotency
    ON executions (project_id, owner_user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX executions_project ON executions (project_id, created_at);
CREATE INDEX executions_owner ON executions (owner_user_id, created_at);

CREATE TRIGGER executions_identity_immutable BEFORE UPDATE ON executions
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'execution_id', 'project_id', 'owner_user_id', 'kind', 'idempotency_key',
        'request_digest', 'requested_options', 'created_at');
CREATE TRIGGER executions_no_delete BEFORE DELETE ON executions
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER executions_no_truncate BEFORE TRUNCATE ON executions
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

CREATE TABLE execution_attempts (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    execution_id        text NOT NULL REFERENCES executions (execution_id),
    attempt_number      integer NOT NULL CHECK (attempt_number >= 1),
    -- Platform (Slurm namespace) and the login-node endpoint selected for it.
    platform_id         uuid NOT NULL REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    login_node_id       bigint NOT NULL,
    login_node_host     text NOT NULL
                        CHECK (login_node_host ~ '^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'),
    -- NULL only in Mode A, where SSH's own default applies.
    login_node_port     integer CHECK (login_node_port BETWEEN 1 AND 65535),
    transport_mode      text NOT NULL CHECK (transport_mode IN ('A', 'B')),
    host_key_pin_ref    text CHECK (host_key_pin_ref ~ '^[A-Za-z0-9_.:/+=@-]{1,200}$'),
    -- HPC binding (the Nest-to-HPC identity mapping) and its version at submission.
    hpc_binding_id      text CHECK (length(hpc_binding_id) BETWEEN 1 AND 200),
    hpc_binding_version integer CHECK (hpc_binding_version >= 1),
    -- Effective UNIX identity on the Platform.
    unix_user           text NOT NULL CHECK (unix_user ~ '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$'),
    unix_uid            integer CHECK (unix_uid >= 0),
    -- The Slurm account charged, from the Platform-scoped account registry (0013),
    -- and which selection step chose it. The composite foreign key keeps the
    -- account on the attempt's own Platform. 'native_default' (Mode B only) names
    -- no account: Slurm charges the HPC identity's own default.
    slurm_account_id    bigint,
    slurm_account       text CHECK (slurm_account ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    slurm_account_origin text NOT NULL CHECK (slurm_account_origin IN (
                            'explicit', 'project_default', 'mode_a_operator_default',
                            'mode_b_user_default', 'native_default')),
    -- 'sha256:<64 hex>' digests of the code, environment and runtime submitted.
    source_digest       text CHECK (source_digest ~ '^sha256:[0-9a-f]{64}$'),
    environment_digest  text CHECK (environment_digest ~ '^sha256:[0-9a-f]{64}$'),
    runtime_digest      text CHECK (runtime_digest ~ '^sha256:[0-9a-f]{64}$'),
    -- 'submitting' is written before sbatch. 'unknown': sbatch may have run but no
    -- job ID arrived; it holds the execution until reconciled, so it is never
    -- resubmitted blindly.
    status              text NOT NULL DEFAULT 'submitting'
                        CHECK (status IN ('submitting', 'submitted', 'running', 'succeeded',
                                          'failed', 'cancelled', 'unknown')),
    slurm_job_id        bigint CHECK (slurm_job_id > 0),
    slurm_state         text CHECK (length(slurm_state) BETWEEN 1 AND 100),
    output_ref          text CHECK (length(output_ref) BETWEEN 1 AND 2000),
    usage_ref           text CHECK (length(usage_ref) BETWEEN 1 AND 2000),
    error_kind          text CHECK (length(error_kind) BETWEEN 1 AND 100),
    error_message       text CHECK (length(error_message) <= 4000),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    -- When Nest recorded the Slurm job ID: on sbatch's reply, or when an
    -- 'unknown' attempt was reconciled.
    submitted_at        timestamptz,
    finished_at         timestamptz,
    UNIQUE (execution_id, attempt_number),
    CHECK ((hpc_binding_id IS NULL) = (hpc_binding_version IS NULL)),
    CHECK (transport_mode = 'A' OR (login_node_port IS NOT NULL AND host_key_pin_ref IS NOT NULL)),
    CONSTRAINT execution_attempts_slurm_account_same_platform
        FOREIGN KEY (platform_id, slurm_account_id)
        REFERENCES slurm_accounts (platform_id, id) ON DELETE RESTRICT,
    CONSTRAINT execution_attempts_slurm_account_origin
        CHECK (CASE WHEN slurm_account_origin = 'native_default'
                    THEN slurm_account_id IS NULL AND slurm_account IS NULL
                         AND transport_mode = 'B'
                    ELSE slurm_account_id IS NOT NULL AND slurm_account IS NOT NULL END),
    CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)),
    -- An attempt Slurm accepted always has its Slurm identity. Only an attempt that
    -- failed or was cancelled before submission, or whose outcome is still
    -- 'unknown', may lack one.
    CONSTRAINT execution_attempts_submitted_has_job
        CHECK (status NOT IN ('submitted', 'running', 'succeeded')
               OR (slurm_job_id IS NOT NULL AND submitted_at IS NOT NULL)),
    CHECK ((slurm_job_id IS NULL) = (submitted_at IS NULL))
);

-- At most one attempt per execution is in flight.
CREATE UNIQUE INDEX execution_attempts_one_live ON execution_attempts (execution_id)
    WHERE status IN ('submitting', 'submitted', 'running', 'unknown');
-- Slurm job IDs are scoped by Platform and are not unique even there (they reset).
CREATE INDEX execution_attempts_platform_job ON execution_attempts (platform_id, slurm_job_id)
    WHERE slurm_job_id IS NOT NULL;
CREATE INDEX execution_attempts_platform_account
    ON execution_attempts (platform_id, slurm_account, created_at);
CREATE INDEX execution_attempts_slurm_account ON execution_attempts (slurm_account_id)
    WHERE slurm_account_id IS NOT NULL;
CREATE INDEX execution_attempts_live ON execution_attempts (status, updated_at)
    WHERE status IN ('submitting', 'submitted', 'running', 'unknown');

-- Allowed attempt transitions. Final states never change, and the Slurm job ID,
-- once recorded, is never replaced.
CREATE FUNCTION nest_execution_attempt_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.slurm_job_id IS NOT NULL AND NEW.slurm_job_id IS DISTINCT FROM OLD.slurm_job_id THEN
        RAISE EXCEPTION 'execution_attempts.slurm_job_id is immutable once recorded'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'execution_attempts_slurm_job_id_immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'submitting'
            AND NEW.status IN ('submitted', 'failed', 'cancelled', 'unknown'))
        OR (OLD.status = 'submitted'
            AND NEW.status IN ('running', 'succeeded', 'failed', 'cancelled', 'unknown'))
        OR (OLD.status = 'running'
            AND NEW.status IN ('succeeded', 'failed', 'cancelled', 'unknown'))
        OR (OLD.status = 'unknown'
            AND NEW.status IN ('submitted', 'running', 'succeeded', 'failed', 'cancelled'))
    ) THEN
        RAISE EXCEPTION 'execution attempt cannot move from % to %', OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'execution_attempts_status_transition';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_attempts_transition BEFORE UPDATE ON execution_attempts
    FOR EACH ROW EXECUTE FUNCTION nest_execution_attempt_transition();
CREATE TRIGGER execution_attempts_identity_immutable BEFORE UPDATE ON execution_attempts
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'id', 'execution_id', 'attempt_number', 'platform_id', 'login_node_id',
        'login_node_host', 'login_node_port', 'transport_mode', 'host_key_pin_ref',
        'hpc_binding_id',
        'hpc_binding_version', 'unix_user', 'unix_uid', 'slurm_account_id', 'slurm_account',
        'slurm_account_origin', 'source_digest',
        'environment_digest', 'runtime_digest', 'created_at');
-- An attempt's account name is always its registered account's.
CREATE FUNCTION nest_check_attempt_account() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.slurm_account_id IS NOT NULL AND NEW.slurm_account IS DISTINCT FROM (
        SELECT account_name FROM slurm_accounts WHERE id = NEW.slurm_account_id
    ) THEN
        RAISE EXCEPTION 'the attempt account does not match its registered Slurm account'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'execution_attempts_slurm_account_name';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_attempts_account_name BEFORE INSERT ON execution_attempts
    FOR EACH ROW EXECUTE FUNCTION nest_check_attempt_account();
CREATE TRIGGER execution_attempts_no_delete BEFORE DELETE ON execution_attempts
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER execution_attempts_no_truncate BEFORE TRUNCATE ON execution_attempts
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- The configuration each attempt resolved, one row per option, written with the
-- attempt and never changed. origin 'request': the caller set it explicitly, and
-- default_value/default_source record the default it overrode, if any.
-- origin 'default': no request value; default_source names the layer that
-- supplied it (such as 'platform', 'project' or 'nest').
CREATE TABLE execution_attempt_options (
    attempt_id      bigint NOT NULL REFERENCES execution_attempts (id),
    name            text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.]{0,63}$'),
    value           jsonb NOT NULL,
    origin          text NOT NULL CHECK (origin IN ('request', 'default')),
    default_value   jsonb,
    default_source  text CHECK (default_source ~ '^[a-z][a-z0-9_]{0,31}$'),
    PRIMARY KEY (attempt_id, name),
    CHECK ((default_value IS NULL) = (default_source IS NULL)),
    CHECK (origin = 'request' OR (default_source IS NOT NULL AND value = default_value))
);

CREATE TRIGGER execution_attempt_options_append_only
    BEFORE UPDATE OR DELETE ON execution_attempt_options
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER execution_attempt_options_no_truncate BEFORE TRUNCATE ON execution_attempt_options
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- Compute nodes Slurm allocated to an attempt. Node names belong to the attempt's
-- Platform; query them through execution_attempts.platform_id.
CREATE TABLE execution_attempt_nodes (
    attempt_id  bigint NOT NULL REFERENCES execution_attempts (id),
    node_name   text NOT NULL CHECK (length(node_name) BETWEEN 1 AND 253),
    PRIMARY KEY (attempt_id, node_name)
);

CREATE INDEX execution_attempt_nodes_node ON execution_attempt_nodes (node_name, attempt_id);

CREATE TRIGGER execution_attempt_nodes_append_only
    BEFORE UPDATE OR DELETE ON execution_attempt_nodes
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER execution_attempt_nodes_no_truncate BEFORE TRUNCATE ON execution_attempt_nodes
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- An execution's status follows its attempts, for every writer:
--   no attempt yet              -> 'pending'
--   latest attempt in flight    -> 'active' ('submitting', 'submitted', 'running', 'unknown')
--   latest attempt final        -> that status ('succeeded', 'failed' or 'cancelled')
-- Checked at commit, so the registry may write the attempt and its execution in
-- either order within one transaction.
CREATE FUNCTION nest_check_execution_lifecycle() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    eid text := NEW.execution_id;
    current text;
    latest text;
    expected text;
BEGIN
    SELECT status INTO current FROM executions WHERE execution_id = eid;
    SELECT status INTO latest FROM execution_attempts
        WHERE execution_id = eid ORDER BY attempt_number DESC LIMIT 1;
    expected := CASE
        WHEN latest IS NULL THEN 'pending'
        WHEN latest IN ('submitting', 'submitted', 'running', 'unknown') THEN 'active'
        ELSE latest
    END;
    IF current IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'execution % is % but its attempts say %', eid, current, expected
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'executions_status_follows_attempts';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER executions_status_follows_attempts
    AFTER INSERT OR UPDATE ON executions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nest_check_execution_lifecycle();
CREATE CONSTRAINT TRIGGER execution_attempts_status_follows_attempts
    AFTER INSERT OR UPDATE ON execution_attempts
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nest_check_execution_lifecycle();

-- Allowed execution transitions. 'succeeded' and 'cancelled' are final; a failed
-- execution may only be retried (back to 'active').
CREATE FUNCTION nest_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'pending' AND NEW.status = 'active')
        OR (OLD.status = 'active' AND NEW.status IN ('succeeded', 'failed', 'cancelled'))
        OR (OLD.status = 'failed' AND NEW.status = 'active')
    ) THEN
        RAISE EXCEPTION 'execution cannot move from % to %', OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'executions_status_transition';
    END IF;
    IF OLD.status IN ('succeeded', 'cancelled')
       AND NEW.finished_at IS DISTINCT FROM OLD.finished_at THEN
        RAISE EXCEPTION 'a finished execution keeps its finished_at'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'executions_status_transition';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER executions_transition BEFORE UPDATE ON executions
    FOR EACH ROW EXECUTE FUNCTION nest_execution_transition();
