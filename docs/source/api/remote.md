# Remote functions and resources

Use `@nest.remote(...)` to wrap a Python function. Calling the wrapper directly or
using `.local()` runs it in the current process; `.remote()` sends it to the
Controller and blocks until a result or error arrives.

Start with [Run your first remote function](../quickstart). For dependency files,
see [Use project dependencies](../project-dependencies).

## Decorator

[[autodoc]] remote

## Wrapped functions

The decorator returns a `nest.remote.RemoteFunction`. The implementation is in
[`remote.py`](https://github.com/cerulean-works/nest/blob/main/src/nest/remote.py).

| Method | Behavior | Result |
| --- | --- | --- |
| `local(*args, **kwargs)` | Calls the original function in the current process. | The function's return value; local exceptions propagate directly. |
| `__call__(*args, **kwargs)` | Delegates to `local`. | The same result as calling `.local()`. |
| `remote(*args, **kwargs)` | Sends the function, arguments, and optional project files to the Controller. | The decoded remote result; blocks until completion or failure. |

The wrapper exposes `func` (the original function), `resources` (a `Resources`
instance), and `project` (a resolved `Path`, or `None`). It preserves the original
function's metadata with `functools.update_wrapper`.

Remote calls can raise `OSError` while reading dependency files,
`ConnectionError` when the Controller is unavailable, an
[`errors.InfrastructureError`] subclass for classified SSH, Slurm, and timeout
failures, `grpc.RpcError` for other RPC failures, or
[`errors.RemoteExecutionError`] for a remote Python exception.

<!-- nest.remote exports the decorator and shadows the module in attribute
     traversal. Keep the wrapper and resources explicit until doc-builder can
     resolve classes through that shadowed module. -->

## Resource requests

The client sends both resource counts. The Slurm Controller requests `gpu` with
`sbatch --gpus-per-node` and `cpu` with `--cpus-per-task`, for both the worker
and any environment build. A count of `0` omits the flag, so the batch script's
own directive applies. The local Controller does not allocate Slurm resources.

`nest.remote.Resources(gpu=0, cpu=1)` is a frozen dataclass with integer `gpu` and
`cpu` fields. It raises `TypeError` for non-integer counts and `ValueError` for
negative counts, so `@nest.remote(cpu=-1)` fails when the decorator is created.
Nest creates it from the decorator arguments; callers normally do not need to
construct it directly.

Import the types from their defining module when needed:

```python
from nest.remote import RemoteFunction, Resources
```
