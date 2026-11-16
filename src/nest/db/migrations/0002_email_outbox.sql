-- 0002: Persistent outbox for optional email delivery (A07).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- Business code (such as Project invitations) inserts a row in the same
-- transaction as the record the message is about, so a message is queued if and
-- only if that record commits. The Controller's delivery worker then sends it over
-- SMTP. Delivery only ever updates this table: a failed, retried or repeated send
-- never changes an invitation or a membership.

CREATE TABLE email_outbox (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Caller-chosen idempotency key, such as 'invitation:42:send:1'. Queuing the
    -- same key again returns the existing row instead of a second message.
    message_key      text NOT NULL UNIQUE CHECK (message_key ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    -- Template name, such as 'project_invitation'.
    kind             text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]{0,63}$'),
    -- Opaque reference to the record the message is about, such as
    -- 'invitation:42', so its owner can look up delivery state. Never an address.
    subject_ref      text CHECK (subject_ref ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    -- Private: read only by the delivery worker. Delivery-status queries never
    -- return the recipient or the message.
    recipient        text NOT NULL CHECK (length(recipient) BETWEEN 3 AND 320),
    subject          text NOT NULL CHECK (length(subject) BETWEEN 1 AND 998),
    -- Rendered body. It can hold an invitation link, so it is cleared once the
    -- message is sent.
    body_text        text,
    -- Stable RFC 5322 Message-ID, reused by every attempt so a resend after an
    -- unconfirmed attempt is recognizable as the same message.
    message_id       text NOT NULL UNIQUE,
    -- 'pending': waiting for its next attempt. 'sending': claimed by a worker
    -- until locked_until. 'sent': accepted by the SMTP server. 'failed': rejected
    -- permanently or out of attempts; it can be queued again explicitly.
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    locked_until     timestamptz,
    -- Random token of the current claim. A worker records its result only while
    -- the row still carries its token, so a worker whose lease expired cannot
    -- overwrite the outcome of the worker that claimed the message after it.
    claim_token      uuid,
    -- Last failure, without addresses or server text: an error class and SMTP
    -- reply code, such as 'SMTPConnectError code=421'.
    last_error       text,
    -- 'transient' failures are retried; 'permanent' ones are not.
    last_error_kind  text CHECK (last_error_kind IN ('transient', 'permanent')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    sent_at          timestamptz,
    CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
    CHECK ((status = 'sending') = (claim_token IS NOT NULL AND locked_until IS NOT NULL)),
    CHECK (status = 'sent' OR body_text IS NOT NULL)
);

-- The worker's scan: due messages, oldest first.
CREATE INDEX email_outbox_due ON email_outbox (next_attempt_at) WHERE status = 'pending';
-- Claims whose worker stopped before recording a result.
CREATE INDEX email_outbox_stale ON email_outbox (locked_until) WHERE status = 'sending';
CREATE INDEX email_outbox_subject ON email_outbox (subject_ref) WHERE subject_ref IS NOT NULL;
