# Bootstrap and recover platform administration

`platform_admin` is the service-wide role that manages Nest itself. It grants no
Project role and no Provider or HPC access (see
[Users, Projects and memberships](database#users-projects-and-memberships)). A new
deployment has no administrator. Nest creates the first one exactly once, from
the Google account you name in `NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL`. After that, only
an existing administrator or an operator with database credentials can grant the
role.

The bootstrap needs the PostgreSQL database (`NEST_DATABASE_URL`) with migration
`0005_platform_admin_bootstrap` applied (`nest-db upgrade`), and
[Google sign-in](authentication) enabled (`NEST_GOOGLE_CLIENT_ID`).

## Bootstrap the first administrator

1. Set the address of the Google account that should become the first
   administrator, then restart the Controller:

   ```bash
   export NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL="admin@example.com"
   ```

   The Controller logs `Platform bootstrap pending` at startup. It never logs the
   address.

2. Sign in to Nest with that Google account. At the first successful sign-in whose
   address Google reports as **verified**, Nest records the bootstrap and grants
   `platform_admin` to that User in one transaction.

3. Check the result:

   ```bash
   nest-db admin status
   # bootstrap: completed 2026-11-17T09:12:03+00:00 method=google_first_login user_id=0b6c…
   # platform_admin: 1
   #   0b6c… granted=2026-11-17T09:12:03+00:00 status=active
   ```

4. Unset `NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL`. Leaving it set is harmless, as the
   next section explains, and the Controller logs at startup that it has no effect.

### What the bootstrap guarantees

- **Only that account, only verified.** The identity must be a Google identity
  (issuer `https://accounts.google.com`). Its current email claim, as stored
  by the sign-in, must equal the configured address (matched case-insensitively,
  with no other rewriting, so Gmail's dot and `+` variants are different
  addresses). Google must report it as verified. The User must not be
  disabled. Nest reads these facts from the database inside the claim's
  transaction, never from values the caller passes in.
- **Once.** The bootstrap is a single row in `platform_bootstrap`, so concurrent
  sign-ins serialize on it and at most one claim succeeds. The row cannot be
  updated, deleted or truncated.
- **Changing the setting grants nothing.** Once the row exists, a different
  address, or the same one on a new Google account, claims nothing, and
  existing role assignments are never touched. Revoking every administrator
  does not reopen the bootstrap either.
- **Any earlier administrator closes it.** If an administrator is granted any
  other way before the configured account signs in, through `nest-db admin
  grant`, the Python API or plain SQL, that grant records the bootstrap
  (`method=direct_grant`) and the setting can no longer take effect. A
  database that already had an administrator when migration 0005 was applied
  is recorded as `method=preexisting_admin`.
- **Audited.** The claim, every grant and revocation of `platform_admin`, and
  every refused claim by an identity with the configured address are written to
  `platform_audit_events` (see [Audit log](#audit-log)).

- **Active Users only.** A disabled User cannot be granted `platform_admin`
  (`nest-db admin grant` exits with status 1), and an administrator who is
  disabled has no effective role until re-enabled (migration
  `0016_platform_role_boundaries`).

If the claim is refused, for example because the address was not yet verified,
the sign-in itself still succeeds without the role, a `bootstrap_refused` event
records why, and a later verified sign-in can still claim.

## Recover platform administration

Use this when no usable administrator is left: the only administrator left the
team, lost their Google account or was disabled, or the wrong person holds the
role. Recovery uses `nest-db admin` on a host that has the Controller's database
settings. It does not reopen the bootstrap and does not use
`NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL`.

**Authentication.** `nest-db admin` authenticates as the PostgreSQL role in
`NEST_DATABASE_URL` (with `NEST_DATABASE_PASSWORD_FILE`, TLS client certificates
and the other `NEST_DATABASE_*` settings, exactly as the Controller does). Anyone
who can run it can already change every table, so restrict those credentials to
the operators who may recover administration. Every change records the
PostgreSQL role (`database_role`, set by the server and not by Nest) together
with the operator name and reason you give. The operator name is self-declared;
the database role is the authenticated part. Give each operator their own
database role if you need to tell operators apart.

1. Make sure the schema is current. `nest-db admin` refuses to run otherwise:

   ```bash
   nest-db verify
   ```

2. Inspect the current state:

   ```bash
   nest-db admin status
   nest-db admin audit --limit 20
   ```

3. Ask the new administrator to sign in to Nest once, so that they have a User,
   then find their User ID. Matching ignores case. Check that the identity is the
   one you expect (`issuer`, `subject`) and that `email_verified=True`, because
   an address alone never identifies a User:

   ```bash
   nest-db admin find-user --email new.admin@example.com
   # 5f1e… status=active issuer=https://accounts.google.com subject=1034… email_verified=True last_login=…
   ```

4. Grant the role, naming yourself and the reason:

   ```bash
   nest-db admin grant --user-id 5f1e… --operator "alice (ops)" \
       --reason "Only admin account disabled; ticket OPS-123"
   ```

5. Revoke the role from any account that should no longer hold it:

   ```bash
   nest-db admin revoke --user-id 0b6c… --operator "alice (ops)" \
       --reason "Account compromised; ticket OPS-123"
   ```

   To end that User's existing sessions as well, run
   `nest-db sessions revoke --user-id 0b6c…`, or disable the User (for example
   with [`db.IdentityStore.set_user_status`]), which also revokes all of their
   sessions. See [Revoke sessions](authentication#revoke-sessions).

6. Confirm with `nest-db admin status` and `nest-db admin audit`.

`grant` and `revoke` exit with status 1 when nothing changed (the User already had,
or did not have, the role, or does not exist), 2 for invalid arguments, and 3 when
the database is unreachable.

## Audit log

`platform_audit_events` is append-only: the database rejects every update, delete
and truncate. It records:

| Event | When |
| --- | --- |
| `bootstrap_completed` | The bootstrap row was written. `details.method` says how; for `google_first_login`, `details.issuer` and `details.subject` name the claiming identity. |
| `bootstrap_refused` | An identity with the configured address could not claim. `details.outcome` is `email_not_verified`, `not_google`, `user_not_active` or `already_completed`. Ordinary sign-ins, and the first administrator signing in again, are not recorded. |
| `platform_role_granted`, `platform_role_revoked` | Any change to `platform_role_assignments`, however it was made, including plain SQL and the cascade when a User row is deleted. |

Each entry has `target_user_id`, `actor_user_id` (an administrator acting through
Nest), `actor_operator` and `reason` (from `nest-db admin`), and `database_role`.
User IDs are kept even after the User is deleted. No email address is stored in
the log. Read it with `nest-db admin audit [--user-id UUID] [--limit N]` or
[`db.audit_events`].

## Integrate the bootstrap with sign-in

The Controller's Google sign-in ([`auth.GoogleSignIn`]) already calls
[`db.PlatformAdminBootstrap.claim`] on every successful sign-in, in the same
transaction as the sign-in and its session. A different sign-in flow can do the
same:

```python
from nest.db import BootstrapOutcome, IdentityStore, PlatformAdminBootstrap

identities = IdentityStore(database)
bootstrap = PlatformAdminBootstrap(database, config.bootstrap_admin_email)

with database.connection() as conn:
    user, identity, created = identities.sign_in(
        issuer=claims["iss"],
        subject=claims["sub"],
        email=claims["email"],
        email_verified=claims["email_verified"],
        conn=conn,
    )
    if bootstrap.claim(identity, conn=conn) is BootstrapOutcome.CLAIMED:
        ...  # user is now the first platform_admin
```

Pass only claims from an ID token whose signature, issuer, audience and expiry
you have verified.
