# Store Controller records in PostgreSQL

A Controller can keep its control-plane records in PostgreSQL: the Provider Profiles
it is configured with and every Slurm job it submits. The records survive Controller
restarts, upgrades, and host moves.

The database is **externally managed** and **opt-in**:

- You provide the PostgreSQL server and an empty database, for example a managed
  cloud database or your organization's PostgreSQL cluster, and pass its URL in
  `NEST_DATABASE_URL`. The Controller never starts a server, never creates a
  database, and never falls back to SQLite, files, or memory. For development or a
  small self-hosted deployment, the optional `local-db` Compose profile can run the
  server for you (see [Run PostgreSQL with Docker Compose](#run-postgresql-with-docker-compose)).
- Without `NEST_DATABASE_URL`, the Controller runs stateless, as earlier releases
  did. Set `NEST_DATABASE_REQUIRED=true` to make a missing URL a startup error.
- When `NEST_DATABASE_URL` is set, the Controller checks the database before it
  accepts any RPC. It exits with status 3 if the database is unreachable, rejects
  the credentials or TLS settings, or has an out-of-date or modified schema. It
  never continues without the database it was given.
- Schema changes are applied only by the `nest-db upgrade` command, never by the
  Controller itself.

## What is stored

| Table | Contents |
| --- | --- |
| `provider_profiles` | One row per Provider Profile (login nodes, SSH mode and user, Slurm defaults). The environment stays the source of truth: the Controller rewrites these rows at every start, marks the default, and marks profiles that were removed from the environment `active = false`. Rows are never deleted, so job records keep their provider. |
| `jobs` | One row per `Execute` (kind `execution`) or `SubmitJob` (kind `batch`) request that reached Slurm: its Execution ID, provider, Slurm job ID, request ID, script and directories, resources, Nest's `status`, the last observed Slurm state, and the error kind and message on failure. |
| `email_outbox` | Messages queued for optional email delivery and their delivery status (see [Send email with SMTP](email)). Empty unless `NEST_EMAIL_ENABLED=true`. Each row keeps its recipient address; the body is cleared once the message is sent. |
| `users`, `user_identities`, `user_sessions`, `platform_role_assignments` | Nest Users, the external sign-in identities linked to them, signed-in sessions (stored as SHA-256 hashes of their secrets) and service-wide roles. See [Users, Projects and memberships](#users-projects-and-memberships). |
| `oidc_login_flows` | Google sign-ins started and not yet finished: SHA-256 hashes of the OAuth `state` and of the browser's sign-in cookie, with a 10-minute expiry (see [Sign in with Google](authentication)). Each row is deleted when its callback arrives or, once expired, when a later sign-in starts. Empty unless Google sign-in is enabled. |
| `organizations`, `organization_memberships` | Optional Organizations that group Projects, and their members' Organization roles. |
| `projects`, `project_memberships` | Nest Projects and their active members with their Project roles. |
| `project_email_invitations`, `project_invite_links`, `project_invitation_redemptions` | Pending email invitations, Invite Link grants, and each User's redemption of a link. Kept apart from memberships. |
| `platform_bootstrap`, `platform_audit_events` | The one-time record of how the first `platform_admin` was created, and the append-only audit log of platform role changes. See [Bootstrap and recover platform administration](platform-admin). |
| `schema_migrations` | The applied schema versions and their checksums (see [Migrations](#migrations)). |

Every record has an `execution_id`, a UUID written as 32 lowercase hexadecimal
characters. The Controller generates it and stores the row before any SSH or Slurm
command. For an `execution` record the same ID names the remote execution
directory (`<workdir>/jobs/<execution_id>`, stored in `job_dir`) and is passed to
the worker job. So even a record whose submission outcome is `unknown` identifies
the directory to check for `output.pkl`:

```sql
SELECT execution_id, job_dir, error_message FROM jobs WHERE status = 'unknown';
```

A job record moves through these `status` values:

| Status | Meaning |
| --- | --- |
| `submitting` | Written before any SSH command. If it stays here, the Controller stopped or lost the database during submission. |
| `submitted` | `sbatch` returned the Slurm job ID. Batch jobs stay here; `slurm_state` is updated by each `GetJobStatus` call. |
| `succeeded` | An `Execute` worker finished and its result was returned. A function that raised still counts: Nest returned its exception. |
| `failed` | SSH or Slurm failed. `error_kind` holds the [error kind](remote-errors). |
| `unknown` | `sbatch` may have submitted a job, but no job ID arrived (`submission_unknown`). |

Local execution (`NEST_EXECUTOR=local`) creates no job records. No user payloads,
results, or credentials are stored, nor are `SubmitJob` script arguments.

**If the database fails while the Controller runs**, a new Slurm request is refused
before any side effect, with gRPC status `UNAVAILABLE` and error kind
`database_unavailable` (`nest.errors.DatabaseUnavailableError`). Nothing was
submitted, so the call is safe to retry. Updates after a job was submitted are
logged as errors instead, and the RPC still returns its result, because the
job cannot be un-submitted. The pool reconnects on its own once the database is
back.

## Users, Projects and memberships

Migration `0003_identity_and_projects` adds the records that authentication and
shared Projects build on. [Google sign-in](authentication) creates Users,
identities and browser sessions, and the Projects API applies the authorization
policy to Projects and memberships (see
[Share Projects and manage roles](projects)); invitation flows are a separate
feature. The data model and its rules are enforced by the database.

**Separate identities.** Each kind of identity has its own key, and none is
derived from another:

| Identity | Key | Notes |
| --- | --- | --- |
| Nest User | `users.id`, a generated UUID | Never an email address. A User row exists only once a person has signed in or an operator has created it. |
| External sign-in identity | `user_identities (issuer, subject)` | The identity provider's stable subject ID (Google's `sub`) under its issuer, linked to one User. A changed email keeps the same User; a reused address on another subject is a different identity. These identity providers are unrelated to Compute Providers. |
| Nest Project | `projects.id`, a generated UUID | Independent of its name, Owner and Provider Accounts. Two Projects may share a name. |
| Provider and Provider Account | `provider_profiles.id` and the Provider's account name | No identity or Project table references them. |

**Membership.** Users and Projects are many-to-many through `project_memberships`,
one row per (Project, User) (`UNIQUE (project_id, user_id)`), with a role of
`owner`, `admin`, `member` or `viewer`. Removing a member deletes the row, and
the member's access ends when that transaction commits.

**Exactly one Owner.** The Owner is the membership with role `owner`. The
database enforces exactly one per Project: a unique index allows at most one at
any moment, even across concurrent transactions, and a deferred constraint trigger
checks at commit that there is one. A Project and its first Owner are therefore
created in one transaction, and an Ownership Transfer demotes the old Owner and
promotes the new one in one transaction. `nest.db.ProjectStore` ([reference](api/db#projects-and-memberships)) applies these rules
for every caller:

- Creating a Project makes its creator the Owner.
- `owner` can be assigned only by the current Owner's transfer, never by adding a
  member, changing a role, or an invitation.
- The Owner cannot be removed or demoted; they transfer ownership first.
- Only the current Owner can transfer ownership or delete the Project. Deleting
  marks the Project `deleted_at`; its rows and job records are kept, and it no
  longer has members with access.
- Each change locks the Project row first, so concurrent changes to one Project
  apply one after another: of two racing transfers, the second finds its actor is
  no longer the Owner and fails. Writers that bypass the store are still stopped
  by the constraints.

**Creator, Owner and submitter are separate.** `projects.created_by_user_id`
records who created the Project, for audit only, and cannot be updated. The
current Owner is the `owner` membership. A job record's `project_id` and
`submitted_by_user_id` record the Run's Project and submitter. They are written
with the record, together or not at all, and a trigger rejects any later change,
so ownership transfers and membership changes never alter who submitted a Run.
Records written before authentication have neither. Projects and Users referenced
by job records cannot be deleted.

**Pending invitations are not memberships.** `project_email_invitations` holds an
invitation for a normalized email address, which needs no User row. At most one
invitation per address and Project is `pending`; its status (`pending`,
`accepted`, `declined`, `revoked`, `expired`) must match its timestamps. An
invitation's email delivery state is the `email_outbox` row whose `subject_ref` is
`invitation:<id>` (see [Send email with SMTP](email)), not a column here.
`project_invite_links` holds bounded, revocable Invite Link grants; its
`use_count` can never exceed `max_uses`, and `project_invitation_redemptions`
records each User's single redemption of a link. No invitation or link can grant
`owner`. Tokens are stored only as SHA-256 hashes.

**Sessions and other roles.** `user_sessions` stores browser and CLI sessions by
the hash of their secret, with an expiry and a revocation time; disabling a User
revokes all their sessions. Migration `0006_browser_session_rotation` makes each
row one refresh session in a *family* (`family_id`, capped by `family_expires_at`):
a refresh revokes the row as `rotated`, links it to its replacement
(`replaced_by_session_id`) and inserts the next row of the family. A revoked row
can never be restored, and a session's User, secret and family never change.
`session_access_tokens` holds the hashes of short-lived access tokens, each tied to
one session and deleted when that session is rotated or revoked. See
[Browser sessions](authentication#browser-sessions). Migration
`0007_cli_login_requests` adds `cli_login_requests`, the pending browser-approved
CLI sign-ins (hashes of their device and user codes, the requested scope, and the
decision, which moves only from `pending` to `approved` or `denied` and from
`approved` to `consumed`), and an immutable `scope` on `user_sessions`, set only for
`cli` sessions. See [Sign in from the command line](cli-login).
`platform_role_assignments` holds `platform_admin`,
and `organization_memberships` holds `org_admin` and `member`. Neither grants a
Project role, and no role grants Provider or HPC access. Migration
`0005_platform_admin_bootstrap` makes platform role assignments grant-or-revoke
only (their `granted_by_user_id` is cleared only by the foreign key when the
granting User is deleted; the audit log keeps the grantor), audits every change in `platform_audit_events`, and adds the one-time
`platform_bootstrap` record (see
[Bootstrap and recover platform administration](platform-admin)).

**Project audit history.** Migration `0008_project_audit` adds the append-only
`project_audit_events`, written by triggers on every Project creation, rename and
deletion and every membership addition, role change and removal, whoever makes
it. It also keeps a deleted Project deleted. See
[Audit history](projects#audit-history).

## Configure the connection

```bash
export NEST_DATABASE_URL="postgresql://nest@db.example.internal:5432/nest"
export NEST_DATABASE_PASSWORD_FILE=/run/secrets/nest-db-password
export NEST_DATABASE_SSLMODE=verify-full
export NEST_DATABASE_SSLROOTCERT=/run/secrets/db-ca.crt
```

`NEST_DATABASE_URL` is a [libpq connection URL](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNSTRING-URIS).
It must start with `postgresql://` and name the host and the database. Other libpq
parameters, such as `options=-csearch_path%3Dnest` or `target_session_attrs`, can
go in its query string. Its password and whole query string are always shown as
`***` in errors and logs. Prefer `NEST_DATABASE_PASSWORD_FILE`, or your platform's
secret injection, over a password inside the URL.

See [Environment variables and defaults](configuration#database) for every
setting. Each one is validated at startup together with the rest of the Controller
configuration, so a typo is reported with all other problems at once.

### TLS

Use `NEST_DATABASE_SSLMODE=verify-full` with `NEST_DATABASE_SSLROOTCERT` for any
database reached over a network. `verify-full` encrypts the connection and checks
both the server certificate and the host name. Setting only
`NEST_DATABASE_SSLROOTCERT` selects `verify-full` automatically.

| `sslmode` | Encrypted | Server verified | Use |
| --- | --- | --- | --- |
| `verify-full` | Yes | Certificate and host name | Recommended. |
| `verify-ca` | Yes | Certificate only | When the host name cannot match the certificate. |
| `require` | Yes | No | Only on a trusted network path. |
| `prefer` (libpq default), `allow`, `disable` | Not guaranteed | No | Local development only. |

The Controller logs a warning at startup when a non-loopback database uses a mode
that does not guarantee encryption. For client-certificate authentication, set
`NEST_DATABASE_SSLCERT` and `NEST_DATABASE_SSLKEY`; the key must not be readable
by other users (`chmod 600`). If `sslmode` is already in the URL, do not also set
`NEST_DATABASE_SSLMODE`; Nest rejects the duplicate so only one value can apply.

### Connection pool

The Controller keeps a pool of connections (psycopg's `ConnectionPool`): it opens
`NEST_DATABASE_POOL_MIN_SIZE` (1) connections at startup, grows to
`NEST_DATABASE_POOL_MAX_SIZE` (10) under load, checks each connection before
handing it out, and closes the pool on shutdown. An RPC that waits longer than
`NEST_DATABASE_POOL_TIMEOUT` (30 s) for a free connection fails with
`database_unavailable`. Each new connection may take up to
`NEST_DATABASE_CONNECT_TIMEOUT` (10 s).

Size the pool below the database's `max_connections`, minus what `nest-db`, backups
and other clients need. The Controller serves four RPCs at a time, so a maximum
above 4–5 per Controller is rarely useful. When several Controllers share a
database, or you use PgBouncer in transaction mode, count every Controller's
maximum.

### Database user

Create a dedicated role and database. The role that runs `nest-db upgrade` needs to
create tables in the target schema; the Controller then needs only `SELECT`,
`INSERT` and `UPDATE` on the Nest tables, plus `DELETE` on `project_memberships`,
`organization_memberships` and `platform_role_assignments` (removing a member or a
role deletes its row) and on `oidc_login_flows` (a finished or expired sign-in
deletes its row). For example:

```sql
CREATE ROLE nest LOGIN PASSWORD '...';
CREATE DATABASE nest OWNER nest ENCODING 'UTF8';
```

## Migrations

Nest keeps its whole schema in one ordered migration chain, packaged with Nest in
`nest/db/migrations/`. Each file is named `NNNN_description.sql`, numbered from
`0001` without gaps. `nest-db upgrade` applies pending files in order:

- Each migration runs in its own transaction together with its
  `schema_migrations` row, so a failing migration is rolled back completely and
  stops the run. Migrations applied before it stay applied.
- A PostgreSQL advisory lock is held for the whole run, so concurrent upgrades from
  several hosts or containers wait for each other instead of racing.
  `--lock-timeout` (60 s) bounds the wait.
- `schema_migrations` records each version's name and SHA-256 checksum. An applied
  migration is immutable: if its file is edited or renamed, `upgrade` and `verify`
  both refuse to continue.
- A database with a version this Nest does not know (it was migrated by a newer
  Nest) is rejected rather than used.

```console
$ nest-db upgrade
applied 0001_providers_and_jobs
Schema is at version 1 on postgresql://nest:***@db.example.internal:5432/nest
$ nest-db verify
Schema is current at version 1 on postgresql://nest:***@db.example.internal:5432/nest
$ nest-db status
applied  0001_providers_and_jobs
```

`nest-db` reads the same `NEST_DATABASE_*` settings as the Controller. In the
Controller image it is on the `PATH`; from a checkout use `uv run nest-db`.

| Exit status | Meaning |
| --- | --- |
| 0 | Success; for `verify` and `status`, the schema is current. |
| 1 | The schema is not current (pending, modified or unknown migrations), or a migration failed. |
| 2 | Invalid configuration, such as an unset `NEST_DATABASE_URL`. |
| 3 | The database is unreachable or rejected the connection. |

### Deploy or upgrade Nest

Migrations only add to the schema, so the order is always: back up, migrate,
then start the new Controller.

1. [Back up](#back-up-and-restore) the database.
2. Run `nest-db upgrade` with the **new** Nest version (new image or checkout).
3. Start or restart the Controller. It runs the equivalent of `nest-db verify`
   before serving.

With Compose:

```bash
docker compose build controller
docker compose run --rm --no-deps controller nest-db upgrade
docker compose up -d --wait
```

A Controller started against a database that is behind exits with status 3 and
logs `run nest-db upgrade`. Nest has no down-migrations. To roll back a Nest
release after migrating, restore the backup taken in step 1 and deploy the older
version.

### Add a migration

Every Nest component that needs tables, including future accounts and
authentication, adds files to this same chain. There is one `schema_migrations`
table and no per-component migration system or database.

1. Add `src/nest/db/migrations/NNNN_short_name.sql` with the next number.
2. Write plain SQL without `BEGIN`, `COMMIT` or `ROLLBACK`; the runner wraps the
   file in a transaction. Statements that cannot run in a transaction, such as
   `CREATE INDEX CONCURRENTLY`, are not supported.
3. Never edit, rename or renumber a migration that has been released. Fix mistakes
   in a new migration.
4. Prefer additive, backward-compatible changes (new tables, nullable columns) so
   the previous Controller keeps working between `nest-db upgrade` and restart.
5. Run the PostgreSQL tests (see [Develop Nest](developing)).

## Back up and restore

The Controller's database is the only state Nest keeps outside the HPC cluster.
Back it up with your provider's managed backups or point-in-time recovery, or with
`pg_dump`:

```bash
pg_dump --format=custom --no-owner --file=nest-$(date -u +%Y%m%dT%H%M%SZ).dump \
    "postgresql://nest@db.example.internal:5432/nest?sslmode=verify-full"
```

Use a `pg_dump` at least as new as the server. A dump taken while the Controller
runs is consistent. Store dumps encrypted; they contain job metadata such as
script paths and error messages, and personal data: Users' names and email
addresses, invited email addresses, and identity-provider subject IDs. They hold
no payloads or credentials; sessions and invitation tokens are stored only as
hashes.

To restore into an **empty** database:

```bash
docker compose stop controller            # or stop the native Controller
createdb --owner=nest nest_restored       # or have your provider create it
pg_restore --no-owner --role=nest --exit-on-error \
    --dbname="postgresql://nest@db.example.internal:5432/nest_restored" nest-20261115T120000Z.dump
NEST_DATABASE_URL=postgresql://nest@db.example.internal:5432/nest_restored nest-db verify
```

Then point `NEST_DATABASE_URL` at the restored database and start the Controller.
If `verify` reports pending migrations because the dump is from an older Nest, run
`nest-db upgrade` first. If it reports an unknown migration, the dump is from a
newer Nest: deploy that version instead. Restoring does not change anything on
the HPC cluster. Jobs submitted after the backup are missing from the restored
records, but are still in Slurm.

## Run PostgreSQL with Docker Compose

`compose.yaml` includes an optional PostgreSQL service for local development and
small self-hosted deployments. It starts **only** when you select the `local-db`
profile. The default `docker compose up -d` starts no database, so a Controller
there uses whatever external `NEST_DATABASE_URL` you configure, or none.

The profile is a convenience, not a production requirement. You take on the
database operations a managed service would otherwise do: backups, minor and
major PostgreSQL upgrades, and disk monitoring. The service:

- runs a pinned `postgres` image (`NEST_POSTGRES_IMAGE`), with a `pg_isready`
  health check;
- keeps its data in the named volume `<project>_postgres-data` (`nest_postgres-data`
  by default), which survives `docker compose down` and image updates. Only
  `docker compose down --volumes` or `docker volume rm` deletes it;
- reads the `nest` user's password from a file, mounted as a Compose secret. No
  password is set in `compose.yaml` or `.env`;
- publishes port 5432 on `127.0.0.1` only, for a native Controller, `nest-db`, or
  your own client on the same host. Containers in the project reach it as
  `postgres:5432`;
- has no TLS, so the interface is fixed: there is no setting that publishes it on
  another address. To reach it from another machine for maintenance, use an SSH
  tunnel (`ssh -L 5432:127.0.0.1:5432 host`). A Controller on another host needs a
  managed or separately operated PostgreSQL with `verify-full` TLS instead.
  Overriding `ports` in your own Compose file to publish it more widely sends
  passwords and job records unencrypted, and is not supported.

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_POSTGRES_IMAGE` | `postgres:17.12-bookworm` | Official PostgreSQL image. Override only with a reviewed version; changing the major version needs the [upgrade procedure](#upgrade-postgresql). |
| `NEST_POSTGRES_PASSWORD_FILE` | `${NEST_SSH_DIR}/nest-postgres-password` | Host file holding the password. It must exist before the first start; the password is fixed when the volume is initialized. |
| `NEST_POSTGRES_PORT` | `5432` | Published host port. Change it if another PostgreSQL already uses 5432. |
| `NEST_POSTGRES_DATA_DIR` | `/var/lib/postgresql/data` | Where the volume is mounted in the container. Change it only when a new major image moves its data directory. |

### Start the database

Create the password file once. Its default location is inside `NEST_SSH_DIR`, so a
Controller container sees the same file as `/home/nest/.ssh/nest-postgres-password`:

```bash
(umask 077; openssl rand -hex 32 > ssh/nest-postgres-password)
docker compose --profile local-db up -d --wait postgres
```

To use the database from a Controller container, add to `.env`:

```bash
NEST_DATABASE_URL=postgresql://nest@postgres:5432/nest
NEST_DATABASE_PASSWORD_FILE=/home/nest/.ssh/nest-postgres-password
NEST_DATABASE_REQUIRED=true
```

Then apply the migrations and start the Controller. The Controller never applies
migrations, and it does not wait for or start the database:

```bash
docker compose run --rm --no-deps controller nest-db upgrade
docker compose up -d --wait controller
```

The Controller logs a warning that the connection is not encrypted, because
`postgres` is not a loopback address. On the private Compose network that is
expected.

A native Controller or `nest-db` on the same host uses the published port
instead, with the same password file:

```bash
export NEST_DATABASE_URL=postgresql://nest@127.0.0.1:5432/nest
export NEST_DATABASE_PASSWORD_FILE=$PWD/ssh/nest-postgres-password
uv run nest-db upgrade
uv run python -m nest.controller
```

To open a SQL shell: `docker compose --profile local-db exec postgres psql -U nest`.

### Operate the services independently

The Controller and the database are separate services with no Compose dependency,
so you can start, stop, update or replace either one alone:

| Task | Command |
| --- | --- |
| Start or update only the database | `docker compose --profile local-db up -d --wait postgres` |
| Start or update only the Controller | `docker compose up -d --wait controller` |
| Stop only the Controller | `docker compose stop controller` |
| Stop only the database | `docker compose --profile local-db stop postgres` |
| Stop everything, keep the data | `docker compose --profile local-db down` |
| Delete the database and its data | `docker compose --profile local-db down --volumes` |

Include `--profile local-db` in any command that should act on the `postgres`
service, or set `COMPOSE_PROFILES=local-db` in `.env` so every `docker compose`
command includes it. While the database is stopped, a running Controller refuses
new Slurm requests with `database_unavailable` and reconnects once it is back; a
Controller that starts while the database is down exits with status 3, and
Compose's restart policy retries it.

### Upgrade Nest

The [Deploy or upgrade Nest](#deploy-or-upgrade-nest) steps apply unchanged, using
the profile's own `pg_dump` for the backup:

```bash
docker compose --profile local-db exec -T postgres \
    pg_dump -U nest -d nest --format=custom > nest-before-upgrade.dump
docker compose build controller
docker compose run --rm --no-deps controller nest-db upgrade
docker compose up -d --wait controller
```

### Upgrade PostgreSQL

**Minor versions** (for example 17.12 to 17.13) keep the on-disk format. Set the
new tag in `NEST_POSTGRES_IMAGE`, then recreate only the database:

```bash
docker compose --profile local-db pull postgres
docker compose --profile local-db up -d --wait postgres
```

**Major versions** (for example 17 to 18) cannot read the old data directory. A
new image started on the old volume refuses to start. Move the data with a dump
and restore into a fresh volume, keeping the old volume until the new one is
verified:

1. Stop the Controller and take a dump with the **old** server:

   ```bash
   docker compose stop controller
   docker compose --profile local-db exec -T postgres \
       pg_dump -U nest -d nest --format=custom > nest-pg17.dump
   ```

2. Stop the database and copy the old volume aside, so you can roll back:

   ```bash
   docker compose --profile local-db stop postgres
   docker volume create nest_postgres-data-pg17
   docker run --rm -v nest_postgres-data:/from:ro -v nest_postgres-data-pg17:/to \
       --entrypoint cp postgres:17.12-bookworm -a /from/. /to/
   docker compose --profile local-db rm -f postgres
   docker volume rm nest_postgres-data
   ```

3. Set the new image in `.env`, for example `NEST_POSTGRES_IMAGE=postgres:18.7-bookworm`.
   Read the image's release notes: official `postgres:18` and later images keep
   their data under `/var/lib/postgresql/18/docker` and expect the volume at
   `/var/lib/postgresql`, so also set `NEST_POSTGRES_DATA_DIR=/var/lib/postgresql`.
4. Start the new, empty server. It initializes the volume with the same password
   file. Restore the dump and verify the schema:

   ```bash
   docker compose --profile local-db up -d --wait postgres
   docker compose --profile local-db exec -T postgres \
       pg_restore -U nest -d nest --no-owner --exit-on-error < nest-pg17.dump
   docker compose run --rm --no-deps controller nest-db verify
   docker compose up -d --wait controller
   ```

5. Once the Controller works, delete the copy: `docker volume rm nest_postgres-data-pg17`.

To roll back, restore the old `NEST_POSTGRES_IMAGE` and remove
`NEST_POSTGRES_DATA_DIR` from `.env`, then put the copy back:

```bash
docker compose stop controller
docker compose --profile local-db rm -sf postgres
docker volume rm nest_postgres-data
docker volume create nest_postgres-data
docker run --rm -v nest_postgres-data-pg17:/from:ro -v nest_postgres-data:/to \
    --entrypoint cp postgres:17.12-bookworm -a /from/. /to/
docker compose --profile local-db up -d --wait postgres
docker compose run --rm --no-deps controller nest-db verify
docker compose up -d --wait controller
```

The volume names above assume the default project name `nest`; `docker volume ls`
shows yours. Starting the new image on the old volume, with either data
directory, fails with an error rather than touching the old data. The database is
initialized with the `C.UTF-8` locale, so the image's operating-system version
does not change how text sorts. `tests/smoke_compose_postgres_upgrade.sh` runs this
whole procedure, including the rollback.

### Back up and restore the Docker-managed database

The volume holds a live data directory. Copying it while PostgreSQL runs does not
give a usable backup, so use `pg_dump`. The commands run inside the container, so
the client always matches the server version. A dump taken while the Controller
runs is consistent:

```bash
docker compose --profile local-db exec -T postgres \
    pg_dump -U nest -d nest --format=custom > nest-$(date -u +%Y%m%dT%H%M%SZ).dump
```

Run it from cron or a systemd timer, keep copies off the host, and store them
encrypted. To check a backup, or to restore, load it into an empty database next
to the current one, verify it, and switch the Controller over:

```bash
docker compose stop controller
docker compose --profile local-db exec -T postgres createdb -U nest nest_restored
docker compose --profile local-db exec -T postgres \
    pg_restore -U nest -d nest_restored --no-owner --exit-on-error < nest-20261115T120000Z.dump
NEST_DATABASE_URL=postgresql://nest@postgres:5432/nest_restored \
    docker compose run --rm --no-deps controller nest-db verify
```

Then set `NEST_DATABASE_URL=postgresql://nest@postgres:5432/nest_restored` in
`.env` and run `docker compose up -d --wait controller`. To restore under the
original name instead, drop it first, with the Controller stopped:
`docker compose --profile local-db exec -T postgres dropdb -U nest --force nest`,
then `createdb -U nest nest` and the same `pg_restore` into `nest`. The notes in
[Back up and restore](#back-up-and-restore) about older and newer dumps apply.

## Troubleshooting

| Startup error | Fix |
| --- | --- |
| `Cannot connect to PostgreSQL at ...: Connection refused` | Check the host and port, and that the Controller's network can reach the database. |
| `password authentication failed` | Check the user and `NEST_DATABASE_PASSWORD_FILE`. |
| `server does not support SSL, but SSL was required` | Enable TLS on the server, or lower `NEST_DATABASE_SSLMODE` for local development. |
| `certificate verify failed` | `NEST_DATABASE_SSLROOTCERT` must contain the CA that signed the server certificate. With `verify-full`, the URL's host must match the certificate. |
| `The database has no Nest schema; run nest-db upgrade` | Run `nest-db upgrade` against this database. |
| `The schema is at version N but this Nest needs M` | Run `nest-db upgrade` with this Nest version. |
| `Migration ... was changed after it was applied` | Restore the original migration file; never edit applied migrations. |
| `Migration ... was renamed to ... after it was applied` | Restore the original file name; `nest-db status` marks it `RENAMED`. |
| `The database has migration ..., which this Nest version does not have` | Deploy the newer Nest that migrated this database, or restore a matching backup. |
