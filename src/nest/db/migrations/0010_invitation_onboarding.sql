-- 0010: audit events for Invite Link exhaustion and invitation delivery
-- failures (B08).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- The invitation tables themselves (0003) and the audit of their creation,
-- acceptance, decline, expiry, revocation, reissued tokens, queued emails and
-- Invite Link redemptions (0009) are unchanged. This adds the two events those
-- triggers cannot see: an Invite Link reaching its last use, and an invitation
-- email the outbox gave up on. Neither records an address, token or body.

-- An Invite Link whose use count reaches its limit.
CREATE FUNCTION nest_security_audit_invite_link_exhausted() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM nest_security_audit(
        'invite_link.exhausted', p_project => NEW.project_id,
        p_resource_type => 'invite_link', p_resource_id => NEW.id::text,
        p_details => jsonb_build_object('role', NEW.role, 'max_uses', NEW.max_uses));
    RETURN NULL;
END;
$$;

CREATE TRIGGER project_invite_links_exhausted_security_audit AFTER UPDATE ON project_invite_links
    FOR EACH ROW WHEN (NEW.use_count >= NEW.max_uses AND OLD.use_count < OLD.max_uses)
    EXECUTE FUNCTION nest_security_audit_invite_link_exhausted();

-- An invitation email marked failed: rejected permanently or out of attempts.
-- Recorded with outcome 'failed' and the stored error class and SMTP code, which
-- never hold an address or the server's reply text (see 0002).
CREATE FUNCTION nest_security_audit_invitation_email_failed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    invitation bigint;
    project uuid;
BEGIN
    IF NEW.subject_ref ~ '^invitation:[0-9]{1,18}$' THEN
        invitation := split_part(NEW.subject_ref, ':', 2)::bigint;
        SELECT project_id INTO project FROM project_email_invitations WHERE id = invitation;
        INSERT INTO security_audit_events
            (event, outcome, interface, actor_operator, project_id, resource_type,
             resource_id, reason, note, details)
        VALUES (
            'invitation.email_failed', 'failed', 'database',
            nest_audit_setting('nest.audit_operator'), project, 'invitation', invitation::text,
            NEW.last_error_kind, nest_audit_setting('nest.audit_reason'),
            jsonb_strip_nulls(jsonb_build_object(
                'message_key', NEW.message_key, 'kind', NEW.kind,
                'attempts', NEW.attempts, 'error', left(NEW.last_error, 200))));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER email_outbox_invitation_failed_security_audit AFTER UPDATE ON email_outbox
    FOR EACH ROW WHEN (NEW.status = 'failed' AND OLD.status IS DISTINCT FROM 'failed')
    EXECUTE FUNCTION nest_security_audit_invitation_email_failed();
