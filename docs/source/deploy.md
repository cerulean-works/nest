# Deploy a Slurm Controller

This section is intended for hosts who operate a Nest Controller.

For a container deployment, start with [Deploy on Dokploy from environment
variables](dokploy) or [Deploy with Docker Compose](docker-compose). The HPC work
directory and runtime described below are required for every method.

A shared Controller also needs PostgreSQL ([Store Controller records in
PostgreSQL](database)) and Google sign-in ([Sign in with Google](authentication)):
with `NEST_DATABASE_URL` set it authenticates every call
(`NEST_CONTROLLER_AUTH=session`) and admits Slurm submissions through the
[Execution Registry](execution-registry). The example below without a database is
the unauthenticated mode; restrict it to trusted networks.

## Requirements

- Python 3.14+
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
export NEST_SLURM_PARTITION="h200_256gpu"
export NEST_SLURM_WORKDIR="/home/your-user/nest"
```

Start the Controller:

```bash
uv run python -m nest.controller
```

The Controller listens on the configured network address.

The Tailscale binding example requires a locally configured Tailscale network interface.

With a database and session authentication, a plaintext Tailnet listener also
needs `NEST_CONTROLLER_ALLOW_PLAINTEXT=true`, and the operator's UNIX user
(`NEST_SLURM_SSH_USER`). `NEST_SLURM_ACCOUNT` is then charged only if it is
registered and linked in the [Slurm Account Registry](slurm-accounts); see
[Onboard a Project](project-onboarding).


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
│   └── uv-python314.sif
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

`images/uv-python314.sif` is built from the `hpc-runtime` target of the repository's
`Dockerfile`, which adds Git, CA certificates and the OpenSSH client so that
`uv.lock` files with Git dependencies install. See
[Build and roll out the HPC runtime image](runtime-image).

The Controller passes the configured working directory to Slurm jobs. Inside Apptainer, this directory is mounted at `/workspace`.

Changing `NEST_SLURM_WORKDIR` does not automatically migrate existing files or deploy the runtime to the new location.

Users do not need to configure this directory. It is managed entirely by the host.

## Verify the deployment

The Controller host must be able to run noninteractive SSH commands against
`NEST_SLURM_HOST`. The runtime, image, scripts, and shared directories above must
already exist; starting the Controller does not deploy them.

Review the batch scripts for your cluster's partition, account, time, memory,
and container requirements before use. [Onboard a Project](project-onboarding)
for an authenticated Controller, then, from a trusted client, run the
[quickstart](quickstart) with `NEST_CONTROLLER_ADDRESS` set to this Controller.
Set `NEST_CONTROLLER_TLS=false` for the plaintext Tailnet example above, or
configure the client [TLS settings](configuration#client) for a TLS deployment.

The Controller defaults to the team's `h200_256gpu` partition and a `00:05:00`
time limit. For other accounts, set `NEST_SLURM_PARTITION` to a partition the
account can use. Set `NEST_SLURM_TIME` to change the worker time limit.
Environment builds keep the `00:10:00` limit in `scripts/environment-smoke.sbatch`.
See [Environment variables and defaults](configuration) for all settings and
[Slurm backend](api/slurm) for direct backend configuration.
