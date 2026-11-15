# Store Controller records in PostgreSQL

A Controller can keep its control-plane records in PostgreSQL: the Provider Profiles
it is configured with and every Slurm job it submits. The records survive Controller
restarts, upgrades, and host moves.

The database is **externally managed** and **opt-in**:

- You provide the PostgreSQL server and an empty database, for example a managed
  cloud database or your organization's PostgreSQL cluster, and pass its URL in
  `NEST_DATABASE_URL`. Nest never starts a server, never creates a database, and
  never falls back to SQLite, files, or memory.
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
results, or credentials are stored.

**If the database fails while the Controller runs**, a new Slurm request is refused
before any side effect, with gRPC status `UNAVAILABLE` and error kind
`database_unavailable` (`nest.errors.DatabaseUnavailableError`). Nothing was
submitted, so the call is safe to retry. Updates after a job was submitted are
logged as errors instead, and the RPC still returns its result, because the
job cannot be un-submitted. The pool reconnects on its own once the database is
back.

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
`INSERT` and `UPDATE` on the Nest tables. For example:

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
script paths and error messages, though no payloads or credentials.

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

## Try it locally with Compose

`compose.postgres-dev.yaml` adds a throwaway PostgreSQL container and a one-shot
`migrate` service that runs `nest-db upgrade` before the Controller starts. It is
**only for local development**: the database has no TLS, a fixed default
password, and lives in a Docker volume.

```bash
docker compose -f compose.yaml -f compose.postgres-dev.yaml up -d --wait
docker compose -f compose.yaml -f compose.postgres-dev.yaml exec postgres \
    psql -U nest -c 'SELECT id, kind, status, slurm_job_id FROM jobs'
docker compose -f compose.yaml -f compose.postgres-dev.yaml down --volumes  # deletes the data
```

In production, use `compose.yaml` (or another Controller definition) with
`NEST_DATABASE_URL` pointing at your managed database. TLS and password files go
in `NEST_SSH_DIR`, so they appear under `/home/nest/.ssh` in the container, for
example `NEST_DATABASE_SSLROOTCERT=/home/nest/.ssh/db-ca.crt`.

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
