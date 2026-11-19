# Security and limitations

Nest currently uses Python object serialization for remote function execution.

Deserializing untrusted Python payloads may execute arbitrary code.

The current Controller does not provide application-level authentication, multi-tenant isolation, or per-user HPC identity management.

Only trusted users should be granted access to the Controller.

For the current team deployment, restrict network access using Tailscale Grants / ACLs.

Do not expose the Controller directly to the public Internet.

The [shared Tailscale deployment](tailscale-shared) exposes only TCP 50051 to
accepted, policy-authorized recipients. All callers still submit work as the
Controller's single HPC SSH user. Node Sharing and Grants restrict network
reachability; they do not authenticate callers inside Nest or isolate jobs.

## Deployment credentials

Keep SSH keys, `known_hosts`, and the Tailscale Auth Key in the ignored `ssh/` and
`secrets/` directories or Dokploy File Mounts, never in Git, `.env`, or Compose
YAML. Keep `StrictHostKeyChecking yes` with a verified `known_hosts`; never use
`accept-new`, `no`, or `/dev/null` to work around a host-key error. The test suite
fails if a tracked file contains a private key or token, or weakens host-key
verification. See [Deployment validation and roadmap](deployment-architecture).

Mode B (managed SSH, see [configuration](configuration)) can take the key from
`NEST_SSH_PRIVATE_KEY` for platforms without file mounts. Set it from the
platform's secret store or the deploying shell, never in `.env` or Git. The
Controller writes it to a private temporary file, never logs it, and masks it
in its own log output. It remains visible to anyone who can inspect the
container's environment, so prefer `NEST_SSH_IDENTITY_FILE` where a secret
file mount is available. Mode B always uses `StrictHostKeyChecking=yes` and
ignores every SSH configuration file.

The Controller database (see [Store Controller records in PostgreSQL](database))
holds job metadata, not payloads or credentials. Connect to it with
`NEST_DATABASE_SSLMODE=verify-full` and a CA file, keep its password in
`NEST_DATABASE_PASSWORD_FILE` rather than the URL, and store backups encrypted.
Nest redacts the URL's password and query string in every error and log line.
The optional `local-db` Compose profile reads a generated password from a file
and publishes PostgreSQL on `127.0.0.1` only, with no setting to change the
interface, because it has no TLS. Do not override its `ports` to publish it more
widely; databases other hosts reach need `verify-full` TLS.

Optional email delivery (see [Send email with SMTP](email)) always uses STARTTLS
or implicit TLS with a verified certificate, and never falls back to plaintext.
Keep the SMTP password in `NEST_SMTP_PASSWORD_FILE` from your secret manager.
Logs, errors and stored delivery errors never contain the password, recipient
addresses, message bodies, or invitation tokens. The `email_outbox` table does
hold recipients and, until delivery, bodies, so limit access to the database.

Optional Google sign-in (see [Sign in with Google](authentication)) verifies each
ID token's signature, issuer, audience, expiry, nonce and `email_verified`, binds
every callback to the browser that started it, and links Users by Google's `sub`,
never by email. Keep the client secret in `NEST_GOOGLE_CLIENT_SECRET_FILE`, and
serve the auth listener only through a TLS-terminating proxy.

Command-line sign-in (see [Sign in from the command line](cli-login)) is approved
in a signed-in browser, so no credential is typed into a terminal or passed in
`argv`. CLI sessions are scoped, short-lived and rotating, revocable with
`nest logout` or `nest-db sessions revoke`, and kept in a `0600` file. A Nest
session is not an HPC credential: it is never sent to a login node, and NCHC SSH
passwords and one-time codes are never asked for or stored by Nest sign-in.
Approve only sign-ins you started yourself: someone who sends you an approval link
is asking for your Nest account.

The database also holds Users' names and email addresses, invited email
addresses, and identity-provider subject IDs (see
[Users, Projects and memberships](database#users-projects-and-memberships)).
Session secrets (refresh secrets and access tokens) and invitation tokens are
stored only as SHA-256 hashes, and
database errors that Nest logs name only the failing constraint, never row
values.

The first `platform_admin` is created once, from the verified Google account
named by `NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL`; later changes to the setting grant
nothing. Anyone holding the database credentials can grant the role with
`nest-db admin`, so treat those credentials as administrator credentials. Role
changes and bootstrap events are kept in an append-only audit log. See
[Bootstrap and recover platform administration](platform-admin).

## Known limitations

- Experimental API with potential breaking changes.
- No multi-tenant isolation.
- No per-user HPC accounts.
- No automatic project source or data synchronization.
- No application-level authentication. `NEST_AUTH_TOKEN_FILE` is reserved and
  rejected at startup until it is implemented. TLS is available with
  `NEST_TLS_CERT_FILE` and `NEST_TLS_KEY_FILE`.
- Controller and backend deployment are managed by the host.

## Execution behavior

The client defaults to loopback and requires an explicit transport choice for
non-loopback addresses. TLS verifies the Controller's certificate and hostname.
The team deployment uses plaintext gRPC within its authorized Tailnet, with
access restricted by Tailscale and the host configuration.

Project dependency installation applies to the Slurm backend; a local Controller
uses its own environment.

Client RPC and backend polling deadlines do not cancel submitted Slurm jobs.
See [Environment variables and defaults](configuration) for current values and
[Handle remote errors](remote-errors) for failure handling.
