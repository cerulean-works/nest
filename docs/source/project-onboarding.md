# Onboard a Project

**This page is for operators** of an authenticated (M1) Controller: the people
who hold its database credentials and deployment settings. Users never run these
commands; they follow [Run your first remote function](quickstart) and send you
their Project ID.

A Project role never grants HPC access. Before members of a Project can run
anything, you approve it in three independent places, each checked at every new
submission and none implying another. Every change to them is recorded in the
[security audit log](audit-log). A `platform_admin` can make all three through
the [privileged management API](privileged-api) instead of `nest-db`:

| Step | Grants | Interface | Refusal a user sees without it |
| --- | --- | --- | --- |
| 1. Provider approval | The Controller's Provider accepts the Project's Runs | `nest-db provider approve` (durable, `NEST_PROVIDER_APPROVALS=database`), or `NEST_PROVIDER_ALLOWED_PROJECTS` read at start | `PERMISSION_DENIED` `provider_denied` |
| 2. Platform grant | The Project may use an HPC Platform | `nest-db platform grant` | `NOT_FOUND` *Platform not found*, or `FAILED_PRECONDITION` `none_authorized` |
| 3. Slurm account link | Which Slurm account the Project's jobs charge | `nest-db account register` and `link` | `PERMISSION_DENIED` `no_account` |

## Before you start

The Controller must already run with session authentication: a migrated
PostgreSQL database (`NEST_DATABASE_URL`, `nest-db upgrade`), Google sign-in
(`NEST_GOOGLE_*`, served through HTTPS), and gRPC over TLS or an approved
encrypted network (`NEST_CONTROLLER_ALLOW_PLAINTEXT=true` inside a Tailnet). See
[Deploy on Dokploy from environment variables](dokploy), or
[Deploy with Docker Compose](docker-compose) with
[Store Controller records in PostgreSQL](database) and
[Sign in with Google](authentication).

Give users these values; none of them is a secret:

- `NEST_CONTROLLER_ADDRESS` and whether to set `NEST_CONTROLLER_TLS=true` or `false`.
  For a Controller shared over Tailscale, give the node's MagicDNS name
  (`<node>.<tailnet>.ts.net:50051`; for Cerulean-hosted Nest,
  `nest-controller.tail8e5ba1.ts.net:50051`), not its `100.x.y.z` address: a shared
  node's address is assigned per Tailnet and may differ for each recipient. Use
  `NEST_CONTROLLER_TLS=false` only inside the authorized Tailnet;
- `NEST_AUTH_URL`: the origin of `NEST_GOOGLE_REDIRECT_URL`, such as
  `https://nest.example.com` (hosted: `https://auth.nest.cerulean.works`). Users
  must set it whenever `NEST_CONTROLLER_ADDRESS` is anything other than
  `nest.cerulean.works:50051`: otherwise the SDK sends no session to your
  Controller, and for a self-hosted Nest `nest login` signs in to Cerulean-hosted
  Nest instead;
- how to reach the Controller over Tailscale (Tailnet membership or a node share;
  see [Share a Controller over Tailscale](tailscale-shared)).

## Who can do what today

| Actor | Can | Cannot |
| --- | --- | --- |
| Any signed-in User | Become a User at first Google sign-in (no per-user provisioning); accept invitations | Create a Project (`platform_admin_required`); use the privileged management API; grant themselves a Platform, account or Provider approval |
| Project Owner | Everything an Admin can, plus transfer ownership and delete the Project | Grant a Platform, link a Slurm account, or approve the Project for a Provider |
| Project Admin | Invite, add existing Users (`POST /api/projects/<id>/members`), change non-Owner roles, choose the default among granted Platforms | Assign `owner`; anything HPC-side |
| `platform_admin` | With a browser session, through the [privileged management API](privileged-api): create Projects with an explicit Owner, read the User directory and audit log, grant Platforms, link registered Slurm accounts, approve Projects for a Provider | Act in a Project or on the cluster by that role alone: it grants no Project role, Platform, Slurm account or UNIX identity. Register Platforms, login nodes or Slurm accounts (operator CLI only) |
| Operator (database credentials and deployment settings) | Everything in `nest-db`, including `nest-db project create`, Provider approvals and their import, and the deployment settings | Bypass the one-Owner rule, audit triggers or soft-delete: the database enforces them for every writer |

The **creator** of a Project (`projects.created_by_user_id`) is recorded for audit
and never changes; the **Owner** is the current `owner` membership and changes
only by transfer. Only an active `platform_admin` (through Nest) or an operator
(`nest-db project create`) creates Projects, every time with an explicit Owner;
an ordinary signed-in User is refused at every entry point, including
`POST /api/projects`. Do not relax the CSP, the same-origin check or the CSRF
protections to work around it.

## Create a Project for a User

1. Ask the User to sign in once (`nest login`) and send you the User ID that
   `nest auth status` prints as `user:`. It is not secret. Do not collect email
   addresses or session files for this. (`nest-db admin find-user --email ...`
   or `GET /api/admin/users?email=...` also finds a User ID, if you already have
   the address.)
2. Create the Project with that User as its Owner, either as a `platform_admin`
   through `POST /api/admin/projects` (see
   [Use the privileged management API](privileged-api)), or as an operator in the
   Controller's environment (Dokploy terminal of the `controller` service, or
   `docker compose exec -T controller ...`):

   ```bash
   nest-db project create --name "Chemistry lab" --owner-user-id '<user-id>' \
       --operator ops-alice --reason "onboard the chemistry lab"
   ```

   ```text
   project_id=5c2ec1df-... owner=c4d48ca8-...
   ```

   The Project and its Owner are created in one transaction, and nothing is
   created if the Owner is not an active User (exit status 1). The database's
   audit triggers record `project_created` and the Owner's `member_added` (role
   `owner`) with your operator name and reason.

3. Send the Project ID to the User. They are its Owner and invite the others
   (see [Invite people to a Project](invitations)). Then continue with the three
   approvals below.

Never insert into `projects` or `project_memberships` with plain SQL without
setting the audit context.

Run `nest-db` where the Controller's `NEST_DATABASE_*` settings are available: a
Dokploy terminal of the `controller` service, `docker compose exec controller
nest-db ...`, or a host with the same environment. Every change takes
`--operator` and `--reason`, which the audit log keeps. The examples below use:

```bash
PROJECT='<project-id>'
ops=(--operator ops-alice --reason "onboard the chemistry lab")
```

## 1. Approve the Project for the Provider

Every Run executes as the Controller's single HPC account, so accepting a Project
is the operator's decision. The Provider is the Controller's default Platform
Profile ID (`default` for the `NEST_SLURM_*` form, otherwise the `NEST_PLATFORMS`
ID), or `local`.

### Durable approvals (recommended)

With `NEST_PROVIDER_APPROVALS=database`, the default whenever
`NEST_PROVIDER_ALLOWED_PROJECTS` is unset, approvals live in PostgreSQL:

```bash
nest-db provider approve campus --project-id "$PROJECT" "${ops[@]}"
nest-db provider list --project-id "$PROJECT"
```

A `platform_admin` can do the same with `POST /api/admin/provider-approvals/approve`.
An approval or revocation applies from the next submission on every Controller
sharing the database: nothing is cached, nothing is redeployed, and a Controller
restart changes nothing. Registry admission checks the approval again, locked,
in the transaction that records the attempt, so a revocation racing a submission
either waits for it or refuses it. If the database cannot answer, the submission
is refused (`UNAVAILABLE`), never treated as approved.

A revocation affects new submissions only: running jobs are not cancelled, and
members can still cancel their own existing Runs (`CancelJob` keeps every other
check: the role, being the submitter, the job's Project and Platform). It is kept
as a record (`status=revoked`), and only an explicit `approve` undoes it.
`nest-db provider revoke` also works for a Project that has no database approval
yet, such as one listed only in `NEST_PROVIDER_ALLOWED_PROJECTS`: it records a
revocation that every later import keeps.

### Startup-only list (legacy)

With `NEST_PROVIDER_APPROVALS=environment`, the default while the variable is
set, the Controller reads the comma-separated list once at start, and adding a
Project needs a redeploy. In this mode a database approval does nothing, but a
revocation recorded with `nest-db provider revoke` (or the privileged API) refuses
the Project's new submissions at once, even though it is still listed. Registry
admission checks for it again in the transaction that records the attempt, so a
revocation racing a submission either waits for it or refuses it, as in database
mode. The
privileged API reports which mode the Controller is in (`policy.source`), so an
approval made there is never mistaken for an effective one:

```ini
NEST_PROVIDER_ALLOWED_PROJECTS='<project-id-1>,<project-id-2>'
```

`*` accepts every Project; prefer an explicit list. With the list empty, every
submission is refused, and the Controller logs a warning at start. A value that
is not a UUID, or `*` mixed with IDs, stops the Controller with a configuration
error naming the variable.

**Where the value comes from.** Every Compose file passes the variable to the
`controller` service as `NEST_PROVIDER_ALLOWED_PROJECTS: ${NEST_PROVIDER_ALLOWED_PROJECTS:-}`,
so it must be set in the Compose project's environment, not inside the container:

- **Dokploy:** edit the application's **Environment** panel, save, and press
  **Deploy**. Dokploy writes the panel to the `.env` file next to the Compose
  file, and the Controller container is recreated with it. Restarting the
  existing container (or `docker restart`) keeps its old environment.
- **Docker Compose:** set it in `.env` (or the shell running Compose) and run
  `docker compose up -d controller`, which recreates the container when its
  environment changed.

Check the value the running Controller received, from the host or the Dokploy
terminal, without printing other settings:

```bash
docker compose -p YOUR_APP exec controller printenv NEST_PROVIDER_ALLOWED_PROJECTS
nest-db audit list --event controller.started --limit 1
```

The `controller.started` record's `provider_approvals` detail is the mode, and
`allowed_projects` the number of listed Projects (or `*`). If a user still sees
`provider_denied` after this, the Project ID they send (`NEST_PROJECT_ID`) is not
the one you approved.

### Move Provider approvals to the database

Nothing is imported implicitly: the Controller never copies
`NEST_PROVIDER_ALLOWED_PROJECTS` into the database, at start or otherwise.

1. Upgrade the schema (`nest-db upgrade`, migration
   `0017_provider_project_approvals`). The upgrade creates no approval and
   changes no Platform grant or account link.
2. Import the listed IDs explicitly. Preview first; `*` is refused, so list every
   Project you want to keep:

   ```bash
   nest-db provider import campus --project-ids "$(printenv NEST_PROVIDER_ALLOWED_PROJECTS)" \
       --dry-run "${ops[@]}"
   nest-db provider import campus --project-ids "$(printenv NEST_PROVIDER_ALLOWED_PROJECTS)" \
       "${ops[@]}"
   ```

   The import is one transaction and idempotent: an approved Project stays as it
   is, a Project with a recorded revocation **stays revoked** (it is listed as
   `left revoked`; approve it explicitly if that is really intended), and if any
   ID is not a live Project, nothing is written. Running it again changes
   nothing.
3. Cut over: unset `NEST_PROVIDER_ALLOWED_PROJECTS` (or set
   `NEST_PROVIDER_APPROVALS=database` and unset it) and redeploy. A Controller
   with `NEST_PROVIDER_APPROVALS=database` and the variable still set refuses to
   start, so the two can never be mixed. Check `provider_approvals=database` in
   the `controller.started` record.

**Rollback.** Never reuse the list you had before the cutover, and never use
`*`: Projects revoked since then would be listed again. Recompute the list from
the current database approvals, review it, and roll back every Controller sharing
the database in the same change:

```bash
nest-db provider export campus   # approved, live Projects only; revoked ones are left out
```

Set `NEST_PROVIDER_ALLOWED_PROJECTS` to that value (and
`NEST_PROVIDER_APPROVALS=environment` if you had set it) and redeploy every
Controller. As a second safeguard, an environment-mode Controller still refuses
new submissions from any Project with a recorded revocation, so even a stale list
cannot restore revoked access. Approvals recorded after the rollback do nothing
until you cut over again; the privileged API shows `approvals_effective: false`
for them. Cutting over again uses only the database records, including
revocations made in the meantime.

## 2. Grant an HPC Platform

List the Platforms and grant the one this Controller serves. A Platform from the
environment (`NEST_PLATFORMS`, or the `NEST_SLURM_*` form, whose Platform is
`default`) has the profile ID as its slug:

```bash
nest-db platform list
nest-db platform grant campus --project-id "$PROJECT" "${ops[@]}"
```

Each Controller submits to one Platform. A Project whose selected Platform is
another one is refused with `platform_not_served`, never run elsewhere. With
several eligible Platforms, set a server default (`nest-db platform set-default`)
or let a Project Owner or Admin choose the Project's default; see
[Register HPC Platforms and login nodes](platforms).

## 3. Register and link a Slurm account

Register each Slurm account once per Platform, then link it to the Project for the
Controller's SSH mode. With `--default`, jobs that name no account charge it:

```bash
nest-db account register campus chem "${ops[@]}"
nest-db account link campus chem --project-id "$PROJECT" --modes A --default "${ops[@]}"
nest-db account show campus --project-id "$PROJECT"
```

```text
project=... platform=campus granted=yes native_default=forbidden
  link chem id=1 modes=A default=yes status=active kind=standard
```

`NEST_SLURM_ACCOUNT` and a Platform Profile's `ACCOUNT` are not charged unless
registered and linked like this, or set as the Platform's Mode A operator default
(`nest-db account set-mode-a-default`). Mode A also needs the operator's UNIX user
(`NEST_PLATFORM_<ID>_SSH_USER` or `NEST_SLURM_SSH_USER`). See
[Map Slurm accounts to Projects](slurm-accounts) and
[Admit submissions through the Execution Registry](execution-registry).

## Verify

Ask a Project member to run:

```bash
nest auth status --check
nest platforms default --project-id "<project-id>"
```

and then the [quickstart](quickstart#4-run) example. A refusal names
its reason; see [Access and onboarding errors](remote-errors#access-and-onboarding-errors).
`nest-db audit list --project-id "$PROJECT"` shows every decision and refusal
for the Project. Tell the kinds of failure apart before changing anything:

| What the user sees | Layer | Where to fix |
| --- | --- | --- |
| `ConfigError`: *NEST_PROJECT_ID must be a Project ID (a UUID)*, or `INVALID_ARGUMENT` *Name the Project to run in* | Client setting; nothing reached the Controller's checks | The user's `NEST_PROJECT_ID` |
| `UNAUTHENTICATED` *requires a Nest session* | Sign-in | The user's `NEST_AUTH_URL` and `nest login` |
| `NOT_FOUND` *Project not found* | Membership: wrong ID, not a member, or deleted | Invitation, or the ID |
| `PERMISSION_DENIED` *run.submit: provider_denied* | Provider approval (step 1) | `nest-db provider approve`, or `NEST_PROVIDER_ALLOWED_PROJECTS` and a redeploy |
| `NOT_FOUND` *Platform not found*, `FAILED_PRECONDITION` `none_authorized` / `platform_not_served` | Platform grant (step 2) | `nest-db platform grant` |
| `PERMISSION_DENIED` `no_account`, `not_granted` | Slurm account (step 3) | `nest-db account link` |
| `Environment build <job> failed`, `RemoteExecutionError`, Slurm or SSH errors | Runtime, after every authorization check passed | [Runtime image diagnostics](runtime-image#diagnostics), the job logs |

Authorization refusals happen before any Slurm job is submitted. A runtime error
means the call was authorized and reached the cluster.

## Offboard

Each approval is withdrawn on its own, from the next call:

```bash
nest-db account unlink campus chem --project-id "$PROJECT" "${ops[@]}"
nest-db platform revoke campus --project-id "$PROJECT" "${ops[@]}"
```

Revoke the Provider approval with `nest-db provider revoke campus --project-id
"$PROJECT" "${ops[@]}"` (or remove the ID from `NEST_PROVIDER_ALLOWED_PROJECTS`
and redeploy). Revocations refuse new submissions; running jobs are not cancelled. To remove one
person, a Project Owner or Admin removes their membership; to stop a User
everywhere, revoke their sessions with `nest-db sessions revoke --user-id ...`
(see [Revoke sessions](authentication#revoke-sessions)).

## What not to do

- Do not set `NEST_CONTROLLER_AUTH=none` on a shared Controller to work around a
  refusal: it removes every Nest access check for everyone who can reach it.
- Do not ask users for their Nest session file or tokens, and never put a
  credential in an issue, chat or the repository.
- Approval in Nest does not isolate Projects on the cluster: all Runs share the
  Controller's HPC account and work directory, and run trusted, deserialized
  Python. See [Security and limitations](security).
