

# Nest

[![Version](https://img.shields.io/badge/version-0.1.0a1-blue)](https://github.com/cerulean-works/nest/releases)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Status](https://img.shields.io/badge/status-early_access-orange)

Nest is a Python SDK for executing Python functions on remote Slurm clusters.

## Early Access

Nest is currently in Early Access (`0.1.0a1`).

The API is experimental and may change before version 1.0.0.

## Installation

Nest requires Python 3.12 or later.

Install Nest from GitHub:

```bash
pip install "git+https://github.com/cerulean-works/nest.git@main"
```

A versioned release installation command will be provided when the first Early Access release is tagged.

## Quick Start

For the current team deployment, make sure your device is connected to the authorized Tailscale network.

No additional Controller configuration is required.

Create `main.py`:

```python
import nest

@nest.remote(gpu=1)
def compute(x):
    return x * 2

print(compute.local(21))   # 42
print(compute.remote(21))  # 42
```

Run:

```bash
python main.py
```

- `.local()` executes the function on your machine.
- `.remote()` sends the function to the Nest Controller, which schedules execution on the HPC cluster.

The default Controller endpoint is:

```text
services.tail8e5ba1.ts.net:50051
```

If you are a member of the authorized Tailnet, you do not need to configure this address manually.

### Using a Different Controller

You can override the default endpoint:

```bash
export NEST_CONTROLLER_ADDRESS="your-controller:50051"
```

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
    import humanize
    return humanize.intcomma(1234567)

print(compute.remote())
```

Nest transfers the project's `pyproject.toml` and `uv.lock` to the Controller.

The HPC backend prepares an isolated Python environment and caches it for reuse.

**Note:** Automatic project source-code synchronization is not yet supported. Project-local modules and data files are not automatically transferred.

## Remote Exceptions

Exceptions raised during remote function execution are returned as `RemoteExecutionError`.

```python
import nest
from nest.errors import RemoteExecutionError

@nest.remote(gpu=1)
def compute():
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

### Requirements

- Python 3.12+
- Access to the target HPC Login Node via SSH
- A configured Slurm backend
- A working Nest runtime and Apptainer environment on HPC

### Configuration

Example:

```bash
export NEST_EXECUTOR="slurm"
export NEST_CONTROLLER_BIND="$(tailscale ip -4):50051"

export NEST_SLURM_HOST="<ssh-host>"
export NEST_SLURM_ACCOUNT="<slurm-account>"
export NEST_SLURM_WORKDIR="/home/your-user/nest"
```

Start the Controller:

```bash
uv run python -m nest.controller
```

The Controller listens on the configured network address.

The Tailscale binding example requires a locally configured Tailscale network interface.


### HPC Work Directory

Nest uses `NEST_SLURM_WORKDIR` to configure the working directory on the HPC cluster.

```bash
export NEST_SLURM_WORKDIR="/home/your-user/nest"
```

The path must be absolute and accessible from both the HPC Login Node and Compute Nodes.

Nest expects the following directory structure:

```text
nest/
├── images/
│   └── uv-python312.sif
├── project/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── scripts/
│   └── src/
│       └── nest/
├── environments/
└── jobs/
```

Directory purposes:

- `images/`: Apptainer images used for remote execution.
- `project/`: The deployed Nest runtime and Slurm scripts.
- `environments/`: Cached Python environments for user projects.
- `jobs/`: Execution inputs, outputs, and temporary files.

The Nest runtime, Slurm scripts, and Apptainer image must be deployed by the host before starting remote executions.

The Controller passes the configured working directory to Slurm jobs. Inside Apptainer, this directory is mounted at `/workspace`.

Changing `NEST_SLURM_WORKDIR` does not automatically migrate existing files or deploy the runtime to the new location.

Users do not need to configure this directory. It is managed entirely by the host.


### Local Development

For local development without Slurm:

```bash
export NEST_EXECUTOR="local"
export NEST_CONTROLLER_BIND="127.0.0.1:50051"

uv run python -m nest.controller
```

Clients can connect to this Controller by setting:

```bash
export NEST_CONTROLLER_ADDRESS="127.0.0.1:50051"
```

## Security

Nest currently uses Python object serialization for remote function execution.

Deserializing untrusted Python payloads may execute arbitrary code.

The current Controller does not provide application-level authentication, multi-tenant isolation, or per-user HPC identity management.

Only trusted users should be granted access to the Controller.

For the current team deployment, restrict network access using Tailscale Grants / ACLs.

Do not expose the Controller directly to the public Internet.

## Known Limitations

- Experimental API with potential breaking changes.
- No multi-tenant isolation.
- No per-user HPC accounts.
- No automatic project source or data synchronization.
- No application-level authentication.
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
    --grpc_python_out=src \
    src/nest/proto/execution.proto
```
