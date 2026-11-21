-- 0015: Project-to-Platform eligibility, Project default Platforms, and
-- Organization tenancy (Patch B06).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- Organization and Project IDs are generated, immutable tenant keys,
-- independent of names, Owners, Platforms and Slurm accounts. A Project's
-- eligible Platforms are exactly its operator-managed grants (0012). A Project's
-- Owners and Admins may choose one of them as the Project's default Platform;
-- the choice can never point outside the grants, and revoking the grant clears
-- it in the same transaction. Nothing here grants a Platform, a Slurm account or
-- any HPC authority.

-- Immutable tenant identities ------------------------------------------------

CREATE TRIGGER projects_id_immutable BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('id');

CREATE TRIGGER organizations_immutable BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('id', 'created_by_user_id', 'created_at');

CREATE TRIGGER organization_memberships_immutable BEFORE UPDATE ON organization_memberships
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('organization_id', 'user_id');

-- Security audit (0009) of Organization changes. Names only; no email address.
CREATE FUNCTION nest_security_audit_organization() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM nest_security_audit(
            'organization.created', p_actor => NEW.created_by_user_id,
            p_resource_type => 'organization', p_resource_id => NEW.id::text);
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM nest_security_audit(
            'organization.deleted', p_resource_type => 'organization',
            p_resource_id => OLD.id::text);
    ELSIF NEW.name IS DISTINCT FROM OLD.name THEN
        PERFORM nest_security_audit(
            'organization.renamed', p_resource_type => 'organization',
            p_resource_id => NEW.id::text);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER organizations_security_audit AFTER INSERT OR UPDATE OR DELETE ON organizations
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_organization();

CREATE FUNCTION nest_security_audit_organization_membership() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM nest_security_audit(
            'organization.member.added', p_target => NEW.user_id,
            p_resource_type => 'organization', p_resource_id => NEW.organization_id::text,
            p_details => jsonb_build_object('new_role', NEW.role));
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM nest_security_audit(
            'organization.member.removed', p_target => OLD.user_id,
            p_resource_type => 'organization', p_resource_id => OLD.organization_id::text,
            p_details => jsonb_build_object('old_role', OLD.role));
    ELSIF NEW.role IS DISTINCT FROM OLD.role THEN
        PERFORM nest_security_audit(
            'organization.member.role_changed', p_target => NEW.user_id,
            p_resource_type => 'organization', p_resource_id => NEW.organization_id::text,
            p_details => jsonb_build_object('old_role', OLD.role, 'new_role', NEW.role));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER organization_memberships_security_audit
    AFTER INSERT OR UPDATE OR DELETE ON organization_memberships
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_organization_membership();

-- Project default Platforms --------------------------------------------------

-- At most one default Platform per Project, always one of the Project's grants.
-- The foreign key to the grant makes a default outside the Project's eligible
-- Platforms impossible for every writer, and removes the default when the
-- operator revokes the grant. A disabled Platform keeps its grant and so the
-- row, but is never resolved (see "Project default Platform" in the docs).
CREATE TABLE project_default_platforms (
    project_id      uuid PRIMARY KEY,
    platform_id     uuid NOT NULL,
    -- Audit only: the Owner or Admin who chose it.
    set_by_user_id  uuid REFERENCES users (id) ON DELETE SET NULL,
    set_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT project_default_platforms_granted
        FOREIGN KEY (platform_id, project_id)
        REFERENCES hpc_platform_project_grants (platform_id, project_id) ON DELETE CASCADE
);

CREATE INDEX project_default_platforms_platform ON project_default_platforms (platform_id);

CREATE TRIGGER project_default_platforms_immutable BEFORE UPDATE ON project_default_platforms
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('project_id');
CREATE TRIGGER project_default_platforms_no_truncate BEFORE TRUNCATE ON project_default_platforms
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

CREATE FUNCTION nest_security_audit_project_default_platform() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM nest_security_audit(
            'project.default_platform.cleared', p_project => OLD.project_id,
            p_resource_type => 'platform', p_resource_id => OLD.platform_id::text);
    ELSIF TG_OP = 'INSERT' OR NEW.platform_id IS DISTINCT FROM OLD.platform_id THEN
        PERFORM nest_security_audit(
            'project.default_platform.set', p_actor => NEW.set_by_user_id,
            p_project => NEW.project_id, p_resource_type => 'platform',
            p_resource_id => NEW.platform_id::text);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER project_default_platforms_security_audit
    AFTER INSERT OR UPDATE OR DELETE ON project_default_platforms
    FOR EACH ROW EXECUTE FUNCTION nest_security_audit_project_default_platform();
