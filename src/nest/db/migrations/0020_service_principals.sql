-- 0020: service principals and CI workload identity (B09).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- A service principal is a non-human Nest caller, such as one CI pipeline. It is
-- not a User: it has no Google identity, no browser or CLI session, no Project
-- membership, no Organization or platform role, and it can never own or submit an
-- execution (executions.owner_user_id still references users). Everything it may
-- do is an explicit, operator-made grant below, checked on every request.
--
-- A principal authenticates without a browser by exchanging a short-lived
-- workload identity token, such as a GitHub Actions OIDC token, at the auth
-- listener. The token is accepted only if it matches one of the principal's trust
-- policies exactly (issuer, audience, repository and its numeric IDs, ref,
-- workflow, environment, triggering event) and its `jti` was never exchanged
-- before. The exchange
-- issues a service session: an opaque bearer token, stored only as its SHA-256
-- digest, that expires within an hour and cannot be refreshed. Service sessions
-- are a separate table from user_sessions and session_access_tokens, so a service
-- token is never a browser or CLI session, and the reverse.
--
-- Grants name capabilities on one Project, optionally on one HPC Platform. The
-- execution capabilities (run.submit, run.read, run.cancel) can be recorded so
-- that a later change (D09/D10) can route them, but this schema gives them no
-- effect: no column here selects an SSH mode, an HPC identity or a credential, and
-- the Controller refuses every HPC-bound call from a service principal.
--
-- Principals, trust policies and grants are operator managed (an operator must be
-- named, as for Platforms), immutable except for revocation, and never deleted.
-- Revocation is final. Revoking a principal or a trust policy revokes its service
-- sessions in the same transaction. Every change is recorded in the security
-- audit log. Nothing secret is stored: no workload token, no token hash outside
-- service_sessions.token_hash, no HPC password, key or MFA secret.

-- Whether every element of `items` matches `pattern`. Empty arrays match.
CREATE FUNCTION nest_all_match(items text[], pattern text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(bool_and(item ~ pattern), true) FROM unnest(items) AS item
$$;

-- Rejects an UPDATE that brings a revoked row back: revocation is final.
CREATE FUNCTION nest_revocation_is_final() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'revoked' AND NEW.status IS DISTINCT FROM 'revoked' THEN
        RAISE EXCEPTION '% revocation is final', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = TG_TABLE_NAME || '_revocation_final';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE service_principals (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    text NOT NULL CONSTRAINT service_principals_name_unique UNIQUE
                            CHECK (name ~ '^[a-z][a-z0-9_-]{0,62}$'),
    description             text CHECK (length(description) BETWEEN 1 AND 500),
    status                  text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    -- Quotas: unexpired, unrevoked service sessions at once, and exchanges in
    -- any rolling hour.
    max_active_sessions     integer NOT NULL DEFAULT 4 CHECK (max_active_sessions BETWEEN 1 AND 100),
    max_exchanges_per_hour  integer NOT NULL DEFAULT 60
                            CHECK (max_exchanges_per_hour BETWEEN 1 AND 10000),
    created_at              timestamptz NOT NULL DEFAULT now(),
    revoked_at              timestamptz,
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE TABLE workload_trust_policies (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_principal_id  uuid NOT NULL REFERENCES service_principals (id) ON DELETE RESTRICT,
    -- Only GitHub Actions is supported. Every value is an exact match: no
    -- wildcard, prefix or pattern is ever interpreted.
    issuer                text NOT NULL CHECK (issuer = 'https://token.actions.githubusercontent.com'),
    audience              text NOT NULL CHECK (audience ~ '^[\x21-\x7e]{1,255}$'),
    -- owner/name, and the numeric IDs GitHub never reuses: a repository deleted
    -- and recreated under the same name, or a transferred owner, does not match.
    repository            text NOT NULL
                          CHECK (repository ~ '^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$'),
    repository_id         text NOT NULL CHECK (repository_id ~ '^[0-9]{1,20}$'),
    repository_owner_id   text NOT NULL CHECK (repository_owner_id ~ '^[0-9]{1,20}$'),
    -- Branches and tags only: pull request refs are never trusted.
    refs                  text[] NOT NULL
                          CHECK (cardinality(refs) BETWEEN 1 AND 20
                                 AND nest_all_match(refs, '^refs/(heads|tags)/[^*?\[\]\s\\]{1,200}$')),
    -- Workflow files of the repository itself, matched against the token's
    -- workflow_ref ("<repository>/<workflow>@<ref>").
    workflows             text[] NOT NULL
                          CHECK (cardinality(workflows) BETWEEN 1 AND 20
                                 AND nest_all_match(workflows, '^\.github/workflows/[A-Za-z0-9_.-]{1,100}\.ya?ml$')),
    -- GitHub deployment environments. Empty: only tokens of jobs with no
    -- environment match. Otherwise the token's environment must be listed.
    environments          text[] NOT NULL DEFAULT '{}'
                          CHECK (cardinality(environments) <= 20
                                 AND nest_all_match(environments, '^[^*?\s]{1,255}$')),
    -- Events that may trigger a trusted run. Only events a fork cannot cause are
    -- allowed: pull_request_target, workflow_run, issue_comment and the other
    -- events a fork's pull request can trigger in the base repository's context,
    -- on its default branch ref, can never be trusted.
    event_names           text[] NOT NULL DEFAULT ARRAY['push']
                          CHECK (cardinality(event_names) BETWEEN 1 AND 4
                                 AND event_names <@ ARRAY['push', 'workflow_dispatch',
                                                          'schedule', 'release']::text[]),
    status                text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    revoked_at            timestamptz,
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE INDEX workload_trust_policies_principal ON workload_trust_policies (service_principal_id);

CREATE TABLE service_principal_grants (
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    service_principal_id  uuid NOT NULL REFERENCES service_principals (id) ON DELETE RESTRICT,
    project_id            uuid NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
    -- NULL: Project-level only. Platform-bound capabilities need a Platform.
    platform_id           uuid REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    capabilities          text[] NOT NULL
                          CHECK (cardinality(capabilities) >= 1
                                 AND capabilities <@ ARRAY['project.read', 'platform.read',
                                     'run.read', 'run.submit', 'run.cancel']::text[]),
    status                text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    revoked_at            timestamptz,
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
    CHECK (platform_id IS NOT NULL
           OR NOT (capabilities && ARRAY['platform.read', 'run.read', 'run.submit',
                                         'run.cancel']::text[]))
);

-- One active grant per principal, Project and Platform (or Project level).
CREATE UNIQUE INDEX service_principal_grants_active
    ON service_principal_grants (service_principal_id, project_id,
                                 COALESCE(platform_id, '00000000-0000-0000-0000-000000000000'))
    WHERE status = 'active';
CREATE INDEX service_principal_grants_project ON service_principal_grants (project_id);

-- Every workload token ever exchanged, by issuer and jti, so a replayed token is
-- refused. token_expires_at is when the exchange stops accepting the token: its
-- `exp` plus the verifier's clock skew. Rows may be pruned only after that, with
-- another 60 seconds of margin, and once the hour counted by the exchange quota
-- has passed.
CREATE TABLE workload_token_uses (
    issuer                text NOT NULL,
    jti                   text NOT NULL CHECK (jti ~ '^[\x21-\x7e]{1,255}$'),
    service_principal_id  uuid NOT NULL REFERENCES service_principals (id) ON DELETE RESTRICT,
    trust_policy_id       uuid NOT NULL REFERENCES workload_trust_policies (id) ON DELETE RESTRICT,
    token_expires_at      timestamptz NOT NULL,
    used_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (issuer, jti)
);

CREATE INDEX workload_token_uses_principal ON workload_token_uses (service_principal_id, used_at);

CREATE TABLE service_sessions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash            bytea NOT NULL CONSTRAINT service_sessions_token_unique UNIQUE
                          CHECK (length(token_hash) = 32),
    service_principal_id  uuid NOT NULL REFERENCES service_principals (id) ON DELETE RESTRICT,
    trust_policy_id       uuid NOT NULL REFERENCES workload_trust_policies (id) ON DELETE RESTRICT,
    -- Where the session came from: the workload token's issuer and jti, and its
    -- non-secret claims (repository, ref, sha, workflow_ref, environment, run_id,
    -- run_attempt, event_name, actor_id). Never the token.
    issuer                text NOT NULL,
    jti                   text NOT NULL,
    provenance            jsonb NOT NULL
                          CHECK (jsonb_typeof(provenance) = 'object'
                                 AND length(provenance::text) <= 4000),
    created_at            timestamptz NOT NULL DEFAULT now(),
    expires_at            timestamptz NOT NULL
                          CHECK (expires_at > created_at
                                 AND expires_at <= created_at + interval '1 hour'),
    revoked_at            timestamptz,
    revoked_reason        text CHECK (revoked_reason IN ('revoked', 'principal_revoked',
                                                         'policy_revoked', 'logout')),
    CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
);

CREATE INDEX service_sessions_principal ON service_sessions (service_principal_id, expires_at);
CREATE INDEX service_sessions_policy ON service_sessions (trust_policy_id);

-- Immutability: only revocation changes a principal, policy, grant or session.
CREATE TRIGGER service_principals_immutable BEFORE UPDATE ON service_principals
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'id', 'name', 'description', 'max_active_sessions', 'max_exchanges_per_hour',
        'created_at');
CREATE TRIGGER workload_trust_policies_immutable BEFORE UPDATE ON workload_trust_policies
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'id', 'service_principal_id', 'issuer', 'audience', 'repository', 'repository_id',
        'repository_owner_id', 'refs', 'workflows', 'environments', 'event_names',
        'created_at');
CREATE TRIGGER service_principal_grants_immutable BEFORE UPDATE ON service_principal_grants
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'id', 'service_principal_id', 'project_id', 'platform_id', 'capabilities', 'created_at');
CREATE TRIGGER service_sessions_immutable BEFORE UPDATE ON service_sessions
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'id', 'token_hash', 'service_principal_id', 'trust_policy_id', 'issuer', 'jti',
        'provenance', 'created_at', 'expires_at');
CREATE TRIGGER service_sessions_revocation_final BEFORE UPDATE ON service_sessions
    FOR EACH ROW WHEN (OLD.revoked_at IS NOT NULL)
    EXECUTE FUNCTION nest_forbid_column_change('revoked_at', 'revoked_reason');
CREATE TRIGGER workload_token_uses_immutable BEFORE UPDATE ON workload_token_uses
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();

CREATE TRIGGER service_principals_revocation_final BEFORE UPDATE ON service_principals
    FOR EACH ROW EXECUTE FUNCTION nest_revocation_is_final();
CREATE TRIGGER workload_trust_policies_revocation_final BEFORE UPDATE ON workload_trust_policies
    FOR EACH ROW EXECUTE FUNCTION nest_revocation_is_final();
CREATE TRIGGER service_principal_grants_revocation_final BEFORE UPDATE ON service_principal_grants
    FOR EACH ROW EXECUTE FUNCTION nest_revocation_is_final();

-- Never deleted: revocation keeps the history. Expired token uses may be pruned.
CREATE TRIGGER service_principals_no_delete BEFORE DELETE ON service_principals
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER workload_trust_policies_no_delete BEFORE DELETE ON workload_trust_policies
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER service_principal_grants_no_delete BEFORE DELETE ON service_principal_grants
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER service_sessions_no_delete BEFORE DELETE ON service_sessions
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER service_principals_no_truncate BEFORE TRUNCATE ON service_principals
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER workload_trust_policies_no_truncate BEFORE TRUNCATE ON workload_trust_policies
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER service_principal_grants_no_truncate BEFORE TRUNCATE ON service_principal_grants
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER service_sessions_no_truncate BEFORE TRUNCATE ON service_sessions
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER workload_token_uses_no_truncate BEFORE TRUNCATE ON workload_token_uses
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

CREATE FUNCTION nest_prune_workload_token_use() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.token_expires_at + interval '60 seconds' > now()
       OR OLD.used_at > now() - interval '1 hour' THEN
        RAISE EXCEPTION 'a workload token use is kept until the token has expired'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'workload_token_uses_replay_window';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER workload_token_uses_prune BEFORE DELETE ON workload_token_uses
    FOR EACH ROW EXECUTE FUNCTION nest_prune_workload_token_use();

-- Operator managed: principals, trust policies and grants are created and
-- revoked only by a named operator (nest-db), never by a User or a principal.
CREATE TRIGGER service_principals_operator_managed
    BEFORE INSERT OR UPDATE ON service_principals
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();
CREATE TRIGGER workload_trust_policies_operator_managed
    BEFORE INSERT OR UPDATE ON workload_trust_policies
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();
CREATE TRIGGER service_principal_grants_operator_managed
    BEFORE INSERT OR UPDATE ON service_principal_grants
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();

-- A grant names a live Project, and a Platform granted to it.
CREATE FUNCTION nest_check_service_grant() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id AND deleted_at IS NULL) THEN
        RAISE EXCEPTION 'a service principal grant must name a live Project'
            USING ERRCODE = 'foreign_key_violation',
                  CONSTRAINT = 'service_principal_grants_project_live';
    END IF;
    IF NEW.platform_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM hpc_platform_project_grants
        WHERE platform_id = NEW.platform_id AND project_id = NEW.project_id
    ) THEN
        RAISE EXCEPTION 'a service principal grant must name a Platform granted to its Project'
            USING ERRCODE = 'foreign_key_violation',
                  CONSTRAINT = 'service_principal_grants_platform_granted';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM service_principals WHERE id = NEW.service_principal_id AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'a grant needs an active service principal'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'service_principal_grants_principal_active';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER service_principal_grants_check BEFORE INSERT ON service_principal_grants
    FOR EACH ROW EXECUTE FUNCTION nest_check_service_grant();

CREATE FUNCTION nest_check_trust_policy() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM service_principals WHERE id = NEW.service_principal_id AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'a trust policy needs an active service principal'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'workload_trust_policies_principal_active';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workload_trust_policies_check BEFORE INSERT ON workload_trust_policies
    FOR EACH ROW EXECUTE FUNCTION nest_check_trust_policy();

-- Revoking a principal revokes its policies, grants and sessions; revoking a
-- policy revokes the sessions it issued. In the revoking transaction, so no
-- request sees a revoked principal with a live session.
CREATE FUNCTION nest_cascade_service_revocation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'revoked' AND OLD.status = 'active' THEN
        IF TG_TABLE_NAME = 'service_principals' THEN
            UPDATE service_sessions SET revoked_at = now(), revoked_reason = 'principal_revoked'
                WHERE service_principal_id = NEW.id AND revoked_at IS NULL;
            UPDATE workload_trust_policies SET status = 'revoked', revoked_at = now()
                WHERE service_principal_id = NEW.id AND status = 'active';
            UPDATE service_principal_grants SET status = 'revoked', revoked_at = now()
                WHERE service_principal_id = NEW.id AND status = 'active';
        ELSE
            UPDATE service_sessions SET revoked_at = now(), revoked_reason = 'policy_revoked'
                WHERE trust_policy_id = NEW.id AND revoked_at IS NULL;
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER service_principals_cascade AFTER UPDATE ON service_principals
    FOR EACH ROW EXECUTE FUNCTION nest_cascade_service_revocation();
CREATE TRIGGER workload_trust_policies_cascade AFTER UPDATE ON workload_trust_policies
    FOR EACH ROW EXECUTE FUNCTION nest_cascade_service_revocation();

-- The security audit log. The principal is the resource; principal IDs go in
-- details, never in actor_user_id, which only ever names a User.
CREATE FUNCTION nest_security_audit_service() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    kind text;
    principal uuid;
    project uuid;
    details jsonb := '{}';
    reason text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'service_principals' THEN
        kind := 'service_principal';
        principal := NEW.id;
        details := jsonb_build_object('name', NEW.name);
    ELSIF TG_TABLE_NAME = 'workload_trust_policies' THEN
        kind := 'workload_policy';
        principal := NEW.service_principal_id;
        details := jsonb_build_object(
            'repository_id', NEW.repository_id, 'refs', cardinality(NEW.refs),
            'workflows', cardinality(NEW.workflows), 'environments', cardinality(NEW.environments));
    ELSE
        kind := 'service_grant';
        principal := NEW.service_principal_id;
        project := NEW.project_id;
        details := jsonb_build_object(
            'platform_id', NEW.platform_id, 'capabilities', array_to_string(NEW.capabilities, ','));
    END IF;
    PERFORM nest_security_audit(
        kind || CASE WHEN TG_OP = 'INSERT' THEN '.created' ELSE '.revoked' END,
        p_project => project, p_resource_type => kind,
        p_resource_id => NEW.id::text,
        p_details => details || jsonb_build_object('service_principal_id', principal));
    RETURN NULL;
END;
$$;

CREATE TRIGGER service_principals_security_audit AFTER INSERT OR UPDATE ON service_principals
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_service();
CREATE TRIGGER workload_trust_policies_security_audit
    AFTER INSERT OR UPDATE ON workload_trust_policies
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_service();
CREATE TRIGGER service_principal_grants_security_audit
    AFTER INSERT OR UPDATE ON service_principal_grants
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_service();

-- Service sessions: each exchange, and every revocation.
CREATE FUNCTION nest_security_audit_service_session() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM nest_security_audit(
            'service_session.created', p_resource_type => 'service_session',
            p_resource_id => NEW.id::text, p_session => NEW.id,
            p_details => jsonb_build_object(
                'service_principal_id', NEW.service_principal_id,
                'trust_policy_id', NEW.trust_policy_id,
                'repository_id', NEW.provenance ->> 'repository_id',
                'run_id', NEW.provenance ->> 'run_id',
                'run_attempt', NEW.provenance ->> 'run_attempt'));
    ELSIF NEW.revoked_at IS NOT NULL AND OLD.revoked_at IS NULL THEN
        PERFORM nest_security_audit(
            'service_session.revoked', p_resource_type => 'service_session',
            p_resource_id => NEW.id::text, p_session => NEW.id, p_reason => NEW.revoked_reason,
            p_details => jsonb_build_object('service_principal_id', NEW.service_principal_id));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER service_sessions_security_audit AFTER INSERT OR UPDATE ON service_sessions
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_service_session();
