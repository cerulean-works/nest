-- 0020: Pin the worker protocol of each execution attempt (#73).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- Before it runs `sbatch`, the Controller reads the protocol version that the
-- deployed worker script declares and records it here, so how the attempt's job
-- completion settles never depends on what is deployed later:
--
--   2: the worker runs in the attempt's own artifact directory
--      (<execution>/attempts/<attempt_number>), publishes output.pkl atomically
--      and always writes the plain-JSON outcome record. A completed job without
--      an outcome record is never a success.
--   1: a worker that predates the outcome record, in the shared execution
--      directory. Admitted only when the operator allows legacy workers
--      (NEST_LEGACY_WORKERS=allow); its completion is an unverified success.
--
-- The value is set once, while the attempt is still `submitting`, and never
-- changes. Attempts recorded before this migration keep NULL and settle as
-- they did before it.

ALTER TABLE execution_attempts
    ADD COLUMN worker_protocol smallint CHECK (worker_protocol BETWEEN 1 AND 1000);

CREATE FUNCTION nest_execution_attempt_worker_protocol() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.worker_protocol IS NOT DISTINCT FROM OLD.worker_protocol THEN
        RETURN NEW;
    END IF;
    IF OLD.worker_protocol IS NOT NULL THEN
        RAISE EXCEPTION 'execution_attempts.worker_protocol is immutable once recorded'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'execution_attempts_worker_protocol_immutable';
    END IF;
    IF OLD.status <> 'submitting' THEN
        RAISE EXCEPTION 'execution_attempts.worker_protocol is recorded only before submission'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'execution_attempts_worker_protocol_before_submission';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_attempts_worker_protocol BEFORE UPDATE ON execution_attempts
    FOR EACH ROW EXECUTE FUNCTION nest_execution_attempt_worker_protocol();
