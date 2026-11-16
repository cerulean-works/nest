# Send email with SMTP

Nest can send email, such as Project invitations, through an SMTP server you
operate or rent. Email is **optional**:

- With `NEST_EMAIL_ENABLED` unset or `false` (the default), the Controller reads no
  SMTP setting, starts no delivery worker, and never opens an SMTP connection.
  Nothing else depends on email: invitations stay usable by sharing their
  recipient-bound link, or a generic Invite Link, manually.
- With `NEST_EMAIL_ENABLED=true`, the Controller validates the sender, the public
  URL and the SMTP transport before it starts, and refuses to start (exit status 2)
  when a required setting is missing or would make delivery insecure. It does not
  contact the SMTP server at startup, so a mail outage never stops the Controller.

This page covers the transport and the delivery outbox. Creating, resending and
redeeming Project invitations is a separate feature that builds on it and is not
part of this release.

## Enable email

Email needs the Controller database ([Store Controller records in PostgreSQL](database)),
because queued messages and their delivery status are kept in it. Apply the
`0002_email_outbox` migration with `nest-db upgrade` before enabling email.

```bash
NEST_EMAIL_ENABLED=true
NEST_EMAIL_PROVIDER=smtp
NEST_EMAIL_FROM="Nest <nest@example.com>"
NEST_PUBLIC_URL=https://nest.example.com
NEST_SMTP_HOST=smtp.example.com
NEST_SMTP_PORT=587
NEST_SMTP_USERNAME=nest@example.com
NEST_SMTP_PASSWORD_FILE=/run/secrets/nest_smtp_password
NEST_SMTP_STARTTLS=true
```

At startup the Controller logs one line without the password:

```text
INFO nest.mail ... Email delivery enabled provider=smtp host=smtp.example.com port=587 tls=starttls from=nest@example.com
```

All settings are listed in [Environment variables and defaults](configuration#email-delivery).

### Sender and links

- `NEST_EMAIL_FROM` is the `From` header and the SMTP envelope sender of every
  message. Use an address the SMTP account is allowed to send as, on a domain you
  control. Nest never takes a sender from a request.
- `NEST_PUBLIC_URL` is the base of every link in a message. It must use `https://`
  (plain `http://` is accepted only for `localhost`), because invitation links
  carry tokens. Invitation links have the form
  `https://nest.example.com/invitations/accept#token=...`: the token is in the URL
  fragment, which browsers never send to a server, so it stays out of proxy and
  access logs.

### Deliverability

Receivers reject or junk mail whose domain does not authenticate it. For the
domain in `NEST_EMAIL_FROM`:

- **SPF**: publish a TXT record that authorizes your SMTP provider's servers.
- **DKIM**: enable signing at your SMTP provider and publish its public key.
- **DMARC**: publish a `_dmarc` policy, starting with `p=none` and a report
  address, then tighten it to `quarantine` or `reject` once reports are clean.

Use a dedicated sender such as `nest@` or `no-reply@`, and route bounces to a
mailbox someone reads.

## TLS

Nest never sends mail, or a password, over an unencrypted connection.

| Mode | Settings | Behavior |
| --- | --- | --- |
| STARTTLS (default) | `NEST_SMTP_STARTTLS=true`, port `587` | Connects in plaintext, then upgrades with STARTTLS. If the server does not offer STARTTLS, the attempt fails with `STARTTLSNotOffered`; Nest never continues unencrypted. |
| Implicit TLS | `NEST_SMTP_IMPLICIT_TLS=true`, port `465` | TLS from the first byte ("SMTPS"). |

`NEST_SMTP_STARTTLS=false` without `NEST_SMTP_IMPLICIT_TLS=true` is a configuration
error, and so is enabling both. In both modes Nest requires TLS 1.2 or newer,
verifies the server certificate against the system roots (or `NEST_SMTP_CA_FILE`,
for a relay with a private CA) and checks that it is valid for `NEST_SMTP_HOST`.
There is no setting to disable verification. Credentials are sent only after the
handshake succeeds.

## Credentials and rotation

Prefer `NEST_SMTP_PASSWORD_FILE` to `NEST_SMTP_PASSWORD`. Mount the file from your
platform's secret manager, such as a Docker or Compose secret, a Kubernetes
Secret, or a Vault agent template; set only one of the two. A password is never
written to a log, an error, the configuration summary, or the outbox.

The file is read when the Controller starts, to check it is not empty, and again
for every SMTP connection. To rotate the credential:

1. Create the new password or API key at your SMTP provider, keeping the old one.
2. Replace the secret file's contents. The next delivery attempt uses it; no
   restart is needed.
3. Revoke the old credential once the log shows `Email sent` lines again.

An inline `NEST_SMTP_PASSWORD` is read only at startup, so changing it needs a
restart. It is also visible to anyone who can inspect the process environment.

## Delivery outbox

Messages go through the `email_outbox` table rather than straight to SMTP:

1. Business code, such as invitation creation, queues a message in the **same
   transaction** as its own record. The message exists if and only if that record
   commits, and a failed send can never roll the record back.
2. The Controller's delivery worker scans the outbox every
   `NEST_EMAIL_POLL_INTERVAL` seconds (and immediately after a message is queued),
   claims due messages, and sends each over SMTP. Several Controllers can share
   one database: a message is claimed by one worker at a time.
3. The worker records the result in the outbox row, and only there. Delivery never
   changes an invitation or a membership, so an invitation whose email failed stays
   `PENDING`, and its link can still be copied and shared manually.

Each queued message has a caller-chosen **message key** (such as
`invitation:42:send:1`). Queuing the same key again returns the existing message
instead of a second one, so a retried request never sends twice.

### Statuses and retries

| Status | Meaning |
| --- | --- |
| `pending` | Waiting for its next attempt at `next_attempt_at`. |
| `sending` | Claimed by a worker, with a random claim token. A claim older than its lease is assumed interrupted and claimed again with a new token. A worker records its result only while the row still carries its own token, so a worker that outlived its lease can never overwrite the newer claim's outcome; it logs `Email result not recorded` instead. |
| `sent` | Accepted by the SMTP server. The stored body, which can hold an invitation link, is cleared. |
| `failed` | Rejected permanently, or out of attempts. Not retried until queued again. |

Failures are classified:

- **Transient**: connection errors and timeouts, TLS or certificate errors,
  authentication failures, missing STARTTLS, and SMTP `4xx` replies. The message is
  retried after `NEST_EMAIL_RETRY_DELAY` seconds, doubling per attempt up to
  `NEST_EMAIL_RETRY_MAX_DELAY`, until `NEST_EMAIL_MAX_ATTEMPTS` attempts (default 5)
  have been made.
- **Permanent**: SMTP `5xx` rejections of the sender, a recipient, or the content.
  The message is `failed` at once.

A `failed` message can be queued again with `EmailOutbox.retry`: its
attempt count restarts. A `sent` message cannot be re-triggered, so resending never
duplicates a delivered message. Delivery is at least once: if a Controller stops,
or an attempt outlasts its lease, after the server accepted a message but before
the result was recorded, the message is sent again with the same `Message-ID`
header, which lets mail clients recognize the copy.

### Observe delivery

Every attempt is logged without the recipient, subject, body, link or token:

```text
INFO nest.db.outbox ... Email queued outbox_id=12 kind=project_invitation
WARNING nest.mail ... Email delivery attempt failed outbox_id=12 kind=project_invitation attempt=1/5 error=SMTPConnectError code=421 retry_in_s=60
INFO nest.mail ... Email sent outbox_id=12 kind=project_invitation attempt=2/5
ERROR nest.mail ... Email delivery failed outbox_id=13 kind=project_invitation attempt=1/5 error=SMTPRecipientsRefused code=550 kind_of_failure=permanent; queue it again to retry
```

The stored `last_error` has the same form: an error class and SMTP reply code,
never the server's reply text, which can echo addresses. Database errors from the
outbox are reported the same way, as the error class, SQLSTATE and constraint,
table and column names only (for example `CheckViolation sqlstate=23514
constraint=email_outbox_kind_check table=email_outbox`): PostgreSQL's message text
is dropped, because its `DETAIL` can quote the whole row, recipient and body
included. Delivery status returned
by `EmailOutbox.status` and `EmailOutbox.statuses` contains no recipient or
content, so it can be shown to the Project administrators who manage the
invitation; recipients and bodies are read only by the delivery worker. To watch
the queue from SQL:

```sql
SELECT status, count(*) FROM email_outbox GROUP BY status;
SELECT id, kind, attempts, last_error, next_attempt_at
FROM email_outbox WHERE status IN ('pending', 'failed') ORDER BY id;
```

The table holds recipient addresses and, until delivery, message bodies with
invitation links. Restrict database access accordingly, and include it in your
encrypted backups.

## Disable email

Set `NEST_EMAIL_ENABLED=false`, or unset it, and restart. Other `NEST_SMTP_*`
values may stay; they are ignored. Queued messages stay in the outbox and are
delivered when email is enabled again, unless you delete them.

## Test email delivery

The tests never contact a real mail provider. `tests/fake_smtp.py` is an
in-process SMTP server with real TLS from throwaway certificates (created with
`openssl`), offering STARTTLS, implicit TLS, or no STARTTLS, with optional
authentication and injected failures. `tests/test_email.py` covers:

- configuration: disabled by default, stale SMTP settings ignored while disabled,
  every missing or insecure setting reported, `_FILE` secrets and rotation, and no
  password in errors, `repr()` or the summary;
- transport: STARTTLS and implicit TLS delivery, refusal to continue without
  STARTTLS, untrusted certificates and host-name mismatches, authentication and
  recipient failures classified by code, and no secrets in errors or logs;
- outbox (PostgreSQL): idempotent and transactional queuing, delivery and body
  clearing, bounded backoff, permanent failures, re-triggering without duplicates,
  concurrent workers, interrupted claims, stale workers fenced off by claim tokens
  (late successes and failures after a reclaim), database errors that never echo
  row contents, and a stand-in invitation that stays `PENDING` throughout;
- the Controller process: starting with email disabled, rejecting invalid enabled
  settings, starting while SMTP is unreachable, and delivering a queued message.

```bash
export NEST_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
uv run pytest tests/test_email.py
```

Without `NEST_TEST_DATABASE_URL` the outbox and delivery tests are skipped (or fail
in CI, which sets `NEST_TEST_DATABASE_REQUIRED=1`); the others always run.

To try a deployment against a local catcher such as Mailpit, give it a certificate
for the host name you connect to, point `NEST_SMTP_CA_FILE` at its CA, and set
`NEST_SMTP_HOST` to that name. Nest does not offer a mode that skips verification,
even for testing.
