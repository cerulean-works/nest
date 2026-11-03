# Deploy a Slurm Controller

This section is intended for hosts who operate a Nest Controller.

For a container deployment, start with [Deploy with Docker Compose](docker-compose).
The HPC work directory and runtime described below are required for both methods.

## Requirements

- Python 3.12+
- Access to the target HPC Login Node via SSH
- A configured Slurm backend
- A working Nest runtime and Apptainer environment on HPC

## Configuration

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


## HPC Work Directory

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

## Verify the deployment

The Controller host must be able to run noninteractive SSH commands against
`NEST_SLURM_HOST`. The runtime, image, scripts, and shared directories above must
already exist; starting the Controller does not deploy them.

Review the batch scripts for your cluster's partition, account, time, memory,
and container requirements before use. From a trusted client, run the
[quickstart](quickstart) with `NEST_CONTROLLER_ADDRESS` set to this Controller.

The current startup code uses partition `dev` and a `00:05:00` time limit.
See [Environment variables and defaults](configuration) for all settings and
[Slurm backend](api/slurm) for direct backend configuration.
