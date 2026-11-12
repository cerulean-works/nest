# Environment variables and defaults

Nest is configured only through environment variables. The same names work for a
native process (`python -m nest.controller`), Docker Compose, and Dokploy. No project
checkout, YAML or JSON configuration file, or `.env` file is required: Compose reads
`.env` only as a convenience, and a native process reads only its own environment.
Credentials stay in files or your platform's secret store, never in the repository.

Every entry point reads settings through `nest.config`. The Controller validates the
whole environment before it starts listening and reports **every** problem at once,
each naming the variable to change, then exits with status 2:

```text
ERROR nest.controller pid=7 request_id=- Invalid Nest configuration:
  - NEST_SSH_TIMEOUT must be a positive number of seconds, got '0'
  - Slurm execution requires: NEST_SLURM_HOST, NEST_SLURM_WORKDIR (or define Provider Profiles with NEST_PROVIDERS)
```

An empty value means unset. Error messages never repeat secret values
(`NEST_SSH_PRIVATE_KEY`, the password and query of `NEST_DATABASE_URL`). The
Controller logs a redacted `Controller configuration {...}` line at startup, and
its log handler masks an inline private key if any message ever contains it.

## Client

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_ADDRESS` | `127.0.0.1:50051` | Controller `host:port` for each remote call. |
| `NEST_RPC_TIMEOUT` | `900` | Seconds allowed for each `Execute` RPC. Must be a positive number. When it expires, the call raises `RpcTimeoutError`. |
| `NEST_CONTROLLER_TLS` | `false` (`true` when a CA file is set) | Use a TLS channel. Accepts `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`. |
| `NEST_CONTROLLER_TLS_CA_FILE` | Unset | Absolute path to PEM roots for the Controller's certificate. Unset uses the system roots. |

Set these in the process running your Nest application. They do not configure
where the Controller listens. Invalid client settings raise `nest.config.ConfigError`
(a `ValueError`) from `.remote()`.

```{note}
Earlier releases defaulted to `nest-tag.tail8e5ba1.ts.net:50051`, one team's
Tailnet name. The SDK no longer assumes any Tailnet: set
`NEST_CONTROLLER_ADDRESS` to reach a shared Controller.
```

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
| `NEST_TLS_CERT_FILE` | Unset | Absolute path to the PEM certificate chain. Set with `NEST_TLS_KEY_FILE` to serve TLS. |
| `NEST_TLS_KEY_FILE` | Unset | Absolute path to the PEM private key. Both TLS files, or neither. |
| `NEST_PUBLIC_URL` | Unset | Advertised `grpc://`, `grpcs://`, `http://` or `https://` URL, logged at startup. Must not contain credentials or a query string. |

The four timing settings accept positive numbers, including fractions.

### Provider Profiles

A **Compute Provider** is a Slurm cluster. A **Provider Profile** is the operator's
record for one provider: its approved login nodes, SSH mode and user, and Slurm
defaults. Profiles are bootstrapped from environment variables alone; users never
supply SSH configuration. The Controller connects only to a profile's login nodes,
using the first one.

**One provider** uses the `NEST_SLURM_*` variables, which define the profile
`default`. This is the form Compose passes through:

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_SLURM_HOST` | Unset | Login node or Mode A SSH alias. Same as a one-entry `NEST_SLURM_LOGIN_NODES`; set only one. |
| `NEST_SLURM_LOGIN_NODES` | Unset | Approved login nodes, comma-separated `host[:port]`. |
| `NEST_SLURM_SSH_MODE` | `A` | `A` (trusted operator SSH) or `B` (managed SSH); see below. |
| `NEST_SLURM_SSH_USER` | Unset | Remote user. Required in Mode B; in Mode A it overrides the SSH config. |
| `NEST_SLURM_ACCOUNT` | Unset | Default Slurm account. |
| `NEST_SLURM_PARTITION` | `dev` | Default Slurm partition. |
| `NEST_SLURM_TIME` | `00:05:00` | Default `sbatch --time`, in a Slurm format such as `30`, `00:05:00` or `1-00:00:00`. |
| `NEST_SLURM_WORKDIR` | Unset | Absolute shared HPC directory containing `project/`, `images/`, `environments/` and `jobs/`. |

With `NEST_EXECUTOR=slurm`, a login node, account and work directory are required.
In local mode a login node and account are enough to enable the job status and
cancel RPCs.

**Several providers** use `NEST_PROVIDERS`, a comma-separated list of lowercase IDs,
and the same fields under `NEST_PROVIDER_<ID>_`, for example
`NEST_PROVIDER_CAMPUS_LOGIN_NODES`. Here `LOGIN_NODES` and `ACCOUNT` are required, and
so is `WORKDIR` in Slurm mode. `NEST_DEFAULT_PROVIDER` selects the profile used for
execution (default: the first ID). `NEST_PROVIDERS` and the `NEST_SLURM_*` variables
cannot be combined.

```bash
NEST_EXECUTOR=slurm
NEST_PROVIDERS=campus,lab
NEST_DEFAULT_PROVIDER=campus
NEST_PROVIDER_CAMPUS_SSH_MODE=B
NEST_PROVIDER_CAMPUS_LOGIN_NODES=login1.example.edu,login2.example.edu:2222
NEST_PROVIDER_CAMPUS_SSH_USER=svc-nest
NEST_PROVIDER_CAMPUS_ACCOUNT=physics
NEST_PROVIDER_CAMPUS_PARTITION=gpu
NEST_PROVIDER_CAMPUS_TIME=02:00:00
NEST_PROVIDER_CAMPUS_WORKDIR=/scratch/svc-nest/nest
NEST_PROVIDER_LAB_LOGIN_NODES=lab-hpc
NEST_PROVIDER_LAB_ACCOUNT=lab
NEST_SSH_KNOWN_HOSTS=/run/secrets/nest_known_hosts
NEST_SSH_IDENTITY_FILE=/run/secrets/nest_ssh_key
```

The RPC API has no provider field yet, so every call uses the default profile.

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

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_SSH_MODES_ALLOWED` | `A,B` | Modes profiles may use. `B` forbids Mode A profiles. |
| `NEST_SSH_KNOWN_HOSTS` | Unset | Absolute path to verified host keys (Mode B). |
| `NEST_SSH_KNOWN_HOSTS_DATA` | Unset | The same host keys inline, one `known_hosts` line per line or separated by `\n`. Host keys are public. |
| `NEST_SSH_IDENTITY_FILE` | Unset | Absolute path to the private key (Mode B). Must not be readable by group or others. |
| `NEST_SSH_PRIVATE_KEY` | Unset | The private key inline, newlines as `\n`. A secret: it is written to a private temporary file at startup and never logged. |

Mode B needs one host-key source and one key source; set only one of each pair.
Prefer files or your platform's secret store for the key. An inline key is
visible to anyone who can inspect the process or container environment.

### Reserved settings

These names are part of the contract for features that do not exist yet. The
Controller refuses to start when one is set, rather than ignoring it:

| Variable | Planned use |
| --- | --- |
| `NEST_DATABASE_URL` | Job and audit database. Its password and query string are always redacted. |
| `NEST_AUTH_TOKEN_FILE` | Caller authentication. |
| `NEST_RETENTION_DAYS` | Retention of job records and staged files. |
| `NEST_INTEGRATIONS` | Optional integrations, comma-separated. |

Direct backend users can set [`backends.slurm.SlurmConfig`] instead. Review the
deployed batch scripts for memory and cluster-specific settings.

## Docker Compose

Compose uses the same contract. Each Controller definition passes an explicit list
of the variables above (all Controller settings except `NEST_PROVIDERS`, the
`NEST_PROVIDER_<ID>_*` family, and the reserved settings) so unrelated host
variables never reach the Controller. Unset values become empty, which means unset.
The container listens on `0.0.0.0:50051` and defaults to Slurm mode. For several
providers, add their `NEST_PROVIDER_*` variables in an override file.

These variables configure Compose itself:

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_IMAGE` | `nest-controller:local` | Local tag assigned to the image built from this checkout. |
| `NEST_CONTROLLER_HOST_IP` | `127.0.0.1` | Host interface for the published gRPC port. Use the host's Tailscale IPv4 address for Tailnet clients. |
| `NEST_CONTROLLER_PORT` | `50051` | Published host port; the container always listens on port 50051. |
| `NEST_SSH_DIR` | `./ssh` | Mode A: existing host directory containing the SSH configuration, key, and verified host keys. The container uses a private copy at `/home/nest/.ssh`, so in-container paths such as `/home/nest/.ssh/tls.key` work for the file settings above. |

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
| Slurm job time limit | `00:05:00` | `NEST_SLURM_TIME` or `NEST_PROVIDER_<ID>_TIME` | `ExecutionTimeoutError` (Slurm state `TIMEOUT`) |

The client RPC deadline covers the whole call: environment preparation, queueing,
execution, and download. The Controller deadlines are counted separately, so the
default environment build deadline is longer than the client's. A long build can
end with `RpcTimeoutError` while the Controller keeps waiting. Raise
`NEST_RPC_TIMEOUT` or lower the Controller deadlines if you need the classified
Controller error. Status queries and downloads can extend each polling deadline by
up to one SSH timeout.

No timeout cancels a submitted Slurm job, and Nest never retries a submission.
[Handle remote errors](remote-errors) describes each error and when it is safe to
call again.

### Resources

`@nest.remote(gpu=..., cpu=...)` defaults to `gpu=0`, `cpu=1`. The Slurm
Controller passes both counts to the worker job and to any environment build job:
`gpu` as `--gpus-per-node` and `cpu` as `--cpus-per-task`. A count of `0` omits
the flag, so the batch script's `#SBATCH` directive applies (one CPU in
`scripts/worker.sbatch`). Negative counts are rejected before anything is
submitted. The local Controller does not allocate resources.

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
