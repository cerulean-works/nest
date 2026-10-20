import os
import grpc
import cloudpickle
from pathlib import Path
from typing import Any, Callable

from .errors import RemoteExecutionError
from .proto import execution_pb2, execution_pb2_grpc

def execute_remote(
    func: Callable,
    resources: Any,
    project: Path | None,
    *args: Any,
    **kwargs: Any
) -> Any:
    # Serialize function and arguments
    payload = cloudpickle.dumps(
        (func, args, kwargs)
    )
    
    request = execution_pb2.ExecuteRequest(
        payload=payload,
        gpu=resources.gpu,
        cpu=resources.cpu,
    )
    
    if project is not None:
        pyproject_path = project / "pyproject.toml"
        lock_path = project / "uv.lock"
        
        request.environment.CopyFrom(
            execution_pb2.EnvironmentFiles(
                pyproject_toml=pyproject_path.read_bytes(),
                uv_lock=lock_path.read_bytes(),
            )
        )
    
    DEFAULT_CONTROLLER_ADDRESS = "services.tail8e5ba1.ts.net:50051"
    
    address = os.getenv("NEST_CONTROLLER_ADDRESS") or DEFAULT_CONTROLLER_ADDRESS
    
    # Connect to Controller
    try:        
        with grpc.insecure_channel(address) as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)
            response = stub.Execute(request, timeout=900)
    
    except grpc.RpcError as exc:
        if exc.code() == grpc.StatusCode.UNAVAILABLE:
            raise ConnectionError(
                f"Cannot connect to Nest Controller at {address}.\n\n"
                "Please check:\n"
                "  - Tailscale is connected to the required Tailnet.\n"
                "  - You have permission to access the Controller.\n"
                "  - The Controller is running.\n\n"
                "To use another Controller, set NEST_CONTROLLER_ADDRESS."
            ) from exc
        
        raise

    # Deserialze result or raise a remote exception
    return _decode_remote_result(response.result)

def _decode_remote_result(data: bytes) -> Any:
    result = cloudpickle.loads(data)
    
    if (
        isinstance(result, dict)
        and result.get("__nest_remote_error_v1__") is True
        and all(
            isinstance(result.get(key), str)
            for key in ("type", "message", "traceback")
        )
    ):
        raise RemoteExecutionError(
            remote_type=result["type"],
            remote_message=result["message"],
            remote_traceback=result["traceback"],
        )
    
    return result
