# Invite people to a Project

A Project's Owner and Admins bring people in two ways:

- An **email invitation** is addressed to one email address, registered with Nest
  or not. Only the person who signs in with that address, verified by Google,
  can redeem it.
- An **Invite Link** is a bearer link with an expiry and a use limit. Any signed-in
  User who opens it and explicitly accepts it joins, until it runs out.

Both grant `admin`, `member` or `viewer`, never `owner`, and never any Provider,
Slurm or UNIX account: Run actions still need the Provider's own permission (see
[Provider and HPC authority](projects#provider-and-hpc-authority)). Every check
happens on the Controller in [`authz.InvitationService`]; see
[Authorization](api/authz).

## Who may do what

| Action | Owner | Admin | Member | Viewer | Invitee |
| --- | --- | --- | --- | --- | --- |
| Issue an email invitation or Invite Link for `admin`, `member`, `viewer` | yes | yes | no | no | |
| List, resend and revoke the Project's invitations and Invite Links | yes | yes | no | no | |
| See, accept or decline their own pending email invitations | | | | | yes |
| Accept an Invite Link | | | | | any signed-in User |

Managing invitations and accepting them need a browser session. A CLI session can
only list its User's pending invitations, with the `projects:read` scope.

## Email invitations

**A pending invitation is not a membership.** Issuing one creates no User and no
membership: it records the normalized address (trimmed and lowercased), the role,
the issuer and the expiry (7 days by default, at most 30). An address has at most
one pending invitation per Project; a second is refused with `already_invited`,
and inviting the address a member already signs in with is refused with
`already_member`.

An invitation is redeemed in one of two ways:

- **At first sign-in.** When someone signs in with Google for the first time, Nest
  accepts every pending, unexpired invitation addressed to their verified Google
  address in the same transaction that creates their User, so they see those
  Projects immediately. A Google account whose address is not verified cannot
  sign in at all.
- **Explicitly, by an existing User.** A User who already has a Nest account sees
  their pending invitations at `GET /api/invitations` and accepts or declines each
  one, or opens the invitation link and accepts it on the invitation page.

Only a User whose sign-in identity *currently* reports the invited address as
verified can redeem it. Any other account, including one whose address is not
verified, is refused: by ID the invitation looks as if it did not exist, so IDs
cannot be probed; by link the holder is told the invitation is for another
address. A User who is already a member when they accept keeps their current role.

An invitation is honored only while it is pending and unexpired, its Project still
exists, and its issuer is still an active Owner or Admin of the Project. Demoting
or removing the issuer therefore stops their outstanding invitations and Invite
Links from working; another Owner or Admin can issue new ones.

### Email delivery

With email enabled (see [Send email with SMTP](email)), issuing an invitation
queues its email in the delivery outbox in the same transaction. Delivery never
changes the invitation: one whose email failed stays `pending`, and its delivery
state is shown next to it to the Project's Owner and Admins:

```json
{"status": "failed", "attempts": 1, "max_attempts": 5,
 "last_error": "SMTPRecipientsRefused code=550", "last_error_kind": "permanent"}
```

**Resend** issues a new link (the previous one stops working) and, with email
enabled, queues a new message. Use it to retry a failed delivery, or to give the
recipient a fresh link by hand. The expiry stays the same.

With email disabled, nothing changes except that no message is sent: issuing and
resending return the recipient-bound link, which the issuer copies and sends
themselves. The link is useless to anyone else, because redeeming it needs the
invited verified address. Either way the link is returned once, when it is issued,
and never stored: only its SHA-256 is.

Links have the form `https://nest.example.com/invitations/accept#token=...`. The
token is in the URL fragment, which browsers never send to a server, so it stays
out of proxy and access logs. The base is `NEST_PUBLIC_URL` with email enabled,
and the origin of `NEST_GOOGLE_REDIRECT_URL` otherwise.

## Invite Links

An Invite Link grants one role to up to `max_uses` Users (1 by default, at most
10,000) until it expires (7 days by default, at most 30) or is revoked. Its token
has 256 bits of entropy and only its SHA-256 is stored.

**Opening a link never joins a Project.** The invitation page shows the Project
and role, and the User joins only by pressing *Accept* while signed in. Each
acceptance locks the link, records a redemption for that User and counts one use,
in the same transaction as the new membership, so concurrent acceptances can never
admit more than `max_uses` Users. A User who is already a member uses nothing up,
and each User can redeem a given link once: someone who left cannot rejoin through
the same link.

Revoking a link stops new acceptances. Memberships it already granted stay; remove
those members separately if needed.

## The invitation page

`/invitations/accept` is the page every invitation link opens. Its one script
moves the token from the URL fragment into the page's form and removes it from the
address bar. Without a session, the page offers Google sign-in and keeps the token
in the tab's `sessionStorage` for the way back. Signed in, the User reviews the
Project and role, then presses *Accept* (or *Decline*, for an email invitation).
The decision is a same-origin `POST` with a CSRF token bound to the session.

## Revocation, expiry and membership changes

- A revoked, declined, accepted or expired invitation cannot be redeemed, and
  neither can a revoked, expired or used-up Invite Link.
- An accepted invitation is an ordinary membership. Its role is enforced on every
  request, and removing the member or lowering the role takes effect on their next
  request (see [Revocation and caching](projects#revocation-and-caching)).
- Pending invitations past their expiry are refused at once. They are shown as
  `expired`, and marked so in the database the next time the Project's
  invitations are listed.

## Audit

Every step is recorded in the security audit log (see
[Review the security audit log](audit-log)) without the invited address or any
token:

| Step | Events |
| --- | --- |
| Issue | `invitation.created`, `invitation.email_queued` (with email), `invite_link.created` |
| Resend | `invitation.token_issued`, `invitation.email_queued` (with email) |
| Accept | `invitation.accepted`, `invite_link.redeemed`, and `member_added` in the Project audit history |
| Decline, revoke, expire | `invitation.declined`, `invitation.revoked`, `invite_link.revoked`, `invitation.expired` |
| Link used up | `invite_link.exhausted` |
| Delivery failure | `invitation.email_failed`, outcome `failed`, with the error class and SMTP code |
| Refused attempt | `invitation.accept`, `invitation.decline`, `invite_link.redeem` or `invitation.manage`, outcome `denied`, with the reason |

## API

The auth listener serves these JSON routes alongside the
[Projects API](projects#projects-api), with the same authentication, same-origin
and error rules. `POST` bodies are JSON.

| Route | Body | Does |
| --- | --- | --- |
| `GET /api/projects/<id>/invitations?include_closed=true` | | Lists email invitations with delivery state |
| `POST /api/projects/<id>/invitations` | `email`, `role`, optional `ttl_seconds`, `send_email` | Issues an invitation; returns it and its `link` |
| `POST /api/projects/<id>/invitations/<n>/resend` | optional `send_email` | Issues a new link, queues a new email |
| `POST /api/projects/<id>/invitations/<n>/revoke` | | Revokes it |
| `GET /api/projects/<id>/invite-links?include_closed=true` | | Lists Invite Links |
| `POST /api/projects/<id>/invite-links` | `role`, optional `max_uses`, `ttl_seconds` | Creates one; returns it and its `link` |
| `POST /api/projects/<id>/invite-links/<n>/revoke` | | Revokes it |
| `GET /api/invitations` | | Your pending email invitations |
| `POST /api/invitations/<n>/accept`, `/decline` | | Accepts or declines one of them |
| `POST /api/invitations/preview` | `token` | What a link's token grants; changes nothing |
| `POST /api/invitations/redeem` | `token`, optional `decision` (`accept` or `decline`) | Accepts or declines by token |

```bash
curl -s -b "__Host-nest_session=$SESSION" -H "Origin: https://nest.example.com" \
    -H "Content-Type: application/json" \
    -d '{"email": "ada@example.org", "role": "member"}' \
    https://nest.example.com/api/projects/$PROJECT/invitations
```

Besides the Projects API's errors:

| Status | `error` | Meaning |
| --- | --- | --- |
| `404` | `invitation_not_found` | No such invitation or link, or none addressed to you |
| `403` | `email_mismatch` | The invitation is for another address, or yours is not verified |
| `409` | `already_invited`, with `invitation_id` | The address already has a pending invitation |
| `409` | `already_member` | The User is already a member |
| `410` | `invitation_unavailable`, with a `reason` | `expired`, `revoked`, `accepted`, `declined`, `exhausted`, `already_redeemed`, `issuer_not_authorized`, `project_deleted` or `user_disabled` |

## Test invitations

`tests/test_invitations.py` covers the Owner/Admin/Member/Viewer matrix, pending
invitations without Users or memberships, first-sign-in redemption (atomic,
repeated and concurrent), email matching and verification, acceptance and decline,
duplicates, expiry, revocation, issuers who lose their role, deleted Projects,
Invite Link use limits under concurrent redemption, SMTP enabled, disabled,
invalid and failing, resend, tenant isolation, enumeration, and the absence of
addresses and tokens from logs and audit records. It drives the API, the
invitation page and Google sign-in over real HTTP.

```bash
export NEST_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
uv run pytest tests/test_invitations.py
```
