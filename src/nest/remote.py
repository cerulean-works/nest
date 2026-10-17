from dataclasses import dataclass
from functools import update_wrapper
from typing import Any, Callable
from pathlib import Path

from .client import execute_remote

@dataclass(frozen=True)
class Resources:
    gpu: int = 0
    cpu: int = 1
    
class RemoteFunction:
    def __init__(
        self, 
        func: Callable, 
        resources: Resources,
        project: str | Path | None = None,
    ):
        self.func = func
        self.resources = resources
        
        self.project = (
            Path(project).expanduser().resolve()
            if project is not None
            else None
        )
        
        update_wrapper(self, func)
    
    def local(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)
    
    def remote(self, *args: Any, **kwargs: Any) -> Any:
        return execute_remote(self.func, self.resources, self.project, *args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.local(*args, **kwargs)
    
def remote(
    *,
    gpu: int = 0,
    cpu: int = 1,
    project: str | Path | None = None,
):
    resources = Resources(gpu=gpu, cpu=cpu)
    
    def decorator(func: Callable) -> RemoteFunction:
        return RemoteFunction(func, resources, project=project)

    return decorator