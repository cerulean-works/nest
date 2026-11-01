# Handle remote errors

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

## Connection and Controller failures

A remote Python exception is different from a failure to reach or run the
Controller:

| Exception | What to inspect |
| --- | --- |
| `RemoteExecutionError` | The function's remote type, message, and traceback. |
| `ConnectionError` | Tailscale connectivity, Controller address, access rules, and whether the Controller is running. |
| `grpc.RpcError` | `exc.code()` and `exc.details()`, including RPC deadlines and Controller-reported Slurm failures. |
| `OSError` | Whether the local project's `pyproject.toml` and `uv.lock` can be read. |

A timeout does not automatically cancel a Slurm job. Hosts should inspect the
job's status before retrying a call that could have side effects.

See [RemoteExecutionError](api/errors), [Client execution](api/client), and
[Environment variables and defaults](configuration).
