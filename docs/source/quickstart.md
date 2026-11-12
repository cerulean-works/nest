# Run your first remote function

For the current team deployment, make sure your device is connected to the authorized Tailscale network.

Point the SDK at the team Controller:

```bash
export NEST_CONTROLLER_ADDRESS="nest-tag.tail8e5ba1.ts.net:50051"
```

Create `main.py`:

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
python main.py
```

- `.local()` executes the function on your machine.
- `.remote()` sends the function to the Nest Controller, which schedules execution on the HPC cluster.

Without `NEST_CONTROLLER_ADDRESS`, the SDK connects to `127.0.0.1:50051`, a
[local Controller](local-controller). Earlier releases defaulted to the team's
Tailnet address; the SDK no longer depends on any Tailnet. Access to the team
Controller still needs Tailscale connected and access granted through the team
Tailnet or an accepted Controller node share.

## Use a different Controller

Set the address of any other Controller:

```bash
export NEST_CONTROLLER_ADDRESS="your-controller:50051"
```

## Choose how to run

Calling `compute(21)` is equivalent to `compute.local(21)`: both run in the
current Python process. Only `compute.remote(21)` sends the function to the
Controller and waits for a result.

The `gpu` and `cpu` arguments request GPUs per node and CPUs per task for Slurm
execution.

Continue with [Use project dependencies](project-dependencies) or
[Handle remote errors](remote-errors). The full API is in
[Remote functions and resources](api/remote).
