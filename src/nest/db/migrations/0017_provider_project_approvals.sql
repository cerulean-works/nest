-- 0017: durable Provider approvals and the privileged management API (G03).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- A Provider approval is the operator's decision that a Controller's Provider (the
-- Platform Profile it submits to, or `local`) accepts Runs from one Project. It is
-- independent of the Platform grant (0012) and the Slurm account link (0013): all
-- three are checked at every new admission, and none implies another.
--
-- Until now the approval was NEST_PROVIDER_ALLOWED_PROJECTS, read only at start.
-- With NEST_PROVIDER_APPROVALS=database the Controller reads this table instead, on
-- every check, with no cache, so a grant or revocation takes effect from the next
-- submission on every replica without a redeploy. Nothing here imports the
-- environment list: `nest-db provider import` copies explicit Project IDs only,
-- refuses '*', and never turns a recorded revocation back into an approval.
--
-- A revocation is kept as a row (status 'revoked'), never deleted, so an import
-- or a restart cannot silently regrant it; only an explicit approval can. It
-- affects new submissions only: running jobs are not cancelled.

CREATE TABLE provider_project_approvals (
    -- The Provider key the Controller records with each decision: the Platform
    -- Profile ID it submits to, such as 'default' or 'nano4', or 'local'.
    provider     text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_]{0,31}$'),
    project_id   uuid NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
    status       text NOT NULL CHECK (status IN ('approved', 'revoked')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, project_id)
);

CREATE INDEX provider_project_approvals_project ON provider_project_approvals (project_id);

CREATE TRIGGER provider_project_approvals_immutable BEFORE UPDATE ON provider_project_approvals
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('provider', 'project_id', 'created_at');
CREATE TRIGGER provider_project_approvals_no_delete BEFORE DELETE ON provider_project_approvals
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER provider_project_approvals_no_truncate BEFORE TRUNCATE ON provider_project_approvals
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- Operator-managed rows (0012) may now also be changed by an active
-- platform_admin acting through Nest, on the three tables the privileged
-- management API writes: Platform grants, Project account links and Provider
-- approvals. The administrator is named by nest.audit_actor_user_id and checked
-- here, in the writing transaction, so a revoked or disabled administrator is
-- refused even if the application were wrong. Every other operator-managed table
-- (Platforms, login nodes, Slurm accounts, account policies) still needs an
-- operator.
CREATE OR REPLACE FUNCTION nest_require_operator() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    changed text;
    actor uuid;
BEGIN
    IF nest_audit_setting('nest.audit_operator') IS NOT NULL THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_TABLE_NAME IN ('hpc_platform_project_grants', 'project_platform_accounts',
                         'provider_project_approvals') THEN
        actor := nest_audit_setting('nest.audit_actor_user_id')::uuid;
        IF actor IS NOT NULL AND EXISTS (
            SELECT 1 FROM platform_role_assignments r JOIN users u ON u.id = r.user_id
            WHERE r.user_id = actor AND r.role = 'platform_admin' AND u.status = 'active'
        ) THEN
            RETURN COALESCE(NEW, OLD);
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        SELECT string_agg(key, ',' ORDER BY key) INTO changed
        FROM jsonb_each(to_jsonb(NEW)) n
        WHERE NOT (key = ANY (COALESCE(TG_ARGV, '{}'::text[])))
          AND n.value IS DISTINCT FROM to_jsonb(OLD) -> key;
        IF changed IS NULL THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION '% is operator managed: name the operator making the change', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege',
              CONSTRAINT = TG_TABLE_NAME || '_operator_managed';
END;
$$;

CREATE TRIGGER provider_project_approvals_operator_managed
    BEFORE INSERT OR UPDATE ON provider_project_approvals
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();

-- Every approval and revocation, whoever made it, in the security audit log.
CREATE FUNCTION nest_security_audit_provider_approval() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NULL;
    END IF;
    PERFORM nest_security_audit(
        CASE NEW.status WHEN 'approved' THEN 'provider.approval.granted'
                        ELSE 'provider.approval.revoked' END,
        p_project => NEW.project_id, p_resource_type => 'provider',
        p_resource_id => NEW.provider);
    RETURN NULL;
END;
$$;

CREATE TRIGGER provider_project_approvals_security_audit
    AFTER INSERT OR UPDATE ON provider_project_approvals
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_provider_approval();
