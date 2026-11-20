-- 0011: Platform-aware durable execution registry (C01).
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
-- platform_id and login_node_ref are opaque identifiers, not foreign keys: the
-- Platform catalog and login-node registry (D07) do not exist yet. Each attempt
-- keeps a snapshot of the endpoint it used, so history stays correct when the
-- catalog later changes; a later migration may add the foreign keys.

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
    -- Platform and its selected login-node endpoint, as used by this attempt.
    platform_id         text NOT NULL CHECK (platform_id ~ '^[a-z][a-z0-9_-]{0,62}$'),
    login_node_ref      text CHECK (length(login_node_ref) BETWEEN 1 AND 200),
    login_node_host     text NOT NULL CHECK (length(login_node_host) BETWEEN 1 AND 253),
    login_node_port     integer NOT NULL CHECK (login_node_port BETWEEN 1 AND 65535),
    transport_mode      text NOT NULL CHECK (transport_mode ~ '^[a-z][a-z0-9_-]{0,31}$'),
    -- HPC binding (the Nest-to-HPC identity mapping) and its version at submission.
    hpc_binding_id      text CHECK (length(hpc_binding_id) BETWEEN 1 AND 200),
    hpc_binding_version integer CHECK (hpc_binding_version >= 1),
    -- Effective UNIX identity on the Platform.
    unix_user           text NOT NULL CHECK (unix_user ~ '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$'),
    unix_uid            integer CHECK (unix_uid >= 0),
    -- Slurm account, entitled per (platform_id, slurm_account).
    slurm_account       text NOT NULL CHECK (length(slurm_account) BETWEEN 1 AND 200),
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
    submitted_at        timestamptz,
    finished_at         timestamptz,
    UNIQUE (execution_id, attempt_number),
    CHECK ((hpc_binding_id IS NULL) = (hpc_binding_version IS NULL)),
    CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL))
);

-- At most one attempt per execution is in flight.
CREATE UNIQUE INDEX execution_attempts_one_live ON execution_attempts (execution_id)
    WHERE status IN ('submitting', 'submitted', 'running', 'unknown');
-- Slurm job IDs are scoped by Platform and are not unique even there (they reset).
CREATE INDEX execution_attempts_platform_job ON execution_attempts (platform_id, slurm_job_id)
    WHERE slurm_job_id IS NOT NULL;
CREATE INDEX execution_attempts_platform_account
    ON execution_attempts (platform_id, slurm_account, created_at);
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
        'id', 'execution_id', 'attempt_number', 'platform_id', 'login_node_ref',
        'login_node_host', 'login_node_port', 'transport_mode', 'hpc_binding_id',
        'hpc_binding_version', 'unix_user', 'unix_uid', 'slurm_account', 'source_digest',
        'environment_digest', 'runtime_digest', 'created_at');
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
