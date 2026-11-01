# Run your first remote function

For the current team deployment, make sure your device is connected to the authorized Tailscale network.

No additional Controller configuration is required.

Create `main.py`:

```python
import nest

@nest.remote(gpu=1)
def compute(x):
    """Returns twice the input value."""
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

## Use a different Controller

You can override the default endpoint:

```bash
export NEST_CONTROLLER_ADDRESS="your-controller:50051"
```

## Choose how to run

Calling `compute(21)` is equivalent to `compute.local(21)`: both run in the
current Python process. Only `compute.remote(21)` sends the function to the
Controller and waits for a result.

The `gpu` argument requests GPUs for Slurm execution. The current Slurm Controller
does not forward the decorator's `cpu` count to the backend.

Continue with [Use project dependencies](project-dependencies) or
[Handle remote errors](remote-errors). The full API is in
[Remote functions and resources](api/remote).
