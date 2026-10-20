from dataclasses import dataclass
from functools import update_wrapper
from typing import Any, Callable
from pathlib import Path

from .client import execute_remote

@dataclass(frozen=True)
class Resources:
    """
    Stores the resource counts sent with a remote execution request.

    Attributes:
        gpu (`int`, *optional*, defaults to `0`): Number of requested GPUs.
        cpu (`int`, *optional*, defaults to `1`): Number of requested CPUs.
            The Slurm controller does not currently pass this count to the backend.
    """
    gpu: int = 0
    cpu: int = 1
    
class RemoteFunction:
    """
    Wraps a function with local and remote execution methods.

    Attributes:
        func (`Callable`): Original function.
        resources ([`remote.Resources`]): Resource counts for remote requests.
        project (`Path`, *optional*): Resolved project directory, if provided.
    """
    def __init__(
        self, 
        func: Callable, 
        resources: Resources,
        project: str | Path | None = None,
    ):
        """
        Stores a function, its resources, and its resolved project directory.

        Args:
            func (`Callable`): Function to wrap.
            resources ([`remote.Resources`]): Resource counts for remote requests.
            project (`str | Path`, *optional*): Project directory. Expands `~` and
                resolves the path without checking for environment files.
        """
        self.func = func
        self.resources = resources
        
        self.project = (
            Path(project).expanduser().resolve()
            if project is not None
            else None
        )
        
        update_wrapper(self, func)
    
    def local(self, *args: Any, **kwargs: Any) -> Any:
        """
        Runs the wrapped function in the current process.

        Args:
            *args (`Any`): Positional arguments passed to the wrapped function.
            **kwargs (`Any`): Keyword arguments passed to the wrapped function.

        Returns:
            `Any`: The wrapped function's return value.

        Raises:
            `Exception`: Any exception raised by the wrapped function.
        """
        return self.func(*args, **kwargs)
    
    def remote(self, *args: Any, **kwargs: Any) -> Any:
        """
        Sends the wrapped function to the controller and waits for its result.

        Args:
            *args (`Any`): Positional arguments passed to the wrapped function.
            **kwargs (`Any`): Keyword arguments passed to the wrapped function.

        Returns:
            `Any`: The deserialized return value.

        Raises:
            `OSError`: If project environment files cannot be read.
            `ConnectionError`: If the controller is unavailable.
            `grpc.RpcError`: If another RPC error occurs, including the 900-second
                request deadline expiring.
            [`errors.RemoteExecutionError`]: If the worker returns a remote error record.
        """
        return execute_remote(self.func, self.resources, self.project, *args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """
        Runs the wrapped function locally through [`remote.RemoteFunction.local`].

        Args:
            *args (`Any`): Positional arguments passed to the wrapped function.
            **kwargs (`Any`): Keyword arguments passed to the wrapped function.

        Returns:
            `Any`: The wrapped function's return value.

        Raises:
            `Exception`: Any exception raised by the wrapped function.
        """
        return self.local(*args, **kwargs)
    
def remote(
    *,
    gpu: int = 0,
    cpu: int = 1,
    project: str | Path | None = None,
):
    """
    Creates a decorator that adds local and remote execution to a function.

    Args:
        gpu (`int`, *optional*, defaults to `0`): Number of requested GPUs.
        cpu (`int`, *optional*, defaults to `1`): Number of requested CPUs.
            The Slurm controller does not currently pass this count to the backend.
        project (`str | Path`, *optional*): Directory containing `pyproject.toml`
            and `uv.lock`. Remote calls send these files to the controller.
            Project source files and data are not transferred.

    Returns:
        `Callable`: A decorator that wraps a function in [`remote.RemoteFunction`].

    Example:

    ```python
    >>> import nest
    >>> @nest.remote(gpu=1)
    ... def compute(x):
    ...     return x * 2
    >>> compute.local(21)
    42
    ```
    """
    resources = Resources(gpu=gpu, cpu=cpu)
    
    def decorator(func: Callable) -> RemoteFunction:
        """
        Wraps a function with the decorator's resources and project directory.

        Args:
            func (`Callable`): Function to wrap.

        Returns:
            [`remote.RemoteFunction`]: The wrapped function.
        """
        return RemoteFunction(func, resources, project=project)

    return decorator