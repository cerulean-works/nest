# Security and limitations

Nest currently uses Python object serialization for remote function execution.

Deserializing untrusted Python payloads may execute arbitrary code.

With a database, the Controller authenticates every gRPC call with a Nest session
and authorizes it with the Project policy (see
[Authenticated execution](#authenticated-execution)). It does not provide per-user
HPC identity management: every job runs as the Controller's configured HPC account.

Only trusted users should be granted access to the Controller.

**Authentication and authorization are not isolation.** Session authentication
decides *who* may submit to *which* Project; it does not sandbox what a submitted
function does. Every Run executes trusted, deserialized Python as the Controller's
single HPC account, in a work directory shared by all Projects, without an OS
sandbox or per-user HPC identity. A member of any approved Project can therefore
read or change what other Projects' Runs leave in that account. Approve only
Projects whose members you would trust with the account itself.

For the current team deployment, restrict network access using Tailscale Grants / ACLs.

Do not expose the Controller directly to the public Internet.

The [shared Tailscale deployment](tailscale-shared) exposes only TCP 50051 to
accepted, policy-authorized recipients. All callers still submit work as the
Controller's single HPC SSH user. Node Sharing and Grants restrict network
reachability; they do not authenticate callers inside Nest or isolate jobs.

Project roles (see [Share Projects and manage roles](projects)) are enforced by
one server-side, deny-by-default policy on the Projects API and the gRPC execution
service alike. They grant no HPC authority: Run actions also need the Provider's
permission, which no Project role or Platform Admin role implies.

## Authenticated execution

`NEST_CONTROLLER_AUTH=session`, the default when `NEST_DATABASE_URL` is set, makes
every gRPC call carry a Nest CLI session (`authorization: Bearer <access token>`,
sent by the SDK to the hosted Controller, or to any Controller when
`NEST_AUTH_URL` names the sign-in origin; see
[Sign in from the command line](cli-login)) and checks it with
[`authz.ExecutionAccess`], which uses the same [`authz.Authorizer`] as the Projects
API:

| RPC | Needs | Refused as |
| --- | --- | --- |
| `Execute`, `SubmitJob` | `run.submit` in the Project named by `NEST_PROJECT_ID`, a `jobs:run` scope, a Project the operator approved in `NEST_PROVIDER_ALLOWED_PROJECTS`, and for Slurm the served Platform eligible for the Project | `NOT_FOUND` when not a member or the Platform is not eligible, `PERMISSION_DENIED` for the role, scope or Provider |
| `GetJobStatus` | `run.read` in the job's recorded Project, and the job's Platform (the served one) still eligible for it | `NOT_FOUND` |
| `CancelJob` | `run.cancel` in the job's recorded Project, being its submitter, and the job's Platform still eligible | `NOT_FOUND`, or `PERMISSION_DENIED` for someone else's Run |

- A call without an accepted session is refused with `UNAUTHENTICATED`. Browser
  session tokens are not accepted, and a revoked session, a disabled User or a
  removed member is refused from the next call on: nothing is cached.
- Each submission records its Project and submitter with the job. A job recorded
  without a Project, such as one from before authentication, cannot be read or
  cancelled through an authenticated Controller.
- Unauthorized access is denied by default: with no `NEST_PROVIDER_ALLOWED_PROJECTS`
  no Run is accepted, and without Google sign-in nobody can obtain a session.
- Session tokens travel only over TLS (`NEST_TLS_CERT_FILE` and `NEST_TLS_KEY_FILE`)
  or a loopback listener. On a network that is already encrypted and
  access-controlled, such as an authorized Tailnet, the operator approves plaintext
  gRPC explicitly with `NEST_CONTROLLER_ALLOW_PLAINTEXT=true`; the Controller
  refuses to start otherwise.
- Nest authorization and HPC permissions stay separate. A Nest session is never sent
  to a login node, and Slurm and UNIX permissions on the cluster are unchanged.

`NEST_CONTROLLER_AUTH=none`, the only mode without a database, serves every caller
that can reach the listener; the network restrictions above are then the only
access control, and the Controller logs a warning when it listens beyond loopback.

Every submission and cancellation, every refusal on either API, and identity,
session, invitation and Provider changes are recorded without secrets in the
[security audit log](audit-log).

## Platform authorization

HPC Platforms (see [Register HPC Platforms and login nodes](platforms)) are
authorized by the same server-side, deny-by-default policy as Projects. There is
no separate Platform role: a User may use a Platform only through an active
membership in a live Project that the operator granted the Platform to, and only
while the Platform is active. Every decision is made from the authenticated
principal and the immutable Project and Platform IDs, read from the database on
each call; a client can name a Platform or an approved login node only as a
reference to check, never as authority.

| Operation | Interfaces | Enforced by | Refused as |
| --- | --- | --- | --- |
| Discover Platforms, describe one | `GET /api/platforms[/<id or slug>]`, `nest platforms`, [`platforms.list_platforms`] | [`authz.PlatformCatalog.list_platforms`], `.resolve_platform` | not found (HTTP 404) |
| Read a Project's eligible Platforms and default | `GET /api/projects/<id>/platforms`, [`platforms.project_platforms`] | [`authz.PlatformCatalog.project_platforms`] (`project.platform.read`, any role) | not found for a non-member |
| Choose or clear a Project's default | `POST /api/projects/<id>/platforms/default` (browser only) | [`authz.PlatformCatalog.set_project_default`] (`project.platform.default.set`, Owner and Admin) | 403 for the role, 404 for a Platform that is not eligible |
| Select the Platform and login node of a submission | gRPC `Execute`, `SubmitJob` with the Execution Registry | [`authz.PlatformCatalog.select_platform`], `.select_login_node`, then [`authz.AccountResolver`] | `NOT_FOUND`, or `FAILED_PRECONDITION` with the reason |
| Submit, read or cancel a Slurm job on the served Platform | gRPC `Execute`, `SubmitJob` (legacy path), `GetJobStatus`, `CancelJob` | [`authz.ExecutionAccess`]: the Project check, then [`authz.PlatformCatalog.authorize_platform_action`] | `NOT_FOUND` |
| Register, change, disable Platforms, approve login nodes, grant Projects | `nest-db platform` on a host with the database credentials | The database: each change must name an operator, or is refused | not applied |

- **Fail closed.** A revoked grant, a disabled Platform, a removed member, a
  disabled User or a deleted Project is refused from the next call on, and a
  refused Platform is never replaced by another. A Controller whose served Platform
  is not in the catalog refuses every Platform-bound RPC. Running with trusted
  Mode A credentials never skips a check.
- **No existence leaks.** The Project check comes first, so a non-member learns
  nothing about a Project's Platforms. A Platform the caller may not use is
  answered exactly like one that does not exist, and no host, port, host key or
  SSH option is ever returned to a caller.
- **Separate authorities.** No Project role, Organization role or `platform_admin`
  makes a Platform eligible, changes the catalog through an API, or grants a UNIX
  account, a Slurm account or any other HPC identity. Catalog changes are
  operator actions with database credentials; Slurm account choice is the
  [Slurm Account Registry](slurm-accounts)'s.
- **Audited.** Every catalog change, grant and default change is recorded by a
  database trigger with the operator or the acting User. Every refusal is recorded
  as the refused action (such as `run.submit` or `platform.resolve`) with the
  actor, session, Project, reason (such as `platform_not_authorized`) and Platform
  ID, never an endpoint credential. See [Review the security audit log](audit-log).

Future interfaces, such as the Relay, a Web UI or Mode B interactive SSH, must use
the same checks: resolve and authorize the Platform with
[`authz.PlatformCatalog`] after the Project check, bind it to the authenticated
principal, and answer refusals as not found. A Web UI may hide controls that will
be refused; that is never the check. None of these interfaces exists yet.

## Deployment credentials

Keep SSH keys, `known_hosts`, and the Tailscale Auth Key in the ignored `ssh/` and
`secrets/` directories, Dokploy File Mounts, or the deployment platform's
environment settings ([Deploy on Dokploy from environment variables](dokploy)),
never in Git, a committed `.env`, or Compose YAML. Keep `StrictHostKeyChecking yes` with a verified `known_hosts`; never use
`accept-new`, `no`, or `/dev/null` to work around a host-key error. The test suite
fails if a tracked file contains a private key or token, or weakens host-key
verification. See [Deployment validation and roadmap](deployment-architecture).

Every credential can also be given directly in the environment
([Credential sources](configuration#credential-sources)): the database password,
the Google client secret, the SMTP password, gRPC and database TLS keys, SSH keys,
`known_hosts`, Mode A SSH configuration, and in `compose.dokploy.yaml` the
Tailscale Auth Key. Set them from the platform's secret store or environment
panel, never in Git. Nest never logs them, masks them in its own log output,
and keeps them out of SSH, `scp` and local worker processes, so user code, Slurm
jobs and login nodes never receive them. Files that OpenSSH or libpq need are
generated in private tmpfs directories (`0700`, files `0600`) with literal writes
that never replace an existing file. Direct values remain visible through
`docker inspect`, `/proc/<pid>/environ` and the platform's stored settings to
anyone with that access, so prefer the `*_FILE` forms where a secret-file mount is
available; see [Security trade-offs](dokploy#security-trade-offs). Mode B always
uses `StrictHostKeyChecking=yes` and ignores every SSH configuration file. Inline
Mode A SSH configuration that weakens host-key checking is refused.

The Controller database (see [Store Controller records in PostgreSQL](database))
holds job metadata, not payloads or credentials. Connect to it with
`NEST_DATABASE_SSLMODE=verify-full` and a CA, keep its password in
`NEST_DATABASE_PASSWORD_FILE` or `NEST_DATABASE_PASSWORD` rather than the URL, and
store backups encrypted.
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
is asking for your Nest account. With session authentication the SDK sends the CLI
session to the Controller over TLS or an approved encrypted network only, never to
an HPC login node.

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
- No isolation between Projects on the HPC side: all Runs share the Controller's
  HPC account and work directory.
- No per-user HPC accounts.
- No automatic project source or data synchronization.
- No authentication without a database (`NEST_CONTROLLER_AUTH=none`, the
  [local development](local-controller) mode). `NEST_AUTH_TOKEN_FILE` (static
  service tokens) is reserved and rejected at startup.
- No OS sandbox or multi-tenant isolation for submitted code (see above).
- The SDK calls only the synchronous `Execute` RPC (`.remote()` waits for the
  result). Asynchronous `SubmitJob`, `GetJobStatus` and `CancelJob` are gRPC RPCs
  for prepared batch scripts, without an SDK wrapper; see
  [Execution service and RPCs](api/controller).
- The SDK does not choose an HPC Platform per call: Runs use the Project's
  default Platform (see [Default Platform](platforms#project-eligibility-and-default-platform)).
- Projects are created through the Projects API from a browser session; there is
  no Web UI page or CLI command for it yet.
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
