-- 0003: Nest Users, external identities, sessions, Organizations, Projects,
-- Project memberships, pending invitations and the Run submitter (B01).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- Identities are kept apart: a Nest User, an external sign-in identity, a Project
-- and a Provider Account are separate records with separate keys. Users and
-- Projects have generated UUID keys; nothing here references provider_profiles or
-- a Slurm account name.

-- Rejects an UPDATE that changes any of the columns named in the trigger's
-- arguments. Used for audit and attribution columns that must never be rewritten.
CREATE FUNCTION nest_forbid_column_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col text;
BEGIN
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF to_jsonb(OLD) -> col IS DISTINCT FROM to_jsonb(NEW) -> col THEN
            RAISE EXCEPTION '%.% is immutable', TG_TABLE_NAME, col
                USING ERRCODE = 'integrity_constraint_violation',
                      CONSTRAINT = TG_TABLE_NAME || '_' || col || '_immutable';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

-- Nest Users. A row exists only once a person has signed in (or an operator has
-- created the account); a pending invitation never creates one.
CREATE TABLE users (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name   text CHECK (length(display_name) BETWEEN 1 AND 200),
    -- Last email address reported by a verified identity. Informational only:
    -- users are identified by user_identities, never by email.
    email          text CHECK (length(email) BETWEEN 3 AND 320),
    status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    disabled_at    timestamptz,
    CHECK ((status = 'disabled') = (disabled_at IS NOT NULL))
);

-- External sign-in identities (Google or another OIDC provider) linked to a User.
-- An identity is the provider's stable subject ID under its issuer, never an
-- email address, so a changed or reused address cannot take over an account.
-- These identity providers are unrelated to Compute Providers.
CREATE TABLE user_identities (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- OIDC issuer, such as 'https://accounts.google.com'.
    issuer          text NOT NULL CHECK (length(issuer) BETWEEN 1 AND 500),
    -- OIDC 'sub' claim: stable and unique within the issuer.
    subject         text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    -- Claims seen at the last sign-in, for display and invitation matching. Only
    -- a current verified claim may redeem an email invitation.
    email           text CHECK (length(email) BETWEEN 3 AND 320),
    email_verified  boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_login_at   timestamptz,
    UNIQUE (issuer, subject)
);

CREATE INDEX user_identities_user ON user_identities (user_id);

CREATE TRIGGER user_identities_immutable BEFORE UPDATE ON user_identities
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('user_id', 'issuer', 'subject');

-- Signed-in sessions (browser or CLI). Only a hash of the session secret is
-- stored. A session is valid while it is neither revoked nor expired.
CREATE TABLE user_sessions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('browser', 'cli')),
    -- SHA-256 of the session secret.
    token_hash    bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    last_used_at  timestamptz,
    revoked_at    timestamptz,
    revoked_reason text CHECK (revoked_reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    CHECK (expires_at > created_at),
    CHECK (revoked_reason IS NULL OR revoked_at IS NOT NULL)
);

CREATE INDEX user_sessions_user ON user_sessions (user_id) WHERE revoked_at IS NULL;

-- Service-wide roles. 'platform_admin' manages the Nest service; it grants no
-- Project role and no Provider or HPC authority.
CREATE TABLE platform_role_assignments (
    user_id             uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role                text NOT NULL CHECK (role IN ('platform_admin')),
    granted_at          timestamptz NOT NULL DEFAULT now(),
    granted_by_user_id  uuid REFERENCES users (id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, role)
);

-- Optional grouping of Projects. An operator such as NCHC is not an Organization
-- key for accounts; Provider Accounts are scoped to their Provider.
CREATE TABLE organizations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    -- Audit only: who created the Organization.
    created_by_user_id  uuid REFERENCES users (id) ON DELETE RESTRICT,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE organization_memberships (
    organization_id  uuid NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    role             text NOT NULL CHECK (role IN ('org_admin', 'member')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);

CREATE INDEX organization_memberships_user ON organization_memberships (user_id);

-- Nest Projects: the collaboration and authorization boundary. The key is a
-- generated UUID, independent of the name, the Owner and any Provider Account.
CREATE TABLE projects (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid REFERENCES organizations (id) ON DELETE RESTRICT,
    name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    -- Audit only: who created the Project. The current Owner is the
    -- project_memberships row with role 'owner', which can change by transfer.
    created_by_user_id  uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    -- Set when the Owner deletes the Project. Rows are kept so Runs, memberships
    -- and audit history keep their Project.
    deleted_at          timestamptz,
    deleted_by_user_id  uuid REFERENCES users (id) ON DELETE RESTRICT,
    CHECK ((deleted_at IS NULL) = (deleted_by_user_id IS NULL))
);

CREATE TRIGGER projects_immutable BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('created_by_user_id', 'created_at');

-- Active Project memberships: many-to-many between users and projects, one row
-- per (project, user). Pending invitations live in their own tables.
CREATE TABLE project_memberships (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id        uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    user_id           uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    role              text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    -- Audit only: who added this member (NULL for the creator's own membership).
    added_by_user_id  uuid REFERENCES users (id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, user_id)
);

CREATE INDEX project_memberships_user ON project_memberships (user_id);

-- At most one Owner per Project, checked immediately and across concurrent
-- transactions. A transfer therefore demotes the old Owner before promoting the
-- new one.
CREATE UNIQUE INDEX project_memberships_one_owner
    ON project_memberships (project_id) WHERE role = 'owner';

CREATE TRIGGER project_memberships_immutable BEFORE UPDATE ON project_memberships
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change('project_id', 'user_id');

-- At least one Owner per Project, checked when the transaction commits, so a
-- Project and its first Owner are created together and a transfer can pass
-- through an intermediate state. Together with the unique index above, every
-- committed Project has exactly one Owner.
CREATE FUNCTION nest_check_project_owner() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    pid uuid;
    owners integer;
BEGIN
    IF TG_TABLE_NAME = 'projects' THEN
        pid := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        pid := OLD.project_id;
    ELSE
        pid := NEW.project_id;
    END IF;
    -- A Project removed in the same transaction has nothing left to check.
    IF NOT EXISTS (SELECT 1 FROM projects WHERE id = pid) THEN
        RETURN NULL;
    END IF;
    SELECT count(*) INTO owners FROM project_memberships
        WHERE project_id = pid AND role = 'owner';
    IF owners <> 1 THEN
        RAISE EXCEPTION 'Project % must have exactly one owner, found %', pid, owners
            USING ERRCODE = 'integrity_constraint_violation',
                  CONSTRAINT = 'projects_exactly_one_owner';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER projects_exactly_one_owner
    AFTER INSERT ON projects DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nest_check_project_owner();

CREATE CONSTRAINT TRIGGER project_memberships_exactly_one_owner
    AFTER INSERT OR UPDATE OR DELETE ON project_memberships DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nest_check_project_owner();

-- Pending email invitations, bound to one normalized address. The invitee needs
-- no users row: the address is plain data until a verified identity with the
-- same address redeems it. An invitation can never grant 'owner'. Delivery state
-- is not stored here: it lives in email_outbox (0002), whose subject_ref is
-- 'invitation:<id>'.
CREATE TABLE project_email_invitations (
    id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id                uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    invited_email_normalized  text NOT NULL CHECK (length(invited_email_normalized) BETWEEN 3 AND 320),
    role                      text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    invited_by_user_id        uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    status                    text NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'accepted', 'declined', 'revoked', 'expired')),
    -- SHA-256 of the recipient-bound link token, if one was issued.
    token_hash                bytea UNIQUE CHECK (length(token_hash) = 32),
    expires_at                timestamptz NOT NULL,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    accepted_at               timestamptz,
    accepted_by_user_id       uuid REFERENCES users (id) ON DELETE RESTRICT,
    declined_at               timestamptz,
    revoked_at                timestamptz,
    revoked_by_user_id        uuid REFERENCES users (id) ON DELETE RESTRICT,
    CHECK (expires_at > created_at),
    -- Each completion is a timestamp/actor pair set together, and only in the
    -- status it completes, so no partial or contradictory state can be stored.
    CHECK ((accepted_at IS NULL) = (accepted_by_user_id IS NULL)),
    CHECK ((revoked_at IS NULL) = (revoked_by_user_id IS NULL)),
    CHECK ((status = 'accepted') = (accepted_at IS NOT NULL)),
    CHECK ((status = 'declined') = (declined_at IS NOT NULL)),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
);

-- One open invitation per address and Project.
CREATE UNIQUE INDEX project_email_invitations_one_pending
    ON project_email_invitations (project_id, invited_email_normalized) WHERE status = 'pending';
-- Pending invitations for an address, looked up at verified sign-in.
CREATE INDEX project_email_invitations_email
    ON project_email_invitations (invited_email_normalized) WHERE status = 'pending';

CREATE TRIGGER project_email_invitations_immutable BEFORE UPDATE ON project_email_invitations
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'project_id', 'invited_email_normalized', 'role', 'invited_by_user_id');

-- Invite Links: bearer grants with an expiry and a bounded number of uses. A link
-- grant is separate from its per-user redemptions below. Never 'owner'.
CREATE TABLE project_invite_links (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id          uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    role                text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    created_by_user_id  uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    -- SHA-256 of the link token. The raw token is shown once and never stored.
    token_hash          bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),
    expires_at          timestamptz NOT NULL,
    max_uses            integer NOT NULL CHECK (max_uses BETWEEN 1 AND 10000),
    use_count           integer NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    revoked_at          timestamptz,
    revoked_by_user_id  uuid REFERENCES users (id) ON DELETE RESTRICT,
    CHECK (expires_at > created_at),
    -- Holds under concurrent redemption: an increment past max_uses fails.
    CHECK (use_count BETWEEN 0 AND max_uses),
    CHECK ((revoked_at IS NULL) = (revoked_by_user_id IS NULL))
);

CREATE INDEX project_invite_links_project ON project_invite_links (project_id);

CREATE TRIGGER project_invite_links_immutable BEFORE UPDATE ON project_invite_links
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'project_id', 'role', 'created_by_user_id', 'token_hash', 'max_uses');

-- One row per user who accepted an Invite Link; a user redeems a link once.
CREATE TABLE project_invitation_redemptions (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invite_link_id  bigint NOT NULL REFERENCES project_invite_links (id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    accepted_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (invite_link_id, user_id)
);

-- Run attribution. Each job may record the Project it ran in and the User who
-- submitted it. Both are written with the record and never change afterwards,
-- whatever happens to the Project's ownership or the submitter's membership.
-- Both are NULL for records written before authentication existed.
ALTER TABLE jobs
    ADD COLUMN project_id uuid REFERENCES projects (id) ON DELETE RESTRICT,
    ADD COLUMN submitted_by_user_id uuid REFERENCES users (id) ON DELETE RESTRICT,
    ADD CONSTRAINT jobs_project_has_submitter
        CHECK ((project_id IS NULL) = (submitted_by_user_id IS NULL));

CREATE TRIGGER jobs_attribution_immutable BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION nest_forbid_column_change(
        'execution_id', 'project_id', 'submitted_by_user_id');
