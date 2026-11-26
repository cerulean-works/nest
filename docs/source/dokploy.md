# Deploy on Dokploy from environment variables

`compose.dokploy.yaml` runs a complete M1 Controller (PostgreSQL persistence and
migrations, Google sign-in with Nest sessions, Project and Platform authorization,
the Execution Registry, Tailscale, and Mode A or Mode B SSH) with every setting
and credential entered in Dokploy's **Environment** panel. You do not mount a
static configuration or credential file. Durable state lives in named volumes
that survive redeploys.

The file-based deployments ([Docker Compose](docker-compose),
[Share a Controller over Tailscale](tailscale-shared)) are unchanged and remain
supported. Choose a file-based deployment when your platform has a secret-file
mount and you want credentials kept out of process environments (see
[Security trade-offs](#security-trade-offs)).

## What runs

| Service | Started | Purpose | State |
| --- | --- | --- | --- |
| `controller` | Always | gRPC on `:50051`, Google sign-in on `:8080` when enabled. Read-only filesystem; `/tmp` and `~/.ssh` are tmpfs. | None. Regenerated from the environment at every start. |
| `tailscale` | Always | Tailscale Serve forwards `nest-controller:50051` on your Tailnet to the Controller; SOCKS5 `tailscale:1055` for Mode A SSH through the Tailnet. | `tailscale-state` volume: the node identity and keys. |
| `postgres` | `local-db` profile | PostgreSQL 17 for this deployment only, with no host port. | `postgres-data` volume. |
| `migrate` | `migrate` profile | Runs `nest-db upgrade` once before the Controller starts. With no pending migration it changes nothing. | None. |

The Controller waits for the sidecar to be healthy and, when their profiles are
enabled, for PostgreSQL to be healthy and for `migrate` to finish successfully.
A failed migration stops the deployment before the Controller starts.

## Configure Dokploy

1. Create a **Compose** application from this repository with **Compose Type:
   Docker Compose** and **Compose Path: `./compose.dokploy.yaml`**. Use this file
   alone; it is self-contained because Dokploy rewrites YAML before Compose
   resolves it. Compose must support secrets read from environment variables
   (`secrets.<name>.environment`, Compose v2.23 and later).
2. Enable **Isolated Deployments**. Add no host ports. Add a Domain only for the
   Google sign-in listener (step 6).
3. Paste the variables below into **Environment**. Dokploy writes them to the
   `.env` file next to the Compose file, and Compose passes only the variables
   each service lists. Unlisted variables reach no container.
4. Before the first deploy of this file, and whenever you change
   `NEST_GRPC_ORIGIN_SUBNET`, run the read-only preflight on the Dokploy host. The
   Controller always joins the internal `grpc-origin` network. Its default subnet
   (`172.31.250.0/28`) can overlap a network that already exists, and then the
   deploy fails at network creation:

   ```bash
   python3 scripts/nest-deploy-doctor.py preflight --project <appName> --env-file .env
   ```

   On a failure, set `NEST_GRPC_ORIGIN_SUBNET` and `NEST_GRPC_GATEWAY_ADDRESS` as
   [Preflight and recover a Dokploy deployment](deployment-recovery#before-a-deploy-preflight)
   describes.
5. Deploy. Check the logs of `migrate` and `controller`, then run the checks in
   [Verify](#verify). If the deploy waits on `migrate` and does not finish, run
   `python3 scripts/nest-deploy-doctor.py migrate-status --project <appName>` and
   follow [Recover a stuck migrate](deployment-recovery#recover-a-stuck-migrate).
   Never remove the `migrate` profile to get past a failure.
6. For Google sign-in, add a Dokploy **Domain** for the `controller` service on
   container port `8080` with HTTPS, and register
   `https://<that domain>/auth/google/callback` with the OAuth client. gRPC is never
   routed through Traefik; it stays on the Tailnet.

### Environment values

Values are literal when you wrap them in single quotes. Without quotes, or in
double quotes, Compose expands `$NAME`, so a password containing `$` must be
single-quoted. For multiline values (keys, certificates, `known_hosts`, SSH
configuration, Platform lists) either keep the real line breaks inside double
quotes, or put the value on one line in single quotes and write each line break as
the two characters `\n`. Nest converts `\n` back to a line break in those values
and strips carriage returns.

```ini
# --- Profiles: a local PostgreSQL and automatic migrations ---------------------
COMPOSE_PROFILES='local-db,migrate'

# --- Database -----------------------------------------------------------------
# With local-db the server is `postgres`. For an external server, use its URL with
# NEST_DATABASE_SSLMODE='verify-full' and NEST_DATABASE_SSLROOTCERT_DATA, and drop
# local-db from COMPOSE_PROFILES.
NEST_DATABASE_URL='postgresql://nest@postgres:5432/nest'
NEST_DATABASE_PASSWORD='generate-with: openssl rand -hex 32'
NEST_DATABASE_REQUIRED='true'

# --- Google sign-in and the first platform administrator -----------------------
NEST_GOOGLE_CLIENT_ID='1234-example.apps.googleusercontent.com'
NEST_GOOGLE_CLIENT_SECRET='GOCSPX-...'
NEST_GOOGLE_REDIRECT_URL='https://nest.example.com/auth/google/callback'
NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL='admin@example.com'
# NEST_AUTH_HTTP_BIND defaults to 0.0.0.0:8080 in this file once a client ID is set.
# Optional: what the Console's "Connect to Project" page tells users to set
# (see "Environment variables"). Leave NEST_CONSOLE_HOSTED_PROFILE unset (off).
# NEST_CONSOLE_CONTROLLER_ADDRESS='nest-controller.example.ts.net:50051'
# NEST_CONSOLE_CONTROLLER_TLS='false'

# --- Execution -----------------------------------------------------------------
NEST_EXECUTOR='slurm'
# Tailscale encrypts and access-controls the gRPC path, so session tokens may
# travel without Nest's own TLS. Otherwise set NEST_TLS_CERT and NEST_TLS_KEY.
NEST_CONTROLLER_ALLOW_PLAINTEXT='true'
# Provider approvals. Prefer durable ones (no redeploy; see "Onboard a Project"):
# leave NEST_PROVIDER_ALLOWED_PROJECTS unset and approve with `nest-db provider approve`.
# Or the startup-only list of Project UUIDs (not names), comma-separated:
# NEST_PROVIDER_ALLOWED_PROJECTS='<project-uuid-1>,<project-uuid-2>'

# --- HPC Platforms: the lines of a platforms.env file ---------------------------
NEST_PLATFORMS_ENV='NEST_PLATFORMS=campus\nNEST_PLATFORM_CAMPUS_NAME=Campus GPU cluster\nNEST_PLATFORM_CAMPUS_OPERATOR=example-university\nNEST_PLATFORM_CAMPUS_LOGIN_NODES=hpc\nNEST_PLATFORM_CAMPUS_SSH_USER=svc-nest\nNEST_PLATFORM_CAMPUS_ACCOUNT=physics\nNEST_PLATFORM_CAMPUS_WORKDIR=/home/svc-nest/nest'

# --- Mode A SSH (trusted operator SSH through the Tailnet) ----------------------
# Written to ~/.ssh/config, ~/.ssh/id_nest and ~/.ssh/known_hosts in a tmpfs.
NEST_SSH_CONFIG='Host hpc\n    HostName login.YOUR-TAILNET.ts.net\n    User svc-nest\n    IdentityFile ~/.ssh/id_nest\n    IdentitiesOnly yes\n    BatchMode yes\n    StrictHostKeyChecking yes\n    UserKnownHostsFile ~/.ssh/known_hosts\n    ProxyCommand /usr/bin/nc -X 5 -x tailscale:1055 %h %p'
# The key file's contents, with \n for each line break (or double-quoted lines).
NEST_SSH_PRIVATE_KEY='<contents of the dedicated HPC key>'
NEST_SSH_KNOWN_HOSTS_DATA='login.YOUR-TAILNET.ts.net ssh-ed25519 AAAA...'

# --- Tailscale sidecar ------------------------------------------------------------
# A tagged, non-ephemeral Auth Key. It is used only while tailscale-state is
# empty; after the first enrollment you may revoke it and set this to ''.
NEST_TAILSCALE_AUTHKEY='tskey-auth-...'
NEST_TAILSCALE_HOSTNAME='nest-controller'
```

For **Mode B** (managed SSH), set the Platform's `SSH_MODE=B` and its approved
`LOGIN_NODES` in `NEST_PLATFORMS_ENV`, keep `NEST_SSH_PRIVATE_KEY` and
`NEST_SSH_KNOWN_HOSTS_DATA`, and leave `NEST_SSH_CONFIG` unset. Mode B ignores
every SSH configuration file and the Tailnet proxy; the login nodes must be reachable
directly. See [SSH modes](configuration#ssh-modes).

For a single cluster you can use the `NEST_SLURM_*` variables instead of
`NEST_PLATFORMS_ENV`. Every setting in
[Environment variables and defaults](configuration) that the Controller lists is
passed through.

The Tailscale policy, tags, sharing and verification steps are the same as for the
file-based shared deployment: follow
[Share a Controller over Tailscale](tailscale-shared) from *Sharing eligibility and
quarantine* onwards, using `-f compose.dokploy.yaml` in its commands. The reviewed
`docker/tailscale-serve/serve.json` from the checkout is used unless you set
`NEST_TAILSCALE_SERVE_CONFIG` to a replacement JSON document. Use single quotes,
so Compose leaves `${TS_CERT_DOMAIN}` for the sidecar to expand.

## Change a setting such as Provider approval

Dokploy injects the **Environment** panel through Compose: it writes the panel
to `.env` next to `compose.dokploy.yaml`, and Compose substitutes each
`${NAME:-}` that a service lists. The Controller reads its settings once, at
start. To change one, for example to approve a new Project in
`NEST_PROVIDER_ALLOWED_PROJECTS`:

1. Edit the value in **Environment** and save.
2. Press **Deploy**. This recreates the containers whose configuration changed;
   the named volumes, including `tailscale-state` and `postgres-data`, are kept.
   Restarting a container from Dokploy's container view, or `docker restart`,
   keeps the environment the container was created with, so the old value stays
   in effect.
3. Check what the running Controller received, in the `controller` terminal:

   ```bash
   printenv NEST_PROVIDER_ALLOWED_PROJECTS
   nest-db audit list --event controller.started --limit 1
   ```

   `allowed_projects` in the start record is the number of listed Projects, or
   `*`. A value that is not a UUID stops the Controller at start with a
   configuration error naming the variable; read the `controller` logs.

A variable that `compose.dokploy.yaml` does not list never reaches the
Controller, even if it is in the panel. With durable Provider approvals
(`NEST_PROVIDER_APPROVALS=database`, the default once
`NEST_PROVIDER_ALLOWED_PROJECTS` is unset) approving a Project needs no
redeploy at all. See
[Onboard a Project](project-onboarding#1-approve-the-project-for-the-provider).

## How each credential is delivered

| Credential | Direct environment value | File alternative (other Compose files, native) | Delivered to | Generated file |
| --- | --- | --- | --- | --- |
| Database password | `NEST_DATABASE_PASSWORD` | `NEST_DATABASE_PASSWORD_FILE` | Controller, `migrate`; PostgreSQL as a Compose secret file | None. libpq receives it as a keyword, never inside the URL. |
| Database CA, client certificate, client key | `NEST_DATABASE_SSLROOTCERT_DATA`, `_SSLCERT_DATA`, `_SSLKEY_DATA` | `NEST_DATABASE_SSLROOTCERT`, `_SSLCERT`, `_SSLKEY` | Controller, `migrate` | `/tmp/nest-db-tls-*/` (`0700`, files `0600`), rewritten each start. |
| Google client secret | `NEST_GOOGLE_CLIENT_SECRET` | `NEST_GOOGLE_CLIENT_SECRET_FILE` | Controller | None. |
| SMTP password, SMTP CA | `NEST_SMTP_PASSWORD`, `NEST_SMTP_CA` | `NEST_SMTP_PASSWORD_FILE`, `NEST_SMTP_CA_FILE` | Controller | None. |
| gRPC TLS certificate and key | `NEST_TLS_CERT`, `NEST_TLS_KEY` | `NEST_TLS_CERT_FILE`, `NEST_TLS_KEY_FILE` | Controller | None. gRPC reads the PEM bytes directly. |
| SSH private key | `NEST_SSH_PRIVATE_KEY` | `NEST_SSH_IDENTITY_FILE` (Mode B), or `id_*` in `NEST_SSH_DIR` (Mode A) | Controller | Mode B: `/tmp/nest-ssh-*/id`. Mode A: `~/.ssh/id_nest`. Both `0600`. |
| SSH `known_hosts` | `NEST_SSH_KNOWN_HOSTS_DATA` | `NEST_SSH_KNOWN_HOSTS` (Mode B), or `known_hosts` in `NEST_SSH_DIR` (Mode A) | Controller | Mode B: `/tmp/nest-ssh-*/known_hosts`. Mode A: `~/.ssh/known_hosts`. |
| SSH configuration (Mode A) | `NEST_SSH_CONFIG` | `config` in `NEST_SSH_DIR` | Controller | `~/.ssh/config` (`0600`). |
| HPC Platform Profiles | `NEST_PLATFORMS_ENV` | `NEST_PLATFORMS_ENV_FILE` (Compose `env_file`), or the variables themselves | Controller | None. |
| Tailscale Auth Key | `NEST_TAILSCALE_AUTHKEY` | `NEST_TAILSCALE_AUTHKEY_FILE` (other Compose files) | Sidecar only, as a Compose secret file | `/run/secrets/tailscale_authkey`. |
| Tailscale Serve JSON | `NEST_TAILSCALE_SERVE_CONFIG` | `NEST_TAILSCALE_SERVE_DIR` (other Compose files), or the checkout's `serve.json` | Sidecar only | `/run/nest-serve/serve.json` (`0600`, tmpfs). |

The generated files are written by Nest itself, byte for byte, with no shell, so
`$`, quotes and backticks are never interpreted. Each file is created exclusively
(an existing file or symlink is never replaced) with its final mode set before
any content is written. Mode A files go into the tmpfs copy of `~/.ssh`. The
entrypoint writes them as root before dropping to the `nest` user. They are only
written when `NEST_SSH_CONFIG` is set, so mounted-directory and Mode B deployments
behave exactly as before. A file copied from a mounted `NEST_SSH_DIR` is never
overwritten: a different value for the same file stops the container with an error
naming the file. Inline SSH configuration that sets `StrictHostKeyChecking` to
anything but `yes`, or a known-hosts file of `/dev/null`, is refused. Host-key and
TLS verification are never weakened.

## Value rules

These rules apply to every pair of direct and file settings:

- **Unset or empty.** An unset variable, an empty value and a value of only spaces
  all mean unset. Compose passes every listed variable, so an empty entry is
  normal.
- **Both set.** Setting the direct and the file form of the same credential is a
  configuration error naming both variables. Neither value is ever shown.
- **Files.** The path must be absolute and name a readable UTF-8 file. An empty
  file is an error. A private key or database client key file must not be
  readable by group or others.
- **One-line secrets.** Passwords, the Google client secret and the Tailscale key
  are trimmed of surrounding spaces. A line break inside one is an error. A
  password may contain any other character, including `@ : / ? # % & = ' " \ ;`
  and spaces.
- **Multiline content.** Keys, certificates, `known_hosts`, SSH configuration and
  Platform lists accept real line breaks or `\n`. Carriage returns are dropped,
  surrounding blank lines are trimmed, and one final newline is added. Inline PEM
  values must contain the matching `BEGIN CERTIFICATE` or `PRIVATE KEY` marker.
- **Platform lists.** `NEST_PLATFORMS_ENV` holds `NAME=value` lines. Blank lines,
  `#` comments and a leading `export` are ignored. One pair of surrounding quotes
  is removed and nothing is expanded. Only `NEST_PLATFORMS`,
  `NEST_DEFAULT_PLATFORM` and `NEST_PLATFORM_<ID>_<FIELD>` are accepted. A name
  that is also set as its own variable, or that appears twice, is an error.

`load_controller_config` (Controller), `load_database_config` (`nest-db`, including
`migrate`) and the healthcheck apply these rules in one place, `nest.config`.

## Persistent storage

| Data | Where | Why it must persist |
| --- | --- | --- |
| PostgreSQL data: Users, Projects, Platforms, Slurm accounts, the Execution Registry, sessions, audit log | `postgres-data` volume, or your external database | All M1 state, including executions that are still running and their recovery records. |
| Tailscale node identity and preferences | `tailscale-state` volume | Losing it enrolls a new node with a new address and drops existing shares. Never copy it to a second running instance. |
| Operator-supplied credentials (database password, keys, certificates, client secret) | Dokploy Environment (`.env` on the Dokploy host) | Static: re-read at every start. Generated copies live in tmpfs and disappear with the container. |

M1 has no dynamically managed SSH keys. The Mode B key is a static operator
credential, so no key rotation state needs a volume. Execution and recovery state
is in PostgreSQL. Keep the Dokploy application name and volume settings unchanged:
a new project name selects new, empty volumes. Do not delete the application's
volumes, or run `docker compose down --volumes`, unless you intend to erase the
database and the node identity.

PostgreSQL reads `NEST_DATABASE_PASSWORD` only when it first initializes an empty
`postgres-data` volume. To change the password later, run `ALTER ROLE nest PASSWORD
'...'` in the `postgres` terminal, then update the variable and redeploy.

## Verify

In Dokploy's terminals (or `docker compose -f compose.dokploy.yaml -p YOUR_APP ...`
on the host):

```bash
# controller, as user nest: the schema, generated SSH files and the listener
nest-db verify
ls -l ~/.ssh && ssh -G hpc | grep -E '^(hostname|stricthostkeychecking) '
python -m nest.healthcheck
# sidecar: enrolled with the expected tag and Serve mapping
tailscale status --json | head -n 20
tailscale serve status --json
```

```bash
# host: the migration finished, nothing is stuck, the network settings are consistent
python3 scripts/nest-deploy-doctor.py migrate-status --project <appName>
python3 scripts/nest-deploy-doctor.py preflight --project <appName> --env-file .env
```

Then follow *Start, migrate state, and verify* in
[Share a Controller over Tailscale](tailscale-shared) for the Tailnet and denial
checks, and [Sign in with Google](authentication) for the first sign-in.

## Compatibility matrix

| Setting family | `compose.yaml` / `compose-nop.yaml` | `+ compose.managed-ssh.yaml` | `compose.tailscale.yaml` | `compose.tailscale-shared.yaml` | `compose.dokploy.yaml` | Native process |
| --- | --- | --- | --- | --- | --- | --- |
| Mounted SSH directory (`NEST_SSH_DIR`) | Yes (default) | No | Yes | Yes | No | Not applicable |
| `NEST_SSH_CONFIG` (+ key, `known_hosts`) for Mode A | Yes | Not applicable (Mode B) | Yes | Yes | Yes | No: uses your own `~/.ssh` |
| Mode B inline key and `known_hosts` | Yes | Yes | Yes | Yes | Yes | Yes |
| `NEST_DATABASE_PASSWORD` / `_FILE` | Yes / yes | Yes / yes | Yes / yes | Yes / yes | Yes / yes (in-container path) | Yes / yes |
| Inline database TLS (`*_DATA`) | Yes | Yes | Yes | Yes | Yes | Yes |
| Inline gRPC TLS (`NEST_TLS_CERT`, `NEST_TLS_KEY`) | Yes | Yes | Yes | Yes | Yes | Yes |
| Google sign-in (direct or file secret) | Yes; publish `8080` yourself | Yes | Yes | Yes | Yes, through a Dokploy Domain | Yes |
| `NEST_PLATFORMS_ENV` / `platforms.env` file | Yes / yes | Yes / yes | Yes / yes | Yes / yes | Yes / optional | Yes / not applicable |
| Tailscale key: environment / file | No sidecar | No sidecar | No / yes | No / yes | Yes / no | Not applicable |
| Serve JSON: environment / file | No sidecar | No sidecar | No Serve | No / yes | Yes / checkout default | Not applicable |
| Local PostgreSQL password | File (`NEST_POSTGRES_PASSWORD_FILE`) | File | Not included | Not included | `NEST_DATABASE_PASSWORD` | Not applicable |
| Automatic `nest-db upgrade` | No | No | No | No | `migrate` profile | No |

## Migrate an existing file-based deployment

1. Record the current Compose project name and the volume names
   (`docker volume ls`). Keep the same Dokploy application so `tailscale-state`
   (and `postgres-data`, if any) are reused.
2. Copy each mounted file into the matching variable from
   [How each credential is delivered](#how-each-credential-is-delivered): for example
   `NEST_SSH_DIR/config` to `NEST_SSH_CONFIG`, its key to `NEST_SSH_PRIVATE_KEY`
   (and change `IdentityFile` to `~/.ssh/id_nest`), `known_hosts` to
   `NEST_SSH_KNOWN_HOSTS_DATA`, the database password file to
   `NEST_DATABASE_PASSWORD`, and `platforms.env` to `NEST_PLATFORMS_ENV`. Remove
   each `*_FILE` or path variable you replace: keeping both is an error.
3. Moving from `compose.tailscale-shared.yaml`: the volume names match, because
   both files use project `nest`'s `tailscale-state`. The Auth Key is not needed
   for existing state; set `NEST_TAILSCALE_AUTHKEY=''`.
4. Moving from the `compose.yaml` `local-db` profile under the same project name:
   that file's `postgres-data` volume has the same name, and it keeps its original
   password. Set
   `NEST_DATABASE_PASSWORD` to the contents of the old `nest-postgres-password` file.
5. Change the Compose Path to `./compose.dokploy.yaml`, deploy, and run
   [Verify](#verify). To roll back, restore the previous Compose Path and variables.
   No data format changes, so either direction works.

## Security trade-offs

Environment variables are convenient, but they are visible to more parties than a
mounted secret file:

- **`docker inspect`, `docker compose config`** and the Docker API show every
  container environment variable to anyone who can reach the Docker daemon. That
  access already means root on the host.
- **`/proc/<pid>/environ`** of the Controller is readable by the same user and by
  root, and Dokploy's terminals run as that user.
- **Dokploy stores the Environment** in its database and writes it to `.env` on
  the server, readable by Dokploy administrators and host root.

Nest limits the exposure it controls:

- Values are wrapped so `repr()`, the startup summary, the audit record and
  configuration errors never contain them. Nest's log handler masks the database
  password, inline TLS and SSH keys, the SMTP password and the Google client secret
  wherever they appear.
- SSH, `scp` and local worker processes start with an environment that excludes
  every Controller credential ([`config.child_environ`]). User code, Slurm jobs and
  login nodes never receive them, even with an SSH `SendEnv` setting.
- The Tailscale Auth Key and the PostgreSQL password are delivered as Compose
  secret files. They are not in those containers' environments or `docker
  inspect`, and the Controller never receives the Auth Key.
- No credential is baked into the image, a build argument or the repository. The
  build context excludes `.env`, `ssh/`, `secrets/` and `files/`, and the hygiene
  test fails on committed keys.

Use the file alternatives where a secret-file mount is available and the
environment exposure above is not acceptable. Restrict access to Dokploy, the
Docker daemon and backups of `postgres-data` and `tailscale-state`, which hold
device credentials.

## Not verified here

The repository's automated checks render this file, run its PostgreSQL,
migration, Controller and sidecar containers from an environment-only `.env`, and
restart and recreate them (`tests/smoke_compose_env.sh`). They do not run Dokploy
itself, its YAML rewriting or Domains, a real Tailscale enrollment or Serve
connection, Google's token endpoint, or a real login node. Check those during
rollout with the steps above.
