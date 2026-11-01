# Run a local Controller

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

Keep the Controller running in one terminal, then run the
[quickstart](quickstart) client in another terminal with the address above.

The Controller runs calls in a separate subprocess using its own Python
interpreter. It does not allocate GPUs or prepare a project's uv environment.
Install the function's dependencies into the Controller environment first.

The job status, submission, and cancellation RPCs still require a configured
Slurm backend. For HPC execution, use [Deploy a Slurm Controller](deploy).
