

# Nest

[![Release](https://img.shields.io/github/v/release/cerulean-works/nest?include_prereleases&label=release)](https://github.com/cerulean-works/nest/releases)
![Python](https://img.shields.io/badge/python-3.14%2B-blue)
![Status](https://img.shields.io/badge/status-early_access-orange)

Nest runs Python functions on HPC clusters without Slurm scripts: decorate a
function, call `.local()` while you develop and `.remote()` to run it on the
cluster.

**Documentation:** [Quickstart](https://docs.cerulean.works/nest/main/guide/quickstart),
[guides and API reference](https://docs.cerulean.works/nest/main/guide/)

## Early Access

Nest is in Early Access. Install it from `main` (below); the API is experimental
and may change before 1.0. The package version is `0.1.0a5`, the version of the
next planned pre-release, `v0.1.0a5`. That pre-release is the first to include
sign-in. It exists only once it is listed on the
[Releases page](https://github.com/cerulean-works/nest/releases); until then,
`@v0.1.0a5` installs nothing. The older tags (`v0.1.0a1` to `v0.1.0a4`) predate
sign-in, so the authenticated Controller refuses them, and every commit of `main`
reports the same version, so compare commits rather than version strings. See
[Install Nest](docs/source/installation.md#releases-and-main) and
[Release status and checklist](docs/source/releasing.md).

## Quickstart

You need [uv](https://docs.astral.sh/uv/), a Google account, and a Nest Project you
may run in (an invitation from its Owner or an Admin, and the operator's approval;
see [Get access to a Project](docs/source/get-access.md)).

**Connection settings.** Cerulean-hosted Nest is reachable today over its
authorized Tailnet. Connect Tailscale (Tailnet membership or the Controller node
share), then, in a new terminal or after clearing settings left from another
connection (the SDK refuses conflicting settings instead of guessing):

```bash
unset NEST_PROFILE NEST_CONTROLLER_TLS_CA_FILE NEST_HOSTED_SESSION_ADDRESS NEST_PROJECT_ID
export NEST_AUTH_URL="https://auth.nest.cerulean.works"
export NEST_CONTROLLER_ADDRESS="nest-controller.tail8e5ba1.ts.net:50051"
export NEST_CONTROLLER_TLS=false    # plaintext gRPC, only inside the encrypted Tailnet
```

The public gateway (no Tailscale) is an operator-run staging pilot until the H09
production sign-off, not a default; for your own Nest or local development, see
the [full Quickstart](docs/source/quickstart.md).

**1. Install**

```bash
uv init --python 3.14 hello-nest
cd hello-nest
uv add "nest @ git+https://github.com/cerulean-works/nest@main"
```

**2. Log in** by approving the CLI in a browser, then check your Project:

```bash
uv run nest login
uv run nest projects current    # "<project-id> (only eligible)": no Project ID needed
```

`only eligible` (or `saved`) is checked against the Controller's list of Projects
you may run in; `uv run nest projects list` shows that list.

**3. Create `main.py`:**

```python
import nest


@nest.remote(cpu=1)
def compute(x):
    """Returns twice the input value."""
    return x * 2


print(compute.local(21))  # runs here
print(compute.remote(21))  # runs on HPC
```

**4. Run**

```bash
uv run python main.py
```

```text
42
42
```

`.local()` runs the function in your process; `.remote()` sends it to the
Controller, which checks your session and Project and runs it as a Slurm job. With
several Projects, choose one with `uv run nest projects select` (scripts and CI set
`NEST_PROJECT_ID`). A refused call raises `AuthenticationRequiredError` or
`RemoteAccessDeniedError` and names the reason; see
[Access and onboarding errors](docs/source/remote-errors.md#access-and-onboarding-errors).

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
your calls run in.

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

## Operator Guide

This section is for hosts who operate a Nest Controller; users do not need it.
The [Operator guide](https://docs.cerulean.works/nest/main/guide/#operator-guide)
lists every runbook.

- [Deploy on Dokploy from environment variables](docs/source/dokploy.md) with
  [compose.dokploy.yaml](compose.dokploy.yaml): the complete M1 Controller
  (PostgreSQL and migrations, Google sign-in, Project and Platform authorization,
  the Execution Registry, Tailscale, SSH) configured only from Dokploy's
  Environment panel, with a
  [preflight and recovery runbook](docs/source/deployment-recovery.md).
- [Deploy with Docker Compose](docs/source/docker-compose.md) with
  [compose.yaml](compose.yaml), adding [PostgreSQL](docs/source/database.md) and
  [Google sign-in](docs/source/authentication.md) for authentication, and
  [Share a Controller over Tailscale](docs/source/tailscale-shared.md) for members
  on separate Tailnets.
- [Deploy a Slurm Controller](docs/source/deploy.md) for the HPC work directory
  (`NEST_SLURM_WORKDIR`), Slurm scripts and runtime, and
  [Build and roll out the HPC runtime image](docs/source/runtime-image.md) for
  `images/uv-python314.sif`, which includes Git for `uv.lock` Git dependencies.
- [Onboard a Project](docs/source/project-onboarding.md): create it for its
  Owner, approve it for the Provider, grant an HPC Platform and link a Slurm
  account, with `nest-db`, the privileged API or the Console's administration
  pages.
- [Release status and checklist](docs/source/releasing.md) before tagging a
  pre-release: a `v*` tag publishes docs and runtime image tags and is followed by
  the operator's tag-triggered deployment.

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
- No authentication without a database (local development mode).
- Users cannot create Projects themselves: a platform administrator or operator
  creates them for a named Owner.
- The public gateway is a staging pilot; the hosted profile is not a default until
  the H09 production sign-off.
- Controller and backend deployment are managed by the host.

## Development

See [Develop Nest and its docs](docs/source/developing.md) for tests and the docs
preview. Install development dependencies:

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
