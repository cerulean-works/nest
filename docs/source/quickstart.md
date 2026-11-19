# Run your first remote function

The SDK connects to the team Controller at `nest.cerulean.works:50051` by default.
Connect to the authorized Tailscale network or accept a Controller node share
before running remote calls against the team deployment.

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

## Use a different Controller

Set the address of your Controller. For a [local Controller](local-controller),
use `127.0.0.1:50051`.

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
