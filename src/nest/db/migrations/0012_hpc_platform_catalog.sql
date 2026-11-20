-- 0012: HPC Platform Catalog and Login-Node Registry (D07).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- An HPC Platform is one Slurm job and account namespace, such as `nano4` or
-- `f1`. It is keyed by a generated, immutable UUID and named by a unique,
-- immutable slug. Who operates it is optional metadata only: two Platforms run by
-- the same operator (such as NCHC) are separate Platforms with separate IDs, and
-- there is no Provider parent above them. Slurm accounts are not part of a
-- Platform's identity (the account registry is D08).
--
-- Each Platform has one or more approved login nodes. They all reach the same
-- namespace, so moving from one to another (failover) never changes the Platform.
-- Endpoints, transport modes, host-key pin references and Project grants are
-- operator managed: every change to them must name an operator through the audit
-- settings of 0005 (`nest-db platform`, or the Controller's environment sync),
-- and is recorded in security_audit_events (0009). Only the health columns of a
-- login node may change without one, because the Controller records them.

-- Refuses a change to operator-managed rows unless the transaction names an
-- operator (set_config('nest.audit_operator', ...)). On UPDATE, the columns named
-- in the trigger's arguments may change without one.
CREATE FUNCTION nest_require_operator() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    changed text;
BEGIN
    IF nest_audit_setting('nest.audit_operator') IS NOT NULL THEN
        RETURN COALESCE(NEW, OLD);
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

CREATE TABLE hpc_platforms (
    -- Immutable Platform ID. Records that belong to a Slurm namespace, such as
    -- jobs, refer to this, never to the slug or the operator.
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Unique user-facing name, such as 'nano4' or 'f1'. Immutable and never
    -- reused: Platforms are disabled, not deleted.
    slug          text NOT NULL CONSTRAINT hpc_platforms_slug_unique UNIQUE CHECK (slug ~ '^[a-z][a-z0-9_-]{0,31}$'),
    display_name  text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    -- Optional metadata, such as 'NCHC'. Not a key and not unique.
    operator_name text CHECK (length(operator_name) BETWEEN 1 AND 200),
    scheduler     text NOT NULL DEFAULT 'slurm' CHECK (scheduler IN ('slurm')),
    -- 'operator': managed with `nest-db platform`. 'env': mirrored from a Provider
    -- Profile in NEST_PROVIDERS / NEST_SLURM_* at every Controller start.
    source        text NOT NULL DEFAULT 'operator' CHECK (source IN ('operator', 'env')),
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    -- The server's default Platform, used when a caller names none and is
    -- authorized for it.
    is_default    boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    disabled_at   timestamptz,
    CHECK ((status = 'disabled') = (disabled_at IS NOT NULL)),
    CHECK (NOT (is_default AND status = 'disabled'))
);

CREATE UNIQUE INDEX hpc_platforms_one_default ON hpc_platforms (is_default) WHERE is_default;

CREATE TRIGGER hpc_platforms_immutable BEFORE UPDATE ON hpc_platforms
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('id', 'slug', 'created_at');
-- A Platform's ID and slug are never reused, so its rows are never removed.
CREATE TRIGGER hpc_platforms_no_delete BEFORE DELETE ON hpc_platforms
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER hpc_platforms_no_truncate BEFORE TRUNCATE ON hpc_platforms
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER hpc_platforms_operator_managed BEFORE INSERT OR UPDATE ON hpc_platforms
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();

-- Approved login nodes of a Platform. Every node of a Platform reaches the same
-- Slurm namespace.
CREATE TABLE hpc_login_nodes (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform_id        uuid NOT NULL REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    host               text NOT NULL
                       CHECK (host ~ '^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'),
    -- NULL uses the transport's default: SSH's own in Mode A (where the host may
    -- be an alias of the operator's SSH configuration), 22 in Mode B.
    port               integer CHECK (port BETWEEN 1 AND 65535),
    -- Approved transport modes: 'A' (trusted operator SSH) and/or 'B' (managed
    -- SSH, which uses no SSH configuration file and no Tailscale).
    transport_modes    text[] NOT NULL
                       CHECK (cardinality(transport_modes) BETWEEN 1 AND 2
                              AND transport_modes <@ ARRAY['A', 'B']::text[]
                              AND array_position(transport_modes, NULL) IS NULL),
    -- Lower is tried first among nodes of the same health.
    priority           integer NOT NULL DEFAULT 100 CHECK (priority BETWEEN 0 AND 10000),
    -- Operator approval. A disabled node is never selected.
    status             text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    -- Reference to the operator-vetted SSH host key, such as an OpenSSH
    -- 'SHA256:...' fingerprint or the name of a known_hosts entry. Required for
    -- Mode B. A reference only: no key material and no credential.
    host_key_pin_ref   text CHECK (host_key_pin_ref ~ '^[A-Za-z0-9_.:/+=@-]{1,200}$'),
    -- Last health the Controller observed. 'unhealthy' nodes are skipped while
    -- another approved node remains.
    health             text NOT NULL DEFAULT 'unknown'
                       CHECK (health IN ('unknown', 'healthy', 'degraded', 'unhealthy')),
    health_checked_at  timestamptz,
    health_reason      text CHECK (health_reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT hpc_login_nodes_endpoint_unique UNIQUE NULLS NOT DISTINCT (platform_id, host, port),
    CONSTRAINT hpc_login_nodes_mode_b_pin
        CHECK (NOT ('B' = ANY (transport_modes)) OR host_key_pin_ref IS NOT NULL)
);

CREATE INDEX hpc_login_nodes_platform ON hpc_login_nodes (platform_id, priority, id);

CREATE TRIGGER hpc_login_nodes_immutable BEFORE UPDATE ON hpc_login_nodes
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('id', 'platform_id', 'created_at');
CREATE TRIGGER hpc_login_nodes_operator_managed BEFORE INSERT OR UPDATE OR DELETE ON hpc_login_nodes
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator(
        'health', 'health_checked_at', 'health_reason', 'updated_at');
CREATE TRIGGER hpc_login_nodes_no_truncate BEFORE TRUNCATE ON hpc_login_nodes
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- Which Projects may use a Platform. A User discovers a Platform only through an
-- active membership in a live Project granted on it. A grant is not a Slurm
-- account and grants no Project role.
CREATE TABLE hpc_platform_project_grants (
    platform_id  uuid NOT NULL REFERENCES hpc_platforms (id) ON DELETE RESTRICT,
    project_id   uuid NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
    granted_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform_id, project_id)
);

CREATE INDEX hpc_platform_project_grants_project ON hpc_platform_project_grants (project_id);

CREATE TRIGGER hpc_platform_project_grants_immutable BEFORE UPDATE ON hpc_platform_project_grants
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'platform_id', 'project_id', 'granted_at');
CREATE TRIGGER hpc_platform_project_grants_operator_managed
    BEFORE INSERT OR UPDATE OR DELETE ON hpc_platform_project_grants
    FOR EACH ROW EXECUTE FUNCTION nest_require_operator();
CREATE TRIGGER hpc_platform_project_grants_no_truncate BEFORE TRUNCATE ON hpc_platform_project_grants
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

-- A job records the Platform (Slurm namespace) it was submitted to, whichever
-- login node was used. NULL for records written before 0012 or without a
-- Platform. Written with the record and never changed.
ALTER TABLE jobs ADD COLUMN platform_id uuid REFERENCES hpc_platforms (id) ON DELETE RESTRICT;

CREATE INDEX jobs_platform_slurm_job ON jobs (platform_id, slurm_job_id)
    WHERE platform_id IS NOT NULL;

CREATE TRIGGER jobs_platform_immutable BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('platform_id');

-- Security audit (0009) of every catalog change. Only names of changed columns
-- and non-secret attributes are recorded; the tables hold no credentials.
CREATE FUNCTION nest_security_audit_platform() RETURNS trigger
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
            'platform.changed', p_resource_type => 'platform', p_resource_id => NEW.id::text,
            p_details => jsonb_build_object(
                'changed', changed, 'slug', NEW.slug, 'source', NEW.source,
                'status', NEW.status, 'is_default', NEW.is_default));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER hpc_platforms_security_audit AFTER INSERT OR UPDATE ON hpc_platforms
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_platform();

-- Login nodes: approval changes only. Health observations are not audited.
CREATE FUNCTION nest_security_audit_login_node() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    node hpc_login_nodes;
    changed text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        node := OLD;
        changed := 'removed';
    ELSIF TG_OP = 'INSERT' THEN
        node := NEW;
        changed := 'created';
    ELSE
        node := NEW;
        SELECT string_agg(key, ',' ORDER BY key) INTO changed
        FROM jsonb_each(to_jsonb(NEW)) n
        WHERE key NOT IN ('updated_at', 'created_at', 'health', 'health_checked_at',
                          'health_reason')
          AND n.value IS DISTINCT FROM to_jsonb(OLD) -> key;
    END IF;
    IF changed IS NOT NULL THEN
        PERFORM nest_security_audit(
            'platform.login_node.changed', p_resource_type => 'login_node',
            p_resource_id => node.id::text,
            p_details => jsonb_build_object(
                'changed', changed, 'platform_id', node.platform_id, 'host', node.host,
                'port', node.port, 'transport_modes', array_to_string(node.transport_modes, ','),
                'status', node.status, 'priority', node.priority,
                'host_key_pin_ref', node.host_key_pin_ref));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER hpc_login_nodes_security_audit AFTER INSERT OR UPDATE OR DELETE ON hpc_login_nodes
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_login_node();

CREATE FUNCTION nest_security_audit_platform_grant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    grant_row hpc_platform_project_grants;
BEGIN
    grant_row := COALESCE(NEW, OLD);
    PERFORM nest_security_audit(
        CASE TG_OP WHEN 'INSERT' THEN 'platform.grant.added' ELSE 'platform.grant.removed' END,
        p_project => grant_row.project_id, p_resource_type => 'platform',
        p_resource_id => grant_row.platform_id::text);
    RETURN NULL;
END;
$$;

CREATE TRIGGER hpc_platform_project_grants_security_audit
    AFTER INSERT OR DELETE ON hpc_platform_project_grants
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_platform_grant();
