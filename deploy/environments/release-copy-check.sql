-- Option B/C of docs/source/multi-environment.md (#118): run against Release's
-- RESTORED database with Release's Controller stopped, before it first starts.
--
--   psql -v ON_ERROR_STOP=1 -U nest -d nest -f release-copy-check.sql
--
-- Read-only. It fails while the copy holds work in flight from Main: live
-- execution attempts carry Main's Slurm job IDs and hold admission reservations,
-- which Release's recovery would query for and count against its own limits. Quiesce
-- Main (no live or unknown attempt in `nest-db execution list`) and dump again.
-- It then counts what Main issued that would stay usable on Release; every count
-- must be 0 after release-copy-revoke.sql. Only counts are printed.
DO $$
DECLARE
    live bigint;
    held bigint;
BEGIN
    SELECT count(*) INTO live FROM execution_attempts
        WHERE status IN ('submitting', 'submitted', 'running', 'unknown');
    SELECT count(*) INTO held FROM execution_reservations r
        JOIN execution_attempts a ON a.id = r.attempt_id
        WHERE a.status IN ('submitting', 'submitted', 'running', 'unknown');
    IF live > 0 OR held > 0 THEN
        RAISE EXCEPTION 'the copy holds % live attempt(s) and % held reservation(s) from Main; quiesce Main and dump again',
            live, held;
    END IF;
END
$$;

SELECT 'invite links usable' AS item, count(*) AS remaining FROM project_invite_links
    WHERE revoked_at IS NULL AND expires_at > now() AND use_count < max_uses
UNION ALL
SELECT 'email invitations pending', count(*) FROM project_email_invitations WHERE status = 'pending'
UNION ALL
SELECT 'user sessions unrevoked', count(*) FROM user_sessions
    WHERE revoked_at IS NULL AND expires_at > now()
UNION ALL
SELECT 'service sessions unrevoked', count(*) FROM service_sessions WHERE revoked_at IS NULL
UNION ALL
SELECT 'service principals active', count(*) FROM service_principals WHERE status = 'active'
UNION ALL
SELECT 'workload trust policies active', count(*) FROM workload_trust_policies WHERE status = 'active'
UNION ALL
SELECT 'service grants active', count(*) FROM service_principal_grants WHERE status = 'active';
