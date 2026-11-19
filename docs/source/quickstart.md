# Run your first remote function

For the team deployment, connect to the authorized Tailscale network or accept
a Controller node share. This deployment uses plaintext gRPC inside the encrypted
Tailscale network. Select the address and transport explicitly.

```bash
export NEST_CONTROLLER_ADDRESS="nest.cerulean.works:50051"
export NEST_CONTROLLER_TLS=false
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

## Use a different Controller

The SDK defaults to a [local Controller](local-controller) at `127.0.0.1:50051`.
For a TLS-enabled remote Controller, set its address and enable TLS. The client
verifies the server certificate with gRPC's default trust roots or the PEM roots
specified by `NEST_CONTROLLER_TLS_CA_FILE`.

```bash
export NEST_CONTROLLER_ADDRESS="your-controller:50051"
export NEST_CONTROLLER_TLS=true
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
