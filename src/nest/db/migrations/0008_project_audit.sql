-- 0008: immutable audit history of Project and membership changes (B06).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- Every change to a Project's name or deletion and to its memberships writes one
-- row here, and Projects can only be soft-deleted, from triggers, whoever made the change: the authorization layer, an
-- invitation flow or an operator with database credentials. Rows are never
-- updated or deleted. IDs are not foreign keys, so the history outlives the
-- Users and Projects it names. No email address is stored here.

CREATE TABLE project_audit_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    project_id      uuid NOT NULL,
    event           text NOT NULL CHECK (event IN (
                        'project_created',
                        'project_renamed',
                        'project_deleted',
                        'member_added',
                        'member_role_changed',
                        'member_removed')),
    -- The member the event is about, for membership events.
    target_user_id  uuid,
    old_role        text CHECK (old_role IN ('owner', 'admin', 'member', 'viewer')),
    new_role        text CHECK (new_role IN ('owner', 'admin', 'member', 'viewer')),
    -- The Nest User who made the change, when a signed-in User did.
    actor_user_id   uuid,
    -- The operator name, when an operator did (see 0005's audit settings).
    actor_operator  text CHECK (length(actor_operator) BETWEEN 1 AND 200),
    database_role   text NOT NULL DEFAULT session_user,
    reason          text CHECK (length(reason) BETWEEN 1 AND 1000)
);

CREATE INDEX project_audit_events_project ON project_audit_events (project_id, id);

CREATE TRIGGER project_audit_events_append_only BEFORE UPDATE OR DELETE ON project_audit_events
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_change();
CREATE TRIGGER project_audit_events_no_truncate BEFORE TRUNCATE ON project_audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_change();

CREATE FUNCTION nest_audit_project() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    actor uuid := nest_audit_setting('nest.audit_actor_user_id')::uuid;
    operator text := nest_audit_setting('nest.audit_operator');
    why text := nest_audit_setting('nest.audit_reason');
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO project_audit_events (project_id, event, actor_user_id, actor_operator, reason)
        VALUES (NEW.id, 'project_created', COALESCE(actor, NEW.created_by_user_id), operator, why);
        RETURN NEW;
    END IF;
    IF NEW.name IS DISTINCT FROM OLD.name THEN
        INSERT INTO project_audit_events (project_id, event, actor_user_id, actor_operator, reason)
        VALUES (NEW.id, 'project_renamed', actor, operator, why);
    END IF;
    IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
        INSERT INTO project_audit_events (project_id, event, actor_user_id, actor_operator, reason)
        VALUES (NEW.id, 'project_deleted', COALESCE(actor, NEW.deleted_by_user_id), operator, why);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER projects_audit AFTER INSERT OR UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION nest_audit_project();

-- A deleted Project stays deleted: its row is kept for history and nothing may
-- restore it.
CREATE FUNCTION nest_project_stays_deleted() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
        RAISE EXCEPTION 'A deleted Project cannot be restored'
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'projects_stays_deleted';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER projects_stays_deleted BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION nest_project_stays_deleted();

-- Projects are only ever soft-deleted (deleted_at), which the audit trigger above
-- records. A hard DELETE or TRUNCATE would remove the row and cascade away its
-- memberships without a project_deleted event, so both are refused for every
-- writer, including operators with database credentials.
CREATE FUNCTION nest_forbid_project_hard_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Projects cannot be hard-deleted; set deleted_at instead'
        USING ERRCODE = 'integrity_constraint_violation',
              CONSTRAINT = 'projects_soft_delete_only';
END;
$$;

CREATE TRIGGER projects_soft_delete_only BEFORE DELETE ON projects
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_project_hard_delete();
CREATE TRIGGER projects_no_truncate BEFORE TRUNCATE ON projects
    FOR EACH STATEMENT EXECUTE FUNCTION nest_forbid_project_hard_delete();

CREATE FUNCTION nest_audit_project_membership() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    actor uuid := nest_audit_setting('nest.audit_actor_user_id')::uuid;
    operator text := nest_audit_setting('nest.audit_operator');
    why text := nest_audit_setting('nest.audit_reason');
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO project_audit_events
            (project_id, event, target_user_id, new_role, actor_user_id, actor_operator, reason)
        VALUES (NEW.project_id, 'member_added', NEW.user_id, NEW.role,
                COALESCE(actor, NEW.added_by_user_id,
                         CASE WHEN NEW.role = 'owner' THEN NEW.user_id END),
                operator, why);
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.role IS DISTINCT FROM OLD.role THEN
            INSERT INTO project_audit_events
                (project_id, event, target_user_id, old_role, new_role,
                 actor_user_id, actor_operator, reason)
            VALUES (NEW.project_id, 'member_role_changed', NEW.user_id, OLD.role, NEW.role,
                    actor, operator, why);
        END IF;
        RETURN NEW;
    END IF;
    INSERT INTO project_audit_events
        (project_id, event, target_user_id, old_role, actor_user_id, actor_operator, reason)
    VALUES (OLD.project_id, 'member_removed', OLD.user_id, OLD.role, actor, operator, why);
    RETURN OLD;
END;
$$;

CREATE TRIGGER project_memberships_audit AFTER INSERT OR UPDATE OR DELETE ON project_memberships
    FOR EACH ROW EXECUTE FUNCTION nest_audit_project_membership();
