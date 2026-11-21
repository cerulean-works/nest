-- 0013: Platform-scoped Slurm Account Registry and Project mapping (D08).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- A Slurm account exists only inside one HPC Platform's namespace (0012). The same
-- account name on `nano4` and on `f1` is two unrelated accounts, with their own
-- rows, permissions and usage, so every account is keyed by (platform_id,
-- account_name) and every reference to one carries its Platform. A Nest Project is
-- never identified, renamed or merged by a Slurm account name: Projects keep their
-- immutable IDs (0003) and are only linked to accounts here.
--
-- Accounts, Project links and Project account policies are operator managed, like
-- the Platform Catalog: every change must name an operator (0012's
-- nest_require_operator) and is recorded in security_audit_events (0009). Nothing
-- here is an HPC credential, and nothing here grants a Slurm or UNIX permission:
-- the cluster still decides whether an HPC identity may charge an account.

CREATE TABLE slurm_accounts (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The Platform whose namespace the account lives in. Never changes.
    platform_id   uuid NOT NULL REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    -- The Slurm account name. Slurm stores account names in lowercase.
    account_name  text NOT NULL CHECK (account_name ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    -- 'standard': chargeable under the identity a link allows (Mode A and/or B).
    -- 'chargeback': an operator account charged through the Controller's own HPC
    -- identity (Mode A only). It can never be linked for Mode B, so it is never a
    -- Mode B account or fallback.
    kind          text NOT NULL DEFAULT 'standard' CHECK (kind IN ('standard', 'chargeback')),
    description   text CHECK (length(description) BETWEEN 1 AND 200),
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    -- The operator-controlled default of the Platform for Mode A (step 3 of the
    -- account selection), used only for Projects the account is linked to for Mode A.
    is_mode_a_default boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    disabled_at   timestamptz,
    CONSTRAINT slurm_accounts_platform_name_unique UNIQUE (platform_id, account_name),
    -- Target of the composite foreign keys that keep references on one Platform.
    CONSTRAINT slurm_accounts_platform_id_unique UNIQUE (platform_id, id),
    CHECK ((status = 'disabled') = (disabled_at IS NOT NULL)),
    CHECK (NOT (is_mode_a_default AND status = 'disabled'))
);

CREATE UNIQUE INDEX slurm_accounts_one_mode_a_default ON slurm_accounts (platform_id)
    WHERE is_mode_a_default;

CREATE TRIGGER slurm_accounts_immutable BEFORE UPDATE ON slurm_accounts
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'id', 'platform_id', 'account_name', 'kind', 'created_at');
-- Jobs keep referring to an account, so its row is disabled, never removed.
CREATE TRIGGER slurm_accounts_no_delete BEFORE DELETE ON slurm_accounts
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER slurm_accounts_no_truncate BEFORE TRUNCATE ON slurm_accounts
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER slurm_accounts_operator_managed BEFORE INSERT OR UPDATE ON slurm_accounts
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();

-- The Slurm accounts a Project may charge on a Platform. A link exists only while
-- the Project is granted the Platform (0012): revoking the grant removes the
-- Project's links on that Platform. The composite foreign keys make it impossible
-- for a link to name an account of another Platform.
CREATE TABLE project_platform_accounts (
    project_id        uuid NOT NULL,
    platform_id       uuid NOT NULL,
    slurm_account_id  bigint NOT NULL,
    -- The modes the account may be charged under for this Project: 'A' (the
    -- Controller's operator HPC identity) and/or 'B' (the User's own HPC identity).
    -- A set in canonical order, so duplicates such as {A,A} are refused.
    modes             text[] NOT NULL
                      CHECK (modes IN ('{A}'::text[], '{B}'::text[], '{A,B}'::text[])),
    -- The Project x Platform default (step 2 of the account selection).
    is_default        boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, platform_id, slurm_account_id),
    CONSTRAINT project_platform_accounts_grant
        FOREIGN KEY (platform_id, project_id)
        REFERENCES hpc_platform_project_grants (platform_id, project_id) ON DELETE CASCADE,
    CONSTRAINT project_platform_accounts_same_platform
        FOREIGN KEY (platform_id, slurm_account_id)
        REFERENCES slurm_accounts (platform_id, id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX project_platform_accounts_one_default
    ON project_platform_accounts (project_id, platform_id) WHERE is_default;
CREATE INDEX project_platform_accounts_account ON project_platform_accounts (slurm_account_id);

CREATE TRIGGER project_platform_accounts_immutable BEFORE UPDATE ON project_platform_accounts
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'project_id', 'platform_id', 'slurm_account_id', 'created_at');
CREATE TRIGGER project_platform_accounts_operator_managed
    BEFORE INSERT OR UPDATE OR DELETE ON project_platform_accounts
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();
CREATE TRIGGER project_platform_accounts_no_truncate BEFORE TRUNCATE ON project_platform_accounts
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- A chargeback account is the Controller's: it may only be linked for Mode A.
CREATE FUNCTION nest_check_project_account_link() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF 'B' = ANY (NEW.modes) AND EXISTS (
        SELECT 1 FROM slurm_accounts a WHERE a.id = NEW.slurm_account_id AND a.kind = 'chargeback'
    ) THEN
        RAISE EXCEPTION 'a chargeback account can only be linked for Mode A'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'project_platform_accounts_chargeback_mode_a';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER project_platform_accounts_chargeback_mode_a
    BEFORE INSERT OR UPDATE ON project_platform_accounts
    FOR EACH ROW EXECUTE FUNCTION nest_check_project_account_link();

-- Per Project x Platform account policy. Without a row, nothing optional is allowed.
CREATE TABLE project_platform_account_policies (
    project_id            uuid NOT NULL,
    platform_id           uuid NOT NULL,
    -- Whether a Mode B job may omit --account and let Slurm charge the User's own
    -- default account (step 4 of the account selection). Never used in Mode A,
    -- where the native default would be the Controller's.
    allow_native_default  boolean NOT NULL DEFAULT false,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, platform_id),
    CONSTRAINT project_platform_account_policies_grant
        FOREIGN KEY (platform_id, project_id)
        REFERENCES hpc_platform_project_grants (platform_id, project_id) ON DELETE CASCADE
);

CREATE TRIGGER project_platform_account_policies_immutable
    BEFORE UPDATE ON project_platform_account_policies
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('project_id', 'platform_id');
CREATE TRIGGER project_platform_account_policies_operator_managed
    BEFORE INSERT OR UPDATE OR DELETE ON project_platform_account_policies
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();
CREATE TRIGGER project_platform_account_policies_no_truncate
    BEFORE TRUNCATE ON project_platform_account_policies
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- A job records the registered account it charges and where the choice came from,
-- for durable, auditable history (the execution registry, C01, reads these). NULL
-- for jobs written before 0013 or submitted without the account registry. Both are
-- written with the record and never changed.
ALTER TABLE jobs
    ADD COLUMN slurm_account_id bigint,
    ADD COLUMN slurm_account_origin text CHECK (slurm_account_origin IN (
        'explicit', 'project_default', 'mode_a_operator_default', 'mode_b_user_default',
        'native_default')),
    -- The account must belong to the job's Platform.
    ADD CONSTRAINT jobs_slurm_account_same_platform
        FOREIGN KEY (platform_id, slurm_account_id)
        REFERENCES slurm_accounts (platform_id, id) ON DELETE RESTRICT,
    ADD CONSTRAINT jobs_slurm_account_has_platform
        CHECK (slurm_account_id IS NULL OR platform_id IS NOT NULL),
    -- Every origin but the native default names a registered account; the native
    -- default names none, because Slurm chooses it.
    ADD CONSTRAINT jobs_slurm_account_origin
        CHECK (CASE WHEN slurm_account_origin IS NULL
                         OR slurm_account_origin = 'native_default' THEN slurm_account_id IS NULL
                    ELSE slurm_account_id IS NOT NULL END);

CREATE INDEX jobs_slurm_account ON jobs (slurm_account_id) WHERE slurm_account_id IS NOT NULL;

CREATE TRIGGER jobs_slurm_account_immutable BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'slurm_account_id', 'slurm_account_origin');

-- A job's account name is the registered account's, so the name in the record can
-- never disagree with the account it references.
CREATE FUNCTION nest_check_job_account() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    name text;
BEGIN
    IF NEW.slurm_account_id IS NOT NULL THEN
        SELECT account_name INTO name FROM slurm_accounts WHERE id = NEW.slurm_account_id;
        IF NEW.account IS NULL THEN
            NEW.account := name;
        ELSIF NEW.account IS DISTINCT FROM name THEN
            RAISE EXCEPTION 'the job account does not match its registered Slurm account'
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'jobs_slurm_account_name';
        END IF;
    ELSIF NEW.slurm_account_origin = 'native_default' AND NEW.account IS NOT NULL THEN
        RAISE EXCEPTION 'a job charged to the native Slurm default names no account'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'jobs_slurm_account_name';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER jobs_slurm_account_name BEFORE INSERT ON jobs
    FOR EACH ROW EXECUTE FUNCTION nest_check_job_account();

-- Security audit (0009) of every registry change. Only non-secret attributes.
CREATE FUNCTION nest_security_audit_slurm_account() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    changed text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        changed := 'created';
    ELSE
        SELECT string_agg(key, ',' ORDER BY key) INTO changed
        FROM jsonb_each(to_jsonb(NEW)) n
        WHERE key NOT IN ('updated_at', 'created_at')
          AND n.value IS DISTINCT FROM to_jsonb(OLD) -> key;
    END IF;
    IF changed IS NOT NULL THEN
        PERFORM nest_security_audit(
            'slurm_account.changed', p_resource_type => 'slurm_account',
            p_resource_id => NEW.id::text,
            p_details => jsonb_build_object(
                'changed', changed, 'platform_id', NEW.platform_id,
                'account_name', NEW.account_name, 'kind', NEW.kind, 'status', NEW.status,
                'is_mode_a_default', NEW.is_mode_a_default));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER slurm_accounts_security_audit AFTER INSERT OR UPDATE ON slurm_accounts
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_slurm_account();

CREATE FUNCTION nest_security_audit_project_account() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    link project_platform_accounts;
    changed text;
BEGIN
    link := COALESCE(NEW, OLD);
    IF TG_OP = 'UPDATE' THEN
        SELECT string_agg(key, ',' ORDER BY key) INTO changed
        FROM jsonb_each(to_jsonb(NEW)) n
        WHERE key NOT IN ('updated_at', 'created_at')
          AND n.value IS DISTINCT FROM to_jsonb(OLD) -> key;
        IF changed IS NULL THEN
            RETURN NULL;
        END IF;
    END IF;
    PERFORM nest_security_audit(
        CASE TG_OP WHEN 'INSERT' THEN 'slurm_account.link.added'
                   WHEN 'DELETE' THEN 'slurm_account.link.removed'
                   ELSE 'slurm_account.link.changed' END,
        p_project => link.project_id, p_resource_type => 'slurm_account',
        p_resource_id => link.slurm_account_id::text,
        p_details => jsonb_build_object(
            'changed', changed, 'platform_id', link.platform_id,
            'modes', array_to_string(link.modes, ','), 'is_default', link.is_default));
    RETURN NULL;
END;
$$;

CREATE TRIGGER project_platform_accounts_security_audit
    AFTER INSERT OR UPDATE OR DELETE ON project_platform_accounts
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_project_account();

CREATE FUNCTION nest_security_audit_project_account_policy() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    policy project_platform_account_policies;
BEGIN
    policy := COALESCE(NEW, OLD);
    IF TG_OP = 'UPDATE' AND NEW.allow_native_default IS NOT DISTINCT FROM OLD.allow_native_default
    THEN
        RETURN NULL;
    END IF;
    PERFORM nest_security_audit(
        'slurm_account.policy.changed', p_project => policy.project_id,
        p_resource_type => 'platform', p_resource_id => policy.platform_id::text,
        p_details => jsonb_build_object(
            'allow_native_default', CASE WHEN TG_OP = 'DELETE' THEN false
                                         ELSE NEW.allow_native_default END));
    RETURN NULL;
END;
$$;

CREATE TRIGGER project_platform_account_policies_security_audit
    AFTER INSERT OR UPDATE OR DELETE ON project_platform_account_policies
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_project_account_policy();
