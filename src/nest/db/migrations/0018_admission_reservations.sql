-- 0018: trusted-pilot admission reservations (F02a).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- Every attempt the Execution Registry admits (0014) reserves what it asked for,
-- in the same transaction that records it: the CPUs and GPUs requested and the
-- walltime it was submitted with, attributed to the execution's owner, Project
-- and Platform. A reservation holds for exactly as long as its attempt is live
-- ('submitting', 'submitted', 'running' or 'unknown') and is released by the
-- attempt reaching a final state. Nothing else releases it: no timeout, no RPC
-- disconnect, no restart. An attempt whose outcome is unknown keeps its
-- reservation until an operator reconciles it, because its job may be running.
--
-- The reservation itself is append-only and never changes. Whether it is held
-- is derived from the attempt's status, so a release can never be applied twice
-- or be lost: there is no second counter to drift from the attempt.
--
-- Admission takes the 'nest.admission' advisory lock exclusively, counts the
-- held reservations, and inserts its own before committing, so concurrent
-- requests on any number of Controller replicas sharing this database apply one
-- after another and never oversubscribe a limit.

CREATE TABLE execution_reservations (
    attempt_id        bigint PRIMARY KEY REFERENCES execution_attempts (id),
    -- Copied from the execution and the attempt (checked below) so that counting
    -- needs no join through executions.
    project_id        uuid NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
    owner_user_id     uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    platform_id       uuid NOT NULL REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    -- What the job is allocated: the request, or the batch script's default where
    -- the request asks for none, and for an environment build the larger of the
    -- build's and the worker's allocation.
    cpu               integer NOT NULL CHECK (cpu >= 0),
    gpu               integer NOT NULL CHECK (gpu >= 0),
    -- false when Nest cannot bound what the job allocates (a SubmitJob script's
    -- own #SBATCH directives, under NEST_LIMIT_BATCH_SCRIPTS=trusted, or a
    -- reservation backfilled below): cpu and gpu are then only what was asked for.
    bounded           boolean NOT NULL DEFAULT true,
    -- The --time the attempt was submitted with; NULL only for a reservation
    -- backfilled below from an attempt admitted before this migration.
    walltime_seconds  integer CHECK (walltime_seconds > 0),
    -- The limits in force when it was admitted, for audit.
    limits            jsonb NOT NULL CHECK (jsonb_typeof(limits) = 'object'),
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX execution_reservations_owner ON execution_reservations (owner_user_id, created_at);
CREATE INDEX execution_reservations_project ON execution_reservations (project_id);
CREATE INDEX execution_reservations_platform ON execution_reservations (platform_id);

CREATE FUNCTION nest_check_execution_reservation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM execution_attempts a JOIN executions e USING (execution_id)
        WHERE a.id = NEW.attempt_id AND e.project_id = NEW.project_id
          AND e.owner_user_id = NEW.owner_user_id AND a.platform_id = NEW.platform_id
    ) THEN
        RAISE EXCEPTION 'a reservation must name its attempt''s Project, owner and Platform'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'execution_reservations_attribution';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_reservations_attribution BEFORE INSERT ON execution_reservations
    FOR EACH ROW EXECUTE FUNCTION nest_check_execution_reservation();
CREATE TRIGGER execution_reservations_append_only
    BEFORE UPDATE OR DELETE ON execution_reservations
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER execution_reservations_no_truncate BEFORE TRUNCATE ON execution_reservations
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- Attempts already live when this migration runs hold what they asked for, so
-- the first admission after the upgrade counts them too.
INSERT INTO execution_reservations
    (attempt_id, project_id, owner_user_id, platform_id, cpu, gpu, bounded, limits, created_at)
SELECT a.id, e.project_id, e.owner_user_id, a.platform_id,
       COALESCE((SELECT (o.value #>> '{}')::integer FROM execution_attempt_options o
                 WHERE o.attempt_id = a.id AND o.name = 'cpu'
                   AND jsonb_typeof(o.value) = 'number'), 0),
       COALESCE((SELECT (o.value #>> '{}')::integer FROM execution_attempt_options o
                 WHERE o.attempt_id = a.id AND o.name = 'gpu'
                   AND jsonb_typeof(o.value) = 'number'), 0),
       false, '{"backfilled": true}'::jsonb, a.created_at
FROM execution_attempts a JOIN executions e USING (execution_id)
WHERE a.status IN ('submitting', 'submitted', 'running', 'unknown');
