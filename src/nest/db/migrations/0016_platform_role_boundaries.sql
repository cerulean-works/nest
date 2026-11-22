-- 0016: Platform-role boundaries (M1 patch B01).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- Role scopes stay in separate tables with separate CHECKed vocabularies (0003):
--   platform_role_assignments  'platform_admin'                    the Nest service
--   organization_memberships   'org_admin', 'member'               one Organization
--   project_memberships        'owner', 'admin', 'member', 'viewer' one Project
-- None of them references an HPC Platform, a Slurm account or a login node, and
-- none grants a Platform (0012), a Slurm account (0013) or a UNIX identity.
--
-- This migration adds one lifecycle rule: a platform role can only be granted to an
-- active User. A disabled User's existing assignment is kept, so re-enabling them
-- restores exactly what was granted and nothing else, but it is not effective
-- while they are disabled (nest.db.IdentityStore.platform_roles), and a disabled
-- User cannot be granted a new one. Rows already stored are not touched.

CREATE FUNCTION nest_check_platform_role_grantee() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    grantee_status text;
BEGIN
    -- FOR SHARE conflicts with set_user_status's UPDATE, so a grant and a
    -- concurrent disable serialize: the grant sees the committed status.
    SELECT status INTO grantee_status FROM users WHERE id = NEW.user_id FOR SHARE;
    -- A missing User is left to the foreign key.
    IF FOUND AND grantee_status <> 'active' THEN
        RAISE EXCEPTION 'a platform role can only be granted to an active User'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'platform_role_assignments_active_user';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER platform_role_assignments_active_user
    BEFORE INSERT ON platform_role_assignments
    FOR EACH ROW EXECUTE FUNCTION nest_check_platform_role_grantee();
