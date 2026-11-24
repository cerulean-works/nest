

# Nest

[![Version](https://img.shields.io/badge/version-0.1.0a1-blue)](https://github.com/cerulean-works/nest/releases)
![Python](https://img.shields.io/badge/python-3.14%2B-blue)
![Status](https://img.shields.io/badge/status-early_access-orange)

Nest is a Python SDK for executing Python functions on remote Slurm clusters.

**Documentation:** [Guides and API reference](https://docs.cerulean.works/nest/main/guide/)

## Early Access

Nest is currently in Early Access (`0.1.0a1`).

The API is experimental and may change before version 1.0.0.

## Installation

Nest requires Python 3.14 or later. Add it to a [uv](https://docs.astral.sh/uv/)
project from GitHub:

```bash
uv add "nest @ git+https://github.com/cerulean-works/nest@main"
```

`uv.lock` pins the commit that `main` pointed to when you added it. To update to
the current SDK later, run `uv lock --upgrade-package nest && uv sync`. Without
uv, use `pip install "git+https://github.com/cerulean-works/nest.git@main"`. See
[Install Nest](docs/source/installation.md) for checking the installed commit and
how SDK, Controller and HPC runtime versions relate.

A versioned release installation command will be provided when the first Early Access release is tagged.

## Quick Start

The Cerulean-hosted Controller is authenticated (M1): each call carries your Nest
session and runs in a Project that the operator has approved. The
[full walkthrough](docs/source/quickstart.md) explains each step; in short:

1. Sign in by approving the CLI in a browser. No setting is needed: `nest login`
   uses the hosted sign-in at `https://auth.nest.cerulean.works`.

   ```bash
   uv run nest login
   uv run nest auth status --check
   ```

2. Connect to the authorized Tailscale network, or accept the Controller node
   share, and point the SDK at the hosted Controller. These stay explicit because
   the Controller's gRPC is plaintext inside the encrypted Tailnet, and the SDK
   never assumes that ([why](docs/source/configuration.md#why-the-hosted-controller-is-not-a-default)):

   ```bash
   export NEST_CONTROLLER_ADDRESS="nest.cerulean.works:50051"
   export NEST_CONTROLLER_TLS=false
   ```

   The SDK sends your hosted session to this Controller only.

3. Join a Project through an invitation from its Owner or an Admin, or create one,
   and select it by ID (see [Join or create a Project](docs/source/quickstart.md#4-join-or-create-a-project)):

   ```bash
   export NEST_PROJECT_ID="<project-id>"
   ```

4. Ask the operator to approve the Project: Provider approval, an HPC Platform
   grant and a Slurm account ([Onboard a Project](docs/source/project-onboarding.md)).
   `uv run nest platforms default --project-id "$NEST_PROJECT_ID"` shows the
   granted Platform.

Then create `main.py`:

```python
import nest


@nest.remote(gpu=1)
def compute(x):
    """Returns twice the input value."""
    return x * 2


print(compute.local(21))  # 42
print(compute.remote(21))  # 42
```

Run:

```bash
uv run python main.py
```

- `.local()` executes the function on your machine.
- `.remote()` sends the function to the Nest Controller, which checks your session
  and Project and schedules execution on the HPC cluster.

A refused call raises `AuthenticationRequiredError` (`UNAUTHENTICATED`) or
`RemoteAccessDeniedError` (`PERMISSION_DENIED`, `NOT_FOUND`) and names the reason;
see [Access and onboarding errors](docs/source/remote-errors.md#access-and-onboarding-errors).

### Self-hosted Nest

Name your Nest's sign-in origin, and your Controller, explicitly:

```bash
export NEST_AUTH_URL="https://nest.example.com"   # or: nest login --url ...
export NEST_CONTROLLER_ADDRESS="controller.example.com:50051"
export NEST_CONTROLLER_TLS=true
```

`--url` wins over `NEST_AUTH_URL`, which wins over the hosted default. Without
`NEST_AUTH_URL`, a Controller other than the hosted one receives no session. See
[Sign-in origin](docs/source/configuration.md#sign-in-origin).

### Local development without a session

The SDK defaults to a [local Controller](docs/source/local-controller.md) at
`127.0.0.1:50051`. A Controller started without a database has no sign-in,
Projects or authorization, so use it only on loopback:

```bash
NEST_EXECUTOR=local NEST_CONTROLLER_BIND=127.0.0.1:50051 uv run python -m nest.controller
```

In another terminal, with `NEST_CONTROLLER_ADDRESS`, `NEST_CONTROLLER_TLS`,
`NEST_AUTH_URL` and `NEST_PROJECT_ID` unset, run `uv run python main.py`.

### Using a TLS Controller

For a Controller with its own TLS certificate, set `NEST_CONTROLLER_TLS=true`. The
client verifies the server certificate with gRPC's default trust roots or the PEM
roots specified by `NEST_CONTROLLER_TLS_CA_FILE`.

## Project Dependencies

Nest supports project-specific Python environments managed by uv.

Given a project containing:

```text
my-project/
├── pyproject.toml
├── uv.lock
└── main.py
```

Use the `project` parameter:

```python
import nest


@nest.remote(gpu=1, project=".")
def compute():
    """Formats a number with the project's humanize dependency."""
    import humanize

    return humanize.intcomma(1234567)


print(compute.remote())
```

Nest transfers the project's `pyproject.toml` and `uv.lock` to the Controller.
`project=` names this uv project directory; it is unrelated to the Nest Project
selected by `NEST_PROJECT_ID`.

The HPC backend prepares an isolated Python environment and caches it for reuse.

**Note:** Automatic project source-code synchronization is not yet supported. Project-local modules and data files are not automatically transferred.

## Remote Exceptions

Exceptions raised during remote function execution are returned as `RemoteExecutionError`.

```python
import nest
from nest.errors import RemoteExecutionError


@nest.remote(gpu=1)
def compute():
    """Raises an error to demonstrate remote exception handling."""
    raise ValueError("Something went wrong")


try:
    compute.remote()
except RemoteExecutionError as exc:
    print(exc.remote_type)
    print(exc.remote_message)
    print(exc.remote_traceback)
```

The original exception type, message, and remote traceback are preserved.

## Controller Deployment

This section is intended for hosts who operate a Nest Controller.

- [Deploy on Dokploy from environment variables](docs/source/dokploy.md) with
  [compose.dokploy.yaml](compose.dokploy.yaml): the complete M1 Controller
  (PostgreSQL and migrations, Google sign-in, Project and Platform authorization,
  the Execution Registry, Tailscale, SSH) configured only from Dokploy's
  Environment panel.
- [Deploy with Docker Compose](docs/source/docker-compose.md) with
  [compose.yaml](compose.yaml), adding [PostgreSQL](docs/source/database.md) and
  [Google sign-in](docs/source/authentication.md) for authentication, and
  [Share a Controller over Tailscale](docs/source/tailscale-shared.md) for members
  on separate Tailnets.
- [Deploy a Slurm Controller](docs/source/deploy.md) for the HPC work directory
  (`NEST_SLURM_WORKDIR`), Slurm scripts and runtime, and
  [Build and roll out the HPC runtime image](docs/source/runtime-image.md) for
  `images/uv-python314.sif`, which includes Git for `uv.lock` Git dependencies.
- [Onboard a Project](docs/source/project-onboarding.md): approve it in
  `NEST_PROVIDER_ALLOWED_PROJECTS`, grant an HPC Platform and link a Slurm account
  with `nest-db`.

Requirements: Python 3.14+ (or the Controller image), SSH access to the HPC login
node, a Slurm account, and the Nest runtime and Apptainer image deployed in the
HPC work directory. Starting the Controller does not deploy the HPC runtime.

## Logging

Controller and worker commands log to stderr at `INFO` by default. Use
`NEST_LOG_LEVEL=DEBUG` for more detail. Logs include request IDs, execution timings,
Slurm job IDs, cache activity and state changes.

Client applications enable the `nest` logger using Python's `logging` module.
See [logging configuration and troubleshooting](docs/source/configuration.md#logging-and-troubleshooting)
for client setup and how to follow an execution across the Controller and workers.

## Security

Nest currently uses Python object serialization for remote function execution.

Deserializing untrusted Python payloads may execute arbitrary code.

With a database, the Controller authenticates every gRPC call with a Nest session
from Google sign-in and authorizes it with deny-by-default Project, Provider,
Platform and Slurm account policy. Without a database (`NEST_CONTROLLER_AUTH=none`,
for local development) it authenticates nobody.

Authentication is not isolation: every Run executes as the Controller's single HPC
account, in a shared work directory, without an OS sandbox or per-user HPC
identity. Only trusted users should be granted access to the Controller.

For the current team deployment, restrict network access using Tailscale Grants / ACLs.

Do not expose the Controller directly to the public Internet.

See [Security and limitations](docs/source/security.md).

## Known Limitations

- Experimental API with potential breaking changes.
- No multi-tenant isolation or OS sandbox for submitted code.
- No per-user HPC accounts: all Runs use the Controller's HPC identity.
- No automatic project source or data synchronization.
- The SDK wraps only the synchronous `Execute` call; `SubmitJob`, `GetJobStatus`
  and `CancelJob` are available as gRPC RPCs.
- No authentication without a database; no Web UI or CLI command to create Projects.
- Controller and backend deployment are managed by the host.

## Development

Install development dependencies:

```bash
uv sync
```

Run the local examples:

```bash
uv run python examples/hello.py
uv run python examples/serialization.py
```

### Regenerate Protobuf Code

```bash
uv run python -m grpc_tools.protoc \
    -I src \
    --python_out=src \
    --pyi_out=src \
    --grpc_python_out=src \
    src/nest/proto/execution.proto
```

## License

Nest is licensed under the [Apache License 2.0](LICENSE).
