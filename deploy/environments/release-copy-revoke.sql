-- Option B/C of docs/source/multi-environment.md (#118): run against Release's
-- RESTORED database with Release's Controller stopped, after release-copy-check.sql
-- passed its first part. Revokes everything Main issued that would otherwise stay
-- usable on Release:
--   - every unrevoked invite link and every pending email invitation (bearer
--     tokens issued to Main's developer audience);
--   - every user session and every service session;
--   - every service principal, workload trust policy and grant (audited). Recreate on Release,
--     with Release's own NEST_WORKLOAD_OIDC_AUDIENCE, only those the operator
--     re-approves for Release (`nest-db service-principal create/trust/grant`).
-- Name who runs it: `nest.revoker` is the Release platform administrator's User ID,
-- recorded as the one who revoked the invitations, and `nest.operator` goes into the
-- audit log with a fixed reason, as `nest-db` records its own changes:
--
--   psql -v ON_ERROR_STOP=1 -U nest -d nest \
--     -c "SET nest.revoker = '<user uuid>'" -c "SET nest.operator = '<your name>'" \
--     -f release-copy-revoke.sql
--
-- One transaction: all of it or nothing. Also record the run (time, operator, this
-- file's commit) in #118 or the deployment log.
BEGIN;

DO $$
BEGIN
    IF NULLIF(current_setting('nest.revoker', true), '') IS NULL
       OR NULLIF(current_setting('nest.operator', true), '') IS NULL THEN
        RAISE EXCEPTION 'set nest.revoker (a User ID) and nest.operator (your name) first';
    END IF;
END
$$;
SELECT set_config('nest.audit_actor_user_id', current_setting('nest.revoker'), true),
       set_config('nest.audit_operator', current_setting('nest.operator'), true),
       set_config('nest.audit_reason', 'copied from Main into Release (#118): revoke Main-issued access', true);

UPDATE project_invite_links
    SET revoked_at = now(), revoked_by_user_id = current_setting('nest.revoker')::uuid
    WHERE revoked_at IS NULL;

UPDATE project_email_invitations
    SET status = 'revoked', revoked_at = now(),
        revoked_by_user_id = current_setting('nest.revoker')::uuid, updated_at = now()
    WHERE status = 'pending';

UPDATE user_sessions SET revoked_at = now(), revoked_reason = 'environment_copy'
    WHERE revoked_at IS NULL;

UPDATE service_sessions SET revoked_at = now(), revoked_reason = 'revoked'
    WHERE revoked_at IS NULL;

UPDATE service_principal_grants SET status = 'revoked', revoked_at = now()
    WHERE status = 'active';

UPDATE workload_trust_policies SET status = 'revoked', revoked_at = now()
    WHERE status = 'active';

UPDATE service_principals SET status = 'revoked', revoked_at = now()
    WHERE status = 'active';

COMMIT;
