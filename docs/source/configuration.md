# Environment variables and defaults

Nest is configured only through environment variables. The same names work for a
native process (`python -m nest.controller`), Docker Compose, and Dokploy. No project
checkout, YAML or JSON configuration file, or `.env` file is required: Compose reads
`.env` only as a convenience, and a native process reads only its own environment.
Credentials can be given directly in the environment or as files; see
[Credential sources](#credential-sources). They never belong in the repository.

Every entry point reads settings through `nest.config`. The Controller validates the
whole environment before it starts listening and reports **every** problem at once,
each naming the variable to change, then exits with status 2 (status 3 means a
configured database is unreachable or not migrated):

```text
ERROR nest.controller pid=7 request_id=- Invalid Nest configuration:
  - NEST_SSH_TIMEOUT must be a positive number of seconds, got '0'
  - Slurm execution requires: NEST_SLURM_HOST, NEST_SLURM_WORKDIR (or define HPC Platforms with NEST_PLATFORMS)
```

An empty value means unset. Error messages never repeat secret values
(`NEST_DATABASE_PASSWORD`, `NEST_SSH_PRIVATE_KEY`, `NEST_TLS_KEY`,
`NEST_DATABASE_SSLKEY_DATA`, `NEST_SMTP_PASSWORD`, `NEST_GOOGLE_CLIENT_SECRET`, the
password and query of `NEST_DATABASE_URL`). The
Controller logs a redacted `Controller configuration {...}` line at startup, and
its log handler masks inline passwords and keys if any message ever contains them.

## Credential sources

Every credential and every file a tool needs can come from either a direct
environment value or a file. The two forms of a pair are mutually exclusive:

| Direct value | File form | Kind |
| --- | --- | --- |
| `NEST_DATABASE_PASSWORD` | `NEST_DATABASE_PASSWORD_FILE` | One-line secret |
| `NEST_GOOGLE_CLIENT_SECRET` | `NEST_GOOGLE_CLIENT_SECRET_FILE` | One-line secret |
| `NEST_SMTP_PASSWORD` | `NEST_SMTP_PASSWORD_FILE` | One-line secret |
| `NEST_TLS_CERT`, `NEST_TLS_KEY` | `NEST_TLS_CERT_FILE`, `NEST_TLS_KEY_FILE` | PEM |
| `NEST_DATABASE_SSLROOTCERT_DATA`, `NEST_DATABASE_SSLCERT_DATA`, `NEST_DATABASE_SSLKEY_DATA` | `NEST_DATABASE_SSLROOTCERT`, `NEST_DATABASE_SSLCERT`, `NEST_DATABASE_SSLKEY` | PEM |
| `NEST_SMTP_CA` | `NEST_SMTP_CA_FILE` | PEM |
| `NEST_SSH_PRIVATE_KEY` | `NEST_SSH_IDENTITY_FILE` | PEM or OpenSSH key |
| `NEST_SSH_KNOWN_HOSTS_DATA` | `NEST_SSH_KNOWN_HOSTS` | `known_hosts` lines |
| `NEST_SSH_CONFIG` (container, Mode A) | `config` in the mounted `NEST_SSH_DIR` | SSH configuration |
| `NEST_PLATFORMS_ENV` | The `NEST_PLATFORM*` variables, or a Compose `env_file` | `NAME=value` lines |

The naming follows one rule: the direct form drops `_FILE`. A path setting named
after a libpq or OpenSSH option has no `_FILE`, so its direct form adds `_DATA`.

- **Unset and empty** values, including only spaces, mean unset.
- **Both forms set** is an error naming both variables; the value is never shown.
- **Files** must be absolute, readable, UTF-8 and not empty. A private key file
  must not be readable by group or others.
- **One-line secrets** are trimmed. A line break inside one is an error. Any
  other character is allowed: the database password goes to libpq as its own
  keyword, never spliced into `NEST_DATABASE_URL`.
- **Multiline values** accept real line breaks or the two characters `\n`.
  Carriage returns are dropped and one final newline is added. Inline PEM must
  contain the matching `BEGIN CERTIFICATE` or `PRIVATE KEY` marker.

Tools that only read files get private generated copies. libpq's TLS files and
Mode B SSH files go into a new `0700` directory under the temporary directory
(tmpfs in the Compose deployments), and the container entrypoint writes Mode A SSH
files into the tmpfs `~/.ssh` (see
[Deploy on Dokploy from environment variables](dokploy#how-each-credential-is-delivered)).
gRPC TLS and the SMTP CA are passed as bytes, so they are never written. SSH,
`scp` and local workers start without any of these variables in their
environment.

A direct value is visible to anyone who can inspect the process or container
environment. Prefer the file form where a secret-file mount is available; see
[Security trade-offs](dokploy#security-trade-offs).

## Client

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_ADDRESS` | `127.0.0.1:50051` | Controller `host:port` for each remote call. No remote Controller is ever a default. The hosted Controller's verified address is its Tailscale MagicDNS name, `nest-controller.tail8e5ba1.ts.net:50051`; see [below](#why-the-hosted-controller-is-not-a-default). |
| `NEST_RPC_TIMEOUT` | `900` | Seconds allowed for each `Execute` RPC. Must be a positive number. When it expires, the call raises `RpcTimeoutError`. |
| `NEST_CONTROLLER_TLS` | `false` for loopback, `true` when a CA file is set | Use a TLS channel with server certificate verification. Accepts `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`. |
| `NEST_CONTROLLER_TLS_CA_FILE` | Unset | Absolute path to PEM roots for the Controller's certificate. With TLS enabled, an unset file uses gRPC's default trust roots. |
| `NEST_PROJECT_ID` | Unset | ID of the Project your calls run in. Required by a Controller with session authentication (`NEST_CONTROLLER_AUTH=session`); sent as `x-nest-project-id` metadata. |

Set these in the process running your Nest application. They do not configure
where the Controller listens. Invalid client settings raise `nest.config.ConfigError`
(a `ValueError`) from `.remote()`.

### Command-line sign-in

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_AUTH_URL` | Unset: the CLI uses `https://auth.nest.cerulean.works`, and `.remote()` uses it only for the hosted Controller | Origin of your Nest's sign-in, such as `https://nest.example.com` (the origin of `NEST_GOOGLE_REDIRECT_URL`). Must be `https://` (`http://` only for localhost), with no path, credentials, query or fragment. Read by `nest login`, `nest auth`, `nest logout`, `nest platforms` and [`login.ensure_session`]; `--url` overrides it. When set, `.remote()` sends that Nest's stored session to the Controller, whatever its address, as a bearer token. |
| `NEST_CLI_CREDENTIALS_FILE` | `$XDG_CONFIG_HOME/nest/credentials.json`, or `~/.config/nest/credentials.json` | Absolute path where CLI sessions are kept, one per sign-in origin, mode `0600`. A file other users can read is refused. Holds Nest sessions only, never HPC credentials. |

See [Sign in from the command line](cli-login).

### Sign-in origin

The CLI and the SDK resolve the sign-in origin the same way
([`config.resolve_auth_url`]), first match wins:

| Order | Source | CLI (`nest ...`) | SDK (`.remote()`, [`client.session_metadata`]) |
| --- | --- | --- | --- |
| 1 | `--url` | yes | not applicable |
| 2 | `NEST_AUTH_URL` | yes | yes, for any Controller |
| 3 | Hosted default, `https://auth.nest.cerulean.works` | yes | only when `NEST_CONTROLLER_ADDRESS` is exactly `nest.cerulean.works:50051` (`nest.config.HOSTED_CONTROLLER_ADDRESS`); not for the MagicDNS name |
| - | None | never | no session is sent and no sign-in starts |

Hosted users who connect by the MagicDNS name therefore set
`NEST_AUTH_URL=https://auth.nest.cerulean.works` explicitly; without it the SDK
sends no session and the call fails with `AuthenticationRequiredError`.

An empty or blank value counts as unset. An invalid `--url` or `NEST_AUTH_URL` is
an error (exit status 2, or `ConfigError`); it never falls back to the hosted
default. `nest auth status --json` reports the origin as `auth_url` and where it
came from as `auth_url_source` (`--url`, `NEST_AUTH_URL` or `hosted default`).

Sessions are stored per origin, so a session is only ever sent to the origin that
issued it and to the Controllers that origin applies to. Signing in to, refreshing
at or signing out of one Nest never replaces, revokes or deletes the session of
another. A credentials file written by an earlier SDK, holding one session, is
still read.

To opt out of hosted sign-in:

- **Self-hosted Nest:** set `NEST_AUTH_URL` (or pass `--url`) to your Nest's
  origin. Nothing contacts the hosted origin.
- **Local or stateless Controller** (`NEST_CONTROLLER_AUTH=none`): leave
  `NEST_AUTH_URL` unset and point `NEST_CONTROLLER_ADDRESS` anywhere but the hosted
  Controller, or leave it at its loopback default. The SDK sends no session and
  never starts a sign-in, even if you signed in to hosted Nest.

Controller deployment settings never depend on these defaults: the Controller's
own sign-in origin comes only from `NEST_GOOGLE_REDIRECT_URL`.

### Why the hosted Controller is not a default

The hosted Controller serves plaintext gRPC on port 50051; only the
access-controlled Tailnet encrypts it (see
[Share a Controller over Tailscale](tailscale-shared)), and every call carries a
bearer token. The SDK cannot tell whether a machine is on that Tailnet, and
outside it a name may resolve to an address that is not the Controller, so a
default would risk sending sessions in plaintext over an untrusted network. Until
the hosted Controller serves gRPC with TLS, hosted users set all of these
explicitly; the SDK never defaults TLS to `false`:

```bash
export NEST_AUTH_URL="https://auth.nest.cerulean.works"
export NEST_CONTROLLER_ADDRESS="nest-controller.tail8e5ba1.ts.net:50051"
export NEST_CONTROLLER_TLS=false
export NEST_PROJECT_ID="<project-id>"
```

`nest-controller.tail8e5ba1.ts.net` is the Controller node's MagicDNS name, which
was verified end to end (sign-in, Project and Platform grants, Provider approval,
Slurm submission and result retrieval). `nest.cerulean.works:50051`, the address
built into the SDK as `nest.config.HOSTED_CONTROLLER_ADDRESS`, has not been verified
end to end in this deployment; do not rely on it for gRPC. Use the MagicDNS name
rather than a Tailscale IP: a shared node's IP is assigned per Tailnet and is not
guaranteed to be the same for every recipient. `NEST_PROJECT_ID` stays explicit as
well: Nest never chooses a Project.

For a non-loopback address, set `NEST_CONTROLLER_TLS=true` or provide a CA file
to use TLS. Set `NEST_CONTROLLER_TLS=false` explicitly for a plaintext deployment
on a trusted private network such as an authorized Tailnet.

## Controller

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_BIND` | `127.0.0.1:50051` | Listener `host:port`. |
| `NEST_EXECUTOR` | `local` | `local` runs calls in a subprocess; `slurm` submits them to the default Provider Profile. Other values are rejected. |
| `NEST_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. The Controller rejects other values; workers fall back to `INFO`. |
| `NEST_SSH_TIMEOUT` | `60` | Seconds allowed for each SSH command or file transfer. When it expires, the call raises `SSHTimeoutError`, or `SubmissionUnknownError` during `sbatch`. |
| `NEST_RESULT_TIMEOUT` | `600` | Seconds the Controller polls a worker job. When it expires, the call raises `QueueTimeoutError` or `ExecutionTimeoutError`. |
| `NEST_ENVIRONMENT_TIMEOUT` | `1800` | Seconds the Controller polls an environment build job. When it expires, the same timeout classes are raised. |
| `NEST_POLL_INTERVAL` | `5` | Seconds between Slurm status queries. |
| `NEST_PENDING_BLOCKED_TIMEOUT` | `120` | Seconds a job may stay pending for a reason it cannot outwait, such as `PartitionConfig`, `InvalidQOS`, a per-job limit, or a hold, before the call raises `JobBlockedError`. `0` stops at the first such observation. Set it above `NEST_RESULT_TIMEOUT` to wait for the full deadline instead. |
| `NEST_PENDING_LIMIT_TIMEOUT` | Unset | Seconds a job may stay pending on a QoS or association usage limit, such as `MaxGRESPerAccount`, before the call raises `JobBlockedError`. `0` stops at the first such observation. Unset waits until `NEST_RESULT_TIMEOUT`, since such limits clear as the account's other jobs finish. |
| `NEST_CANCEL_BLOCKED_JOBS` | `false` | Whether to `scancel` a job when the call raises `JobBlockedError`. The job is never resubmitted. |
| `NEST_TLS_CERT_FILE` | Unset | Absolute path to the PEM certificate chain. Set with a key to serve TLS. |
| `NEST_TLS_KEY_FILE` | Unset | Absolute path to the PEM private key. A certificate and a key, or neither. |
| `NEST_TLS_CERT` | Unset | The certificate chain inline, instead of `NEST_TLS_CERT_FILE`. |
| `NEST_TLS_KEY` | Unset | The private key inline, instead of `NEST_TLS_KEY_FILE`. A secret: passed to gRPC as bytes, never written to disk or logged. |
| `NEST_CONTROLLER_AUTH` | `session` with `NEST_DATABASE_URL`, otherwise `none` | `session` authenticates every gRPC call with a Nest CLI session and authorizes it with the Project policy (see [Security and limitations](security#authenticated-execution)). Needs `NEST_DATABASE_URL`. `none` serves every caller that can reach the listener. |
| `NEST_CONTROLLER_ALLOW_PLAINTEXT` | `false` | With `NEST_CONTROLLER_AUTH=session`, a non-loopback listener needs TLS (`NEST_TLS_*`) because every call carries a session token. `true` approves plaintext gRPC on a network that is already encrypted and access-controlled, such as an authorized Tailnet. Ignored with `NEST_CONTROLLER_AUTH=none`. |
| `NEST_EXECUTION_REGISTRY` | `required` with session authentication, else `legacy` | `required` admits every Slurm submission through the durable Execution Registry, which needs `NEST_CONTROLLER_AUTH=session` and, for a Mode A Platform, its `SSH_USER`. `legacy` keeps the trusted Mode A path that records only `jobs`; it is refused for a Mode B Platform under session authentication. See [Admit submissions through the Execution Registry](execution-registry). |
| `NEST_PROVIDER_ALLOWED_PROJECTS` | Unset (no Project) | Startup-only Provider approval, used only with `NEST_PROVIDER_APPROVALS=environment`: Project IDs, comma-separated, whose members' Runs the Controller's Provider accepts, or `*` for every Project. Every job runs as the Controller's HPC account, so this is the operator's approval; no Project role grants it. Read only at start: after a change, recreate the Controller container (in Dokploy, **Deploy** again). Needs `NEST_CONTROLLER_AUTH=session`. It is never imported into the database by itself; see [Move Provider approvals to the database](project-onboarding#move-provider-approvals-to-the-database). |
| `NEST_PROVIDER_APPROVALS` | `environment` while `NEST_PROVIDER_ALLOWED_PROJECTS` is set, otherwise `database` | Where Provider approvals come from. `database` reads them from PostgreSQL at every submission (`nest-db provider`, or the [privileged management API](privileged-api)), so they change without a redeploy, on every Controller sharing the database; registry admission checks the approval again in the transaction that records the attempt. `environment` keeps `NEST_PROVIDER_ALLOWED_PROJECTS`. `database` together with `NEST_PROVIDER_ALLOWED_PROJECTS` is refused at start. Needs `NEST_CONTROLLER_AUTH=session`. |
| `NEST_PUBLIC_URL` | Unset | Advertised `grpc://`, `grpcs://`, `http://` or `https://` URL, logged at startup. Must not contain credentials or a query string. With email enabled it is also the base of links in messages, so it is required and must be `https://` (`http://` only for `localhost`). |

The four timing settings accept positive numbers, including fractions.

### HPC Platforms

An **HPC Platform** is one Slurm cluster that the operator connects Nest to. Its
**Platform Profile** is the operator's record for it: a permanent Platform ID, the
operator's label, the approved login nodes, the SSH mode and user, and the
Platform's Slurm defaults. Profiles are bootstrapped from environment variables
alone; users never supply SSH configuration. The Controller connects only to a
Platform's approved login nodes, using the first one.

These are kept apart on purpose:

- The **Platform ID** is the Platform's identity. It namespaces Slurm accounts and
  job records: the account `physics` on Platform `campus` and the account `physics`
  on Platform `lab` are different accounts. Never reuse an ID for a different
  cluster; with a database, the Controller refuses to start if a recorded Platform
  ID is given a different operator label.
- The **operator** label (`NEST_PLATFORM_<ID>_OPERATOR`) says who runs the cluster.
  It is metadata only, so several Platforms can share one operator.
- **Login nodes** are endpoints. Each `host[:port]` may be approved for only one
  Platform. Host names are compared without case, and an omitted port counts as
  22, so `login.example.edu` and `login.example.edu:22` are the same endpoint.
- The **Slurm account**, partition and time limit are per-Platform defaults.

**Several Platforms** use `NEST_PLATFORMS`, a comma-separated list of Platform IDs
(lowercase letters, digits and `_`, starting with a letter, at most 32), and
`NEST_PLATFORM_<ID>_*` settings with the ID in upper case:

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_PLATFORMS` | Unset | Platform IDs, comma-separated. |
| `NEST_DEFAULT_PLATFORM` | The first ID | The Platform the Controller uses for execution. Must be listed in `NEST_PLATFORMS`. |
| `NEST_PLATFORM_<ID>_LOGIN_NODES` | Required | Approved login nodes, comma-separated `host[:port]`. |
| `NEST_PLATFORM_<ID>_ACCOUNT` | Required | Default Slurm account on this Platform. |
| `NEST_PLATFORM_<ID>_WORKDIR` | Required with `NEST_EXECUTOR=slurm` | Absolute shared HPC directory. |
| `NEST_PLATFORM_<ID>_SSH_MODE` | `A` | `A` (trusted operator SSH) or `B` (managed SSH); see below. |
| `NEST_PLATFORM_<ID>_SSH_USER` | Unset | Remote user. Required in Mode B. |
| `NEST_PLATFORM_<ID>_PARTITION` | `h200_256gpu` | Default Slurm partition. |
| `NEST_PLATFORM_<ID>_TIME` | `00:05:00` | Default `sbatch --time`. |
| `NEST_PLATFORM_<ID>_OPERATOR` | Unset | Operator label: lowercase letters, digits, `.`, `-` and `_`, at most 64. Input is lowercased. |
| `NEST_PLATFORM_<ID>_NAME` | Unset | Display name, 1 to 100 printable characters. |

```bash
NEST_EXECUTOR=slurm
NEST_PLATFORMS=campus,lab
NEST_DEFAULT_PLATFORM=campus
NEST_PLATFORM_CAMPUS_OPERATOR=example-university
NEST_PLATFORM_CAMPUS_NAME=Campus GPU cluster
NEST_PLATFORM_CAMPUS_SSH_MODE=B
NEST_PLATFORM_CAMPUS_LOGIN_NODES=login1.example.edu,login2.example.edu:2222
NEST_PLATFORM_CAMPUS_SSH_USER=svc-nest
NEST_PLATFORM_CAMPUS_ACCOUNT=physics
NEST_PLATFORM_CAMPUS_PARTITION=gpu
NEST_PLATFORM_CAMPUS_TIME=02:00:00
NEST_PLATFORM_CAMPUS_WORKDIR=/scratch/svc-nest/nest
NEST_PLATFORM_LAB_OPERATOR=example-university
NEST_PLATFORM_LAB_LOGIN_NODES=lab-hpc
NEST_PLATFORM_LAB_ACCOUNT=physics
NEST_PLATFORM_LAB_WORKDIR=/home/svc-nest/nest
NEST_SSH_KNOWN_HOSTS=/run/secrets/nest_known_hosts
NEST_SSH_IDENTITY_FILE=/run/secrets/nest_ssh_key
```

Validation is strict, so a typo cannot silently drop a setting: a
`NEST_PLATFORM_*` variable that names no listed Platform or no field above is an
error, as is any `NEST_PLATFORM_*` or `NEST_DEFAULT_PLATFORM` without
`NEST_PLATFORMS`. `NEST_PLATFORMS` cannot be combined with `NEST_PROVIDERS`,
`NEST_PROVIDER_<ID>_*` or `NEST_SLURM_*`. The one exception is
`NEST_SLURM_SSH_MODE=B` on its own, which `compose.managed-ssh.yaml` always sets:
it is ignored, with a startup warning, because each Platform sets its own mode.
SSH credentials (`NEST_SSH_*`) are Controller-wide and are not per-Platform
settings.

With Docker Compose, put the Platform settings in an env file; see
[Deploy with Docker Compose](docker-compose#several-hpc-platforms). Where no file can
be mounted, put the same lines in one variable, `NEST_PLATFORMS_ENV` (real line
breaks or `\n`). Its values are literal, only the Platform names above are
accepted, and a name also set as its own variable is an error. The RPC API has
no Platform field yet, so every call uses the default Platform.

**One cluster** can still use the `NEST_SLURM_*` variables, which define the
Platform `default`. This is the form Compose passes through from `.env`:

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_SLURM_HOST` | Unset | Login node or Mode A SSH alias. Same as a one-entry `NEST_SLURM_LOGIN_NODES`; set only one. |
| `NEST_SLURM_LOGIN_NODES` | Unset | Approved login nodes, comma-separated `host[:port]`. |
| `NEST_SLURM_SSH_MODE` | `A` | `A` (trusted operator SSH) or `B` (managed SSH); see below. |
| `NEST_SLURM_SSH_USER` | Unset | Remote user. Required in Mode B; in Mode A it overrides the SSH config. |
| `NEST_SLURM_ACCOUNT` | Unset | Default Slurm account. |
| `NEST_SLURM_PARTITION` | `h200_256gpu` | Slurm partition for the team allocation. Set this to a partition available to the configured account for other deployments. |
| `NEST_SLURM_TIME` | `00:05:00` | Default `sbatch --time`, in a Slurm format such as `30`, `00:05:00` or `1-00:00:00`. Environment builds keep their script's limit. |
| `NEST_SLURM_WORKDIR` | Unset | Absolute shared HPC directory containing `project/`, `images/`, `environments/` and `jobs/`. |

With `NEST_EXECUTOR=slurm`, a login node, account and work directory are required.
In local mode a login node and account are enough to enable the job status and
cancel RPCs.

#### Migrating from Compute Provider settings

Earlier releases called a Platform a **Compute Provider** and used
`NEST_PROVIDERS`, `NEST_DEFAULT_PROVIDER` and `NEST_PROVIDER_<ID>_*`. These still
work unchanged and define the same Platform Profiles, with the same IDs, so job
records and database rows carry over. They are **deprecated**: the Controller logs
a warning at startup. To migrate, rename the variables, keeping every ID:

| Deprecated | Replacement |
| --- | --- |
| `NEST_PROVIDERS` | `NEST_PLATFORMS` |
| `NEST_DEFAULT_PROVIDER` | `NEST_DEFAULT_PLATFORM` |
| `NEST_PROVIDER_<ID>_<FIELD>` | `NEST_PLATFORM_<ID>_<FIELD>` |

Then optionally add `NEST_PLATFORM_<ID>_OPERATOR` and `_NAME`; the deprecated form
does not read them. `NEST_PROVIDER_ALLOWED_PROJECTS` is unrelated and keeps its
name. Single-cluster `NEST_SLURM_*` settings are not deprecated. With a database,
run `nest-db upgrade` first: migration `0011_platform_metadata` adds the operator and
display-name columns (see [Store Controller records in PostgreSQL](database)).

In Python, `nest.config.ProviderProfile` is an alias of
[`config.PlatformProfile`], `ControllerConfig.providers`, `default_provider` and
`provider` are aliases of `platforms`, `default_platform` and `platform`, and
`nest.db.sync_providers` is an alias of `nest.db.sync_platforms`. The startup
configuration summary reports `platforms` and `default_platform` (with each
Platform's `operator` and `name`) in place of `providers` and `default_provider`;
the `controller.started` audit record adds `default_platform` and keeps
`default_provider` with the same value.

### SSH modes

**Mode A, trusted operator SSH**, is the original deployment. The Controller runs
`ssh -o BatchMode=yes [-p port] [-l user] <host>`, so the host is resolved through
the Controller user's SSH configuration: aliases, `ProxyCommand`, and the optional
Tailscale sidecar ([Deploy with Docker Compose](docker-compose)) all work. That
configuration must keep `StrictHostKeyChecking yes`.

**Mode B, managed SSH**, builds the whole command line from the environment:

```text
ssh -F none -o BatchMode=yes -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile=<known_hosts> -o GlobalKnownHostsFile=<known_hosts>
    -o IdentitiesOnly=yes -o IdentityAgent=none -o ForwardAgent=no
    -o ClearAllForwardings=yes -i <key> -p <port, default 22> -l <user> <login node>
```

`-F none` skips every SSH configuration file, so no alias, proxy, or weakened
host-key setting can change the connection. Mode B never reads a `NEST_TAILSCALE_*`
setting. The login node must be reachable from the Controller's network directly.
This holds for every Platform in Mode B, whichever form defines it.

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_SSH_MODES_ALLOWED` | `A,B` | Modes Platforms may use. `B` forbids Mode A Platforms. |
| `NEST_SSH_KNOWN_HOSTS` | Unset | Absolute path to verified host keys (Mode B). |
| `NEST_SSH_KNOWN_HOSTS_DATA` | Unset | The same host keys inline, one `known_hosts` line per line or separated by `\n`. Host keys are public. |
| `NEST_SSH_IDENTITY_FILE` | Unset | Absolute path to the private key (Mode B). Must not be readable by group or others. |
| `NEST_SSH_PRIVATE_KEY` | Unset | The private key inline, with real line breaks or `\n`. A secret: in Mode B it is written to a private temporary file at startup; it is never logged. |
| `NEST_SSH_CONFIG` | Unset | Container only, Mode A: the SSH configuration inline. The entrypoint writes it to `~/.ssh/config`, `NEST_SSH_PRIVATE_KEY` to `~/.ssh/id_nest` and `NEST_SSH_KNOWN_HOSTS_DATA` to `~/.ssh/known_hosts`, all `0600` in tmpfs. It never replaces a different file from the mounted `NEST_SSH_DIR`, and it is refused if it weakens host-key checking. A native Controller uses its user's own `~/.ssh`. |

Mode B needs one host-key source and one key source; set only one of each pair.
Prefer files or your platform's secret store for the key. An inline key is
visible to anyone who can inspect the process or container environment.
In Mode A, `NEST_SSH_PRIVATE_KEY` and `NEST_SSH_KNOWN_HOSTS_DATA` are used only
together with `NEST_SSH_CONFIG`.

### Database

PostgreSQL persistence is opt-in. Without `NEST_DATABASE_URL` the Controller is
stateless. With it, the Controller verifies the database and its schema before
serving and exits with status 3 if either check fails; it never falls back to
running without the database. See [Store Controller records in PostgreSQL](database)
for TLS, pooling, migrations, and backups.

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_DATABASE_URL` | Unset (stateless) | Externally managed PostgreSQL, such as `postgresql://nest@db.example.internal:5432/nest`. Must name the host and database. A secret: its password and query string are always redacted. |
| `NEST_DATABASE_REQUIRED` | `false` | `true` makes an unset `NEST_DATABASE_URL` a configuration error. |
| `NEST_DATABASE_PASSWORD` | Unset | The password directly. A secret: passed to libpq as its own keyword, so any character is safe, and never logged. Set only one of the URL password, this, and `NEST_DATABASE_PASSWORD_FILE`. |
| `NEST_DATABASE_PASSWORD_FILE` | Unset | Absolute path to a file holding the password, on one line. Do not also put a password in the URL. |
| `NEST_DATABASE_SSLMODE` | URL's `sslmode`, else libpq's `prefer` | One of `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`. Use `verify-full` for remote databases. `verify-full` when only `NEST_DATABASE_SSLROOTCERT` is set. |
| `NEST_DATABASE_SSLROOTCERT` | Unset | Absolute path to the CA bundle that verifies the server certificate. |
| `NEST_DATABASE_SSLCERT`, `NEST_DATABASE_SSLKEY` | Unset | Client certificate and key, set together. The key must not be readable by group or others. |
| `NEST_DATABASE_SSLROOTCERT_DATA`, `NEST_DATABASE_SSLCERT_DATA`, `NEST_DATABASE_SSLKEY_DATA` | Unset | The same PEM files inline. libpq reads only files, so Nest writes them to a private `0700` directory (`0600` files) each time it connects. The key is a secret. Same rules as the file forms, including `verify-full` when a CA is given. |
| `NEST_DATABASE_POOL_MIN_SIZE` | `1` | Connections kept open. `0` or more. |
| `NEST_DATABASE_POOL_MAX_SIZE` | `10` | Most connections the pool opens. At least 1 and at least the minimum. |
| `NEST_DATABASE_CONNECT_TIMEOUT` | `10` | Seconds to establish each connection. |
| `NEST_DATABASE_POOL_TIMEOUT` | `30` | Seconds an RPC waits for a free connection before failing with `database_unavailable`. |

Settings may not appear both in the URL query and as a variable, and
`NEST_DATABASE_*` settings without `NEST_DATABASE_URL` are rejected rather than
ignored. `nest-db` reads the same variables.

### Email delivery

Email is opt-in. Unless `NEST_EMAIL_ENABLED=true`, none of the other settings below
are read or validated, and the Controller never contacts an SMTP server. When it is
enabled, every required setting is checked before the Controller starts, and an
insecure transport is never chosen silently. See [Send email with SMTP](email) for
TLS, credential rotation, the outbox and retries.

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_EMAIL_ENABLED` | `false` | `true` turns on email delivery. Requires `NEST_DATABASE_URL`, `NEST_EMAIL_FROM`, `NEST_PUBLIC_URL` and `NEST_SMTP_HOST`. |
| `NEST_EMAIL_PROVIDER` | `smtp` | Transport. Only `smtp` is supported. |
| `NEST_EMAIL_FROM` | Unset | Verified sender, such as `Nest <nest@example.com>`: the `From` header and envelope sender of every message. Exactly one address. |
| `NEST_SMTP_HOST` | Unset | SMTP server host name. Its certificate must be valid for this name. |
| `NEST_SMTP_PORT` | `587` (`465` with implicit TLS) | SMTP server port. |
| `NEST_SMTP_STARTTLS` | `true` | Upgrade the connection with STARTTLS, failing if the server does not offer it. |
| `NEST_SMTP_IMPLICIT_TLS` | `false` | Use TLS from the first byte instead (SMTPS). Only one of the two TLS settings may be `true`; neither is an error, because Nest never sends mail unencrypted. |
| `NEST_SMTP_USERNAME` | Unset | Login name. Unset for a relay that needs no authentication. Needs a password. |
| `NEST_SMTP_PASSWORD_FILE` | Unset | Absolute path to a file holding the password, such as a mounted secret. Re-read for each connection, so rotation needs no restart. Must not be empty. |
| `NEST_SMTP_PASSWORD` | Unset | The password inline. A secret: never logged or echoed. Set only one of the two password settings. |
| `NEST_SMTP_CA_FILE` | Unset (system roots) | Absolute path to a PEM CA bundle for a relay with a private CA. Certificate verification is always on. |
| `NEST_SMTP_CA` | Unset | The same CA bundle inline. Set only one of the two. |
| `NEST_SMTP_TIMEOUT` | `30` | Seconds for the connection and each SMTP command. |
| `NEST_EMAIL_MAX_ATTEMPTS` | `5` | Delivery attempts per message, 1 to 100. |
| `NEST_EMAIL_RETRY_DELAY` | `60` | Seconds before the first retry after a transient failure; doubles per attempt. |
| `NEST_EMAIL_RETRY_MAX_DELAY` | `3600` | Longest wait between attempts. At least `NEST_EMAIL_RETRY_DELAY`. |
| `NEST_EMAIL_POLL_INTERVAL` | `15` | Seconds between outbox scans. Newly queued messages are sent without waiting. |

### Google sign-in

Google sign-in is opt-in and enabled by `NEST_GOOGLE_CLIENT_ID`. Without it none of
the settings below are read and no HTTP listener is opened; setting any of them
without the client ID is an error. When enabled, the Controller serves
`/auth/google/login`, the redirect URL's path, the `/auth/session` routes and the
`/auth/cli/` routes of [command-line sign-in](cli-login) on `NEST_AUTH_HTTP_BIND`. See
[Sign in with Google](authentication) for the OAuth client, the reverse proxy, and
the checks applied to each sign-in.

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_GOOGLE_CLIENT_ID` | Unset (disabled) | OAuth client ID of a Google **Web application** client, such as `1234-example.apps.googleusercontent.com`. ID tokens must name it as their audience. Requires `NEST_DATABASE_URL`, a client secret and `NEST_GOOGLE_REDIRECT_URL`. |
| `NEST_GOOGLE_CLIENT_SECRET_FILE` | Unset | Absolute path to a file holding the client secret, such as a mounted secret. Re-read for each code exchange, so rotation needs no restart. Must not be empty. |
| `NEST_GOOGLE_CLIENT_SECRET` | Unset | The client secret inline. A secret: never logged or echoed. Set only one of the two secret settings. |
| `NEST_GOOGLE_REDIRECT_URL` | Unset | The callback URL registered with the OAuth client, such as `https://nest.example.com/auth/google/callback`. Must be `https://` (`http://` only for `localhost` and `127.0.0.1`), have a path other than `/`, `/auth/google/login`, the `/auth/session` routes and `/auth/cli/...`, and contain no credentials, query string or fragment. The auth listener serves its path; `https://` also makes cookies `Secure`. |
| `NEST_AUTH_HTTP_BIND` | `127.0.0.1:8080` | `host:port` of the plain-HTTP auth listener. Put a TLS-terminating reverse proxy in front of it. |
| `NEST_AUTH_ACCESS_TOKEN_TTL` | `900` | Seconds a browser or CLI access token is accepted. The session lifetimes apply to [CLI sessions](cli-login#sessions) too. 60 to 3600. See [Browser sessions](authentication#browser-sessions). |
| `NEST_AUTH_REFRESH_TTL` | `86400` | Seconds a refresh session stays usable without being refreshed. Each refresh rotates it and starts this period again. At least `NEST_AUTH_ACCESS_TOKEN_TTL`. |
| `NEST_AUTH_SESSION_MAX_LIFETIME` | `604800` | Seconds after sign-in when the session ends however often it is refreshed; the User then signs in again. At least `NEST_AUTH_REFRESH_TTL`, at most 7776000 (90 days). |

### Platform administrator bootstrap

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL` | Unset | The Google account that becomes the first `platform_admin` at its first sign-in with a verified address. Takes effect once: after the bootstrap is recorded in the database, changing or unsetting it grants and removes nothing. Requires `NEST_DATABASE_URL` and Google sign-in (`NEST_GOOGLE_CLIENT_ID`). Never logged. See [Bootstrap and recover platform administration](platform-admin). |

### Reserved settings

These names are part of the contract for features that do not exist yet. The
Controller refuses to start when one is set, rather than ignoring it:

| Variable | Planned use |
| --- | --- |
| `NEST_AUTH_TOKEN_FILE` | Static service tokens. Callers authenticate with Nest sessions (`NEST_CONTROLLER_AUTH`). |
| `NEST_RETENTION_DAYS` | Retention of job records and staged files. |
| `NEST_INTEGRATIONS` | Optional integrations, comma-separated. |

Direct backend users can set [`backends.slurm.SlurmConfig`] instead. Review the
deployed batch scripts for memory and cluster-specific settings.

## Docker Compose

Compose uses the same contract. Each Controller definition passes an explicit list
of the variables above (all Controller settings except `NEST_PROVIDERS`, the
`NEST_PROVIDER_<ID>_*` family, the reserved settings and the pending-job timeouts)
so unrelated host variables never reach the Controller. Unset values become empty,
which means unset. The container listens on `0.0.0.0:50051` and defaults to Slurm
mode. Once `NEST_GOOGLE_CLIENT_ID` is set, `NEST_AUTH_HTTP_BIND` defaults to
`0.0.0.0:8080` inside the container; publish or route that port to your
TLS-terminating proxy. For several Platforms, use a `platforms.env` file or
`NEST_PLATFORMS_ENV`.

`compose.dokploy.yaml` is the environment-only deployment: no host file is mounted,
the Tailscale Auth Key comes from `NEST_TAILSCALE_AUTHKEY` and an optional Serve
configuration from `NEST_TAILSCALE_SERVE_CONFIG` (both reach only the sidecar), and
the `local-db` and `migrate` profiles add PostgreSQL and `nest-db upgrade`. See
[Deploy on Dokploy from environment variables](dokploy).

These variables configure Compose itself:

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_IMAGE` | `nest-controller:local` | Local tag assigned to the image built from this checkout. |
| `NEST_CONTROLLER_HOST_IP` | `127.0.0.1` | Host interface for the published gRPC port. Use the host's Tailscale IPv4 address for Tailnet clients. |
| `NEST_CONTROLLER_PORT` | `50051` | Published host port; the container always listens on port 50051. |
| `NEST_SSH_DIR` | `./ssh` | Mode A: existing host directory containing the SSH configuration, key, and verified host keys. The container uses a private copy at `/home/nest/.ssh`, so in-container paths such as `/home/nest/.ssh/tls.key` work for the file settings above. |

The optional `local-db` profile's PostgreSQL service also reads
`NEST_POSTGRES_IMAGE`, `NEST_POSTGRES_PASSWORD_FILE`, `NEST_POSTGRES_PORT` and
`NEST_POSTGRES_DATA_DIR`; see
[Run PostgreSQL with Docker Compose](database#run-postgresql-with-docker-compose).
The Controller never receives them.

See [Deploy with Docker Compose](docker-compose) for setup and operating commands.

**Mode B without host mounts.** `compose.managed-ssh.yaml` removes the SSH
directory mount and defaults `NEST_SLURM_SSH_MODE` to `B`:

```bash
export NEST_SLURM_LOGIN_NODES=login1.example.edu NEST_SLURM_SSH_USER=svc-nest
export NEST_SLURM_ACCOUNT=physics NEST_SLURM_WORKDIR=/scratch/svc-nest/nest
export NEST_SSH_KNOWN_HOSTS_DATA="$(cat known_hosts)"
export NEST_SSH_PRIVATE_KEY="$(cat id_ed25519)"
docker compose -f compose.yaml -f compose.managed-ssh.yaml up -d
```

**Optional Mode A Tailnet transport.** `compose.tailscale.yaml` also accepts the
following. They configure only the sidecar; the Controller never receives them, and
neither Mode B nor the SDK needs them.

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_TAILSCALE_AUTHKEY_FILE` | `./secrets/tailscale-authkey` | Private host file containing an official Tailscale Auth Key; mounted only into the sidecar as a Compose Secret. Dokploy: `../files/tailscale/authkey`. |
| `NEST_TAILSCALE_HOSTNAME` | `nest-controller` | Sidecar's name in your personal Tailnet. |
| `NEST_TAILSCALE_IMAGE` | `tailscale/tailscale:v1.104.4` | Official sidecar image, pinned to a version; may be overridden with a reviewed version or digest. |

The sidecar uses userspace SOCKS5 on `tailscale:1055`, a persistent state volume,
and `tag:nest-controller`. Configure the SSH alias using the supplied template
and restrict that tag to the Login Node's SSH port in your Tailnet policy.

The image healthcheck runs `python -m nest.healthcheck`, which reads the same
`NEST_CONTROLLER_BIND` and `NEST_TLS_*` settings.

## Deadlines and resources

| Operation | Default | Setting | Error when exceeded |
| --- | --- | --- | --- |
| Client `Execute` RPC | 900 seconds | `NEST_RPC_TIMEOUT` (client) | `RpcTimeoutError` |
| SSH commands and file transfers | 60 seconds | `NEST_SSH_TIMEOUT` | `SSHTimeoutError`; `SubmissionUnknownError` during `sbatch` |
| Worker job polling | 600 seconds | `NEST_RESULT_TIMEOUT` | `QueueTimeoutError` if the job never started, otherwise `ExecutionTimeoutError` |
| Environment build polling | 1,800 seconds | `NEST_ENVIRONMENT_TIMEOUT` | `QueueTimeoutError` or `ExecutionTimeoutError` |
| Job pending for a reason it cannot outwait | 120 seconds | `NEST_PENDING_BLOCKED_TIMEOUT` | `JobBlockedError` |
| Job pending on a QoS or association limit | Polling deadline | `NEST_PENDING_LIMIT_TIMEOUT` | `JobBlockedError` |
| Slurm job time limit | `00:05:00` | `NEST_SLURM_TIME` or `NEST_PROVIDER_<ID>_TIME` | `ExecutionTimeoutError` (Slurm state `TIMEOUT`) |
| Environment build time limit | `00:10:00` | `#SBATCH --time` in `scripts/environment-smoke.sbatch` | `ExecutionTimeoutError` (Slurm state `TIMEOUT`) |

The client RPC deadline covers the whole call: environment preparation, queueing,
execution, and download. The Controller deadlines are counted separately, so the
default environment build deadline is longer than the client's. A long build can
end with `RpcTimeoutError` while the Controller keeps waiting. Raise
`NEST_RPC_TIMEOUT` or lower the Controller deadlines if you need the classified
Controller error. Status queries and downloads can extend each polling deadline by
up to one SSH timeout.

No timeout cancels a submitted Slurm job, and Nest never retries a submission.
`JobBlockedError` cancels the job only when `NEST_CANCEL_BLOCKED_JOBS` is `true`.
[Handle remote errors](remote-errors) describes each error and when it is safe to
call again.

### Resources

`@nest.remote(gpu=..., cpu=...)` defaults to `gpu=0`, `cpu=1`. The Slurm
Controller passes `gpu` to the worker job as `--gpus-per-node` and `cpu` as
`--cpus-per-task`. A count of `0` uses the worker script's `#SBATCH` directive
(one CPU in `scripts/worker.sbatch`). The worker exposes GPUs to the function only
when `gpu` is positive; see [Run functions on GPUs](gpus). Environment builds use the resource
counts and time limit in the deployed `scripts/environment-smoke.sbatch`.
Use `gpu=1` for a smoke test on the team's Nano4 partition.
Negative counts are rejected before anything is submitted. The local Controller
does not allocate resources.

For deployment steps, see [Deploy a Slurm Controller](deploy); for local
execution, see [Run a local Controller](local-controller).

## Logging and troubleshooting

The Controller and worker commands write logs to **stderr** at `INFO` by default.
Set `NEST_LOG_LEVEL` to `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` before
starting the Controller. The Controller rejects invalid values at startup; worker
commands warn and fall back to `INFO`.
`DEBUG` adds file sizes, SSH timings and runtime readiness checks; it does not
print serialized calls or dependency file contents.

```bash
NEST_LOG_LEVEL=DEBUG uv run python -m nest.controller 2>controller.log
```

Command log lines include a UTC timestamp, severity, module, process ID and
`request_id`. Execution stages also record elapsed milliseconds. Startup and
shutdown messages use `request_id=-` because they are outside any request.

### Enable client logs

Importing Nest does not configure application logging. In your client program,
configure the `nest` logger before calling `.remote()`:

```python
import logging
import nest

handler = logging.StreamHandler()
handler.setFormatter(
    logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s",
        defaults={"request_id": "-"},
    )
)
logger = logging.getLogger("nest")
logger.setLevel(logging.INFO)  # Use DEBUG for additional detail.
logger.addHandler(handler)
logger.propagate = False
```

`NEST_LOG_LEVEL` configures the Controller and worker commands. The SDK follows
your application's Python logging configuration. Neither import nor the command
logging setup replaces the application's root logger handlers.

### Follow an execution

1. Find the `request_id` in the client's `Remote execution started` message.
2. Search the Controller log for that ID. RPC entries show the method and elapsed
   time. Errors include the original exception traceback; rejected RPCs show their
   gRPC status. Older clients without an ID receive a new one in the Controller log.
3. For Slurm execution, find `Execution scheduled` to map the request to its
   `job_id`, `execution_id`, `environment_id` and `job_dir`. Environment preparation
   separately records its build job ID and cache hits or misses.
4. Inspect `worker-<job_id>.out` or `env-<build_job_id>.out` in
   `NEST_SLURM_WORKDIR`. These filenames come from the deployed batch scripts.
   The worker and isolated user interpreter receive the request ID and log level
   explicitly, including when Apptainer clears the host environment.

Deploy the updated Python runtime **and** `scripts/worker.sbatch` together to
preserve the request ID and log level across Slurm workers. Existing three-argument
worker script invocations still work; their worker generates its own request ID.

Slurm polling records the initial state and later state changes at `INFO`, so an
unchanged queue state does not flood the log. A timeout records the job ID; it does
not cancel the job. A completed worker or RPC means a result was produced or
transported: a user function exception can still be returned as
[`errors.RemoteExecutionError`], with an error log at the worker and client.

Logs record metadata rather than function arguments, return values, serialized
payloads or the contents of `pyproject.toml` and `uv.lock`. Exception messages,
tracebacks and remote command error output can contain application-specific data.
