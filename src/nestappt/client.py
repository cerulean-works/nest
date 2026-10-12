import os
from typing import Any, Callable

import cloudpickle
import grpc

from .proto import execution_pb2, execution_pb2_grpc

def execute_remote(func: Callable, resources: Any, *args: Any, **kwargs: Any) -> Any:
    # Serialize function and arguments
    payload = cloudpickle.dumps(
        (func, args, kwargs)
    )
    
    request = execution_pb2.ExecuteRequest(
        payload=payload,
        gpu=resources.gpu,
        cpu=resources.cpu,
    )
    
    address = os.getenv(
        "NESTAPPT_CONTROLLER_ADDRESS",
        "127.0.0.1:50051",
    )
    
    # Connect to Controller
    with grpc.insecure_channel(address) as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        response = stub.Execute(request, timeout=900)

    # Deserialze result
    return cloudpickle.loads(response.result)
