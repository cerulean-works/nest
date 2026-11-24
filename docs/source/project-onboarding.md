# Onboard a Project

**This page is for operators** of an authenticated (M1) Controller: the people
who hold its database credentials and deployment settings. Users never run these
commands; they follow [Run your first remote function](quickstart) and send you
their Project ID.

A Project role never grants HPC access. Before members of a Project can run
anything, you approve it in three independent places. Platform grants and
account links are recorded in the [security audit log](audit-log); the Provider
approval is a deployment setting, and each Controller start records how many
Projects it lists (`controller.started`):

| Step | Grants | Interface | Refusal a user sees without it |
| --- | --- | --- | --- |
| 1. Provider approval | The Controller's Provider accepts the Project's Runs | `NEST_PROVIDER_ALLOWED_PROJECTS`, read at start | `PERMISSION_DENIED` `provider_denied` |
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
| Any signed-in User | Become a User at first Google sign-in (no per-user provisioning); accept invitations; through the Projects API with a **browser** session, create a Project they own | Create a Project from the CLI (`browser_only`); grant themselves a Platform, account or Provider approval |
| Project Owner | Everything an Admin can, plus transfer ownership and delete the Project | Grant a Platform, link a Slurm account, or approve the Project for a Provider |
| Project Admin | Invite, add existing Users (`POST /api/projects/<id>/members`), change non-Owner roles, choose the default among granted Platforms | Assign `owner`; anything HPC-side |
| `platform_admin` | Manage Nest administration (see [Bootstrap and recover platform administration](platform-admin)) | Nothing in a Project or on the cluster by that role alone: it grants no Project role, Platform, Slurm account or UNIX identity |
| Operator (database credentials and deployment settings) | `nest-db` Platform grants and account links, `NEST_PROVIDER_ALLOWED_PROJECTS`, and the audited Project creation below | Bypass the one-Owner rule, audit triggers or soft-delete: the database enforces them for every writer |

The **creator** of a Project (`projects.created_by_user_id`) is recorded for audit
and never changes; the **Owner** is the current `owner` membership and changes
only by transfer. A Project is created with its creator as Owner.

Restricting Project creation to platform administrators is planned and **not**
enforced: today any active User with a browser session may call
`POST /api/projects`. In practice, Nest serves no page that does so, and the Nest
origin's Content-Security-Policy (`default-src 'none'`) blocks running `fetch()`
from the browser's developer console there, so hosted users ask the operator.
Do not relax the CSP, the same-origin check or the CSRF protections to enable a
console workaround.

## Create a Project for a User

Until Nest has a Project creation page or command, create the Project as an
operator through [`db.ProjectStore`], inside an audit context that names you and
your reason. The database's audit triggers record `project_created` and the
creator's `member_added` (role `owner`), with the creator as actor and your
operator name and reason, exactly as for other operator changes.

1. Ask the User to sign in once (`nest login`) and send you the User ID that
   `nest auth status` prints as `user:`. It is not secret. Do not collect email
   addresses or session files for this. (`nest-db admin find-user --email ...`
   also finds a User ID, if you already have the address.)
2. In the Controller's environment (Dokploy terminal of the `controller`
   service, or `docker compose exec -T controller ...`), run:

   ```bash
   OWNER_USER_ID='<user-id>' PROJECT_NAME='Chemistry lab' \
   OPERATOR='ops-alice' REASON='onboard the chemistry lab' \
   python - <<'EOF'
   import os

   from nest.config import load_database_config
   from nest.db import Database, IdentityStore, ProjectStore
   from nest.db._transaction import audit_context, transaction
   from nest.db.migrate import verify

   owner, name = os.environ["OWNER_USER_ID"], os.environ["PROJECT_NAME"]
   database = Database(load_database_config(), application_name="nest-ops")
   database.open()
   try:
       with database.connection() as conn:
           verify(conn)
       user = IdentityStore(database).get_user(owner)
       if user is None or user.status != "active":
           raise SystemExit("The owner is not an active Nest User; ask them to sign in first")
       with (
           database.connection() as conn,
           transaction(database, conn, "create a Project") as cursor,
           audit_context(cursor, operator=os.environ["OPERATOR"], reason=os.environ["REASON"]),
       ):
           project = ProjectStore(database).create_project(name, created_by_user_id=owner, conn=conn)
       print(f"project_id={project.id} owner={owner}")
   finally:
       database.close()
   EOF
   ```

   ```text
   project_id=5c2ec1df-... owner=c4d48ca8-...
   ```

3. Send the Project ID to the User. They are its Owner and invite the others
   (see [Invite people to a Project](invitations)). Then continue with the three
   approvals below.

`nest.db._transaction` is internal: this recipe is the supported fallback for the
current release and will be replaced by a supported command. Check it against the
installed version before use. Never insert into `projects` or
`project_memberships` with plain SQL without setting the audit context.

Run `nest-db` where the Controller's `NEST_DATABASE_*` settings are available: a
Dokploy terminal of the `controller` service, `docker compose exec controller
nest-db ...`, or a host with the same environment. Every change takes
`--operator` and `--reason`, which the audit log keeps.

## 1. Approve the Project for the Provider

Every Run executes as the Controller's single HPC account, so accepting a Project
is the operator's decision. Add its ID to the comma-separated list and restart or
redeploy the Controller:

```ini
NEST_PROVIDER_ALLOWED_PROJECTS='<project-id-1>,<project-id-2>'
```

The Controller reads the list only at start. `*` accepts every Project; prefer an
explicit list. With the list empty, every submission is refused, and the
Controller logs a warning at start. A value that is not a UUID, or `*` mixed with
IDs, stops the Controller with a configuration error naming the variable.
Approvals are not dynamic: there is no API or `nest-db` command for them, and
nothing changes until the Controller restarts with the new value.

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

The `controller.started` record's `allowed_projects` detail is the number of
listed Projects (or `*`). If a user still sees `provider_denied` after this, the
Project ID they send (`NEST_PROJECT_ID`) is not the one you listed.

## 2. Grant an HPC Platform

List the Platforms and grant the one this Controller serves. A Platform from the
environment (`NEST_PLATFORMS`, or the `NEST_SLURM_*` form, whose Platform is
`default`) has the profile ID as its slug:

```bash
ops=(--operator ops-alice --reason "onboard the chemistry lab")
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

and then the [quickstart](quickstart#6-run-a-function) example. A refusal names
its reason; see [Access and onboarding errors](remote-errors#access-and-onboarding-errors).
`nest-db audit list --project-id "$PROJECT"` shows every decision and refusal
for the Project. Tell the kinds of failure apart before changing anything:

| What the user sees | Layer | Where to fix |
| --- | --- | --- |
| `ConfigError`: *NEST_PROJECT_ID must be a Project ID (a UUID)*, or `INVALID_ARGUMENT` *Name the Project to run in* | Client setting; nothing reached the Controller's checks | The user's `NEST_PROJECT_ID` |
| `UNAUTHENTICATED` *requires a Nest session* | Sign-in | The user's `NEST_AUTH_URL` and `nest login` |
| `NOT_FOUND` *Project not found* | Membership: wrong ID, not a member, or deleted | Invitation, or the ID |
| `PERMISSION_DENIED` *run.submit: provider_denied* | Provider approval (step 1) | `NEST_PROVIDER_ALLOWED_PROJECTS` and a redeploy |
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

Remove the ID from `NEST_PROVIDER_ALLOWED_PROJECTS` and restart. To remove one
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
