from dataclasses import dataclass
from functools import update_wrapper
from typing import Any, Callable

from .executor import execute

@dataclass(frozen=True)
class Resources:
    gpu: int = 0
    cpu: int = 1
    
class RemoteFunction:
    def __init__(self, func: Callable, resources: Resources):
        self.func = func
        self.resources = resources
        update_wrapper(self, func)
    
    def local(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)
    
    def remote(self, *args: Any, **kwargs: Any) -> Any:
        return execute(self.func, *args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.local(*args, **kwargs)
    
def remote(*, gpu: int = 0, cpu: int = 1):
    resources = Resources(gpu=gpu, cpu=cpu)
    
    def decorator(func: Callable) -> RemoteFunction:
        return RemoteFunction(func, resources)
    
    return decorator