import os
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, cast

import cloudpickle
import grpc

from ._logging import current_request_id, get_logger, operation, request_context
from .errors import (
    ERROR_KIND_METADATA,
    INFRASTRUCTURE_ERRORS,
    JOB_ID_METADATA,
    RemoteExecutionError,
    RpcTimeoutError,
)
from .proto import execution_pb2, execution_pb2_grpc

logger = get_logger(__name__)

DEFAULT_RPC_TIMEOUT = 900
"""Seconds allowed for an `Execute` RPC unless `NEST_RPC_TIMEOUT` overrides it."""


def _rpc_timeout() -> float:
    """
    Reads the `Execute` RPC deadline from `NEST_RPC_TIMEOUT`.

    Returns:
        `float`: Positive deadline in seconds; 900 when the variable is unset or empty.

    Raises:
        `ValueError`: If the value is not a positive finite number.
    """
    raw = os.getenv("NEST_RPC_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_RPC_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not 0 < value < float("inf"):
        raise ValueError(f"NEST_RPC_TIMEOUT must be a positive number of seconds, got {raw!r}")
    return value


def _controller_error(exc: grpc.RpcError, address: str, timeout: float) -> Exception | None:
    """
    Converts a Controller RPC error into a typed Nest exception when possible.

    Args:
        exc (`grpc.RpcError`): Error raised by the stub call.
        address (`str`): Controller address, used in messages.
        timeout (`float`): RPC deadline in seconds, used in messages.

    Returns:
        `Exception | None`: The exception to raise, or `None` to re-raise `exc`.
    """
    call = cast(grpc.Call, exc)
    metadata = {key: value for key, value in call.trailing_metadata() or ()}
    error_class = INFRASTRUCTURE_ERRORS.get(str(metadata.get(ERROR_KIND_METADATA, "")))

    if error_class is not None:
        job_id = str(metadata.get(JOB_ID_METADATA, ""))
        return error_class(call.details() or "", job_id=int(job_id) if job_id.isdecimal() else None)

    status = call.code()
    if status == grpc.StatusCode.DEADLINE_EXCEEDED:
        return RpcTimeoutError(
            f"Nest Controller at {address} did not reply within {timeout:g} seconds. "
            "Any Slurm job it submitted was not cancelled. Set NEST_RPC_TIMEOUT to "
            "allow longer calls."
        )

    if status == grpc.StatusCode.UNAVAILABLE:
        return ConnectionError(
            f"Cannot connect to Nest Controller at {address}.\n\n"
            "Please check:\n"
            "  - Tailscale is connected to the required Tailnet.\n"
            "  - You have permission to access the Controller.\n"
            "  - The Controller is running.\n\n"
            "To use another Controller, set NEST_CONTROLLER_ADDRESS."
        )

    return None


def _logged_execution(method):
    @wraps(method)
    def wrapper(*args, **kwargs):
        with request_context(), operation(logger, "Remote execution"):
            return method(*args, **kwargs)

    return wrapper


@_logged_execution
def execute_remote(
    func: Callable, resources: Any, project: Path | None, *args: Any, **kwargs: Any
) -> Any:
    """
    Serializes a function call and executes it through the controller.

    Uses `NEST_CONTROLLER_ADDRESS` when it is nonempty. Otherwise, connects to
    `nest-tag.tail8e5ba1.ts.net:50051`. The insecure gRPC call has a 900-second
    deadline unless `NEST_RPC_TIMEOUT` sets another positive number of seconds.
    The call is never retried. Only send trusted functions and accept results from
    trusted hosts: deserializing Python objects can execute code.

    Args:
        func (`Callable`): Function to serialize with cloudpickle.
        resources (`Any`): Object with `gpu` and `cpu` resource counts.
        project (`Path | None`): Directory whose `pyproject.toml` and `uv.lock`
            are sent with the request. `None` omits environment files.
        *args (`Any`): Positional arguments for the function.
        **kwargs (`Any`): Keyword arguments for the function.

    Returns:
        `Any`: The decoded worker result.

    Raises:
        `OSError`: If project environment files cannot be read.
        `ValueError`: If `NEST_RPC_TIMEOUT` is invalid.
        `ConnectionError`: If gRPC reports that the controller is unavailable.
        [`errors.RpcTimeoutError`]: If the RPC deadline expires.
        [`errors.InfrastructureError`]: If the Controller reports a classified SSH or
            Slurm failure, such as [`errors.SSHTimeoutError`],
            [`errors.QueueTimeoutError`], [`errors.ExecutionTimeoutError`], or
            [`errors.SubmissionUnknownError`]. These are also `grpc.RpcError`s.
        `grpc.RpcError`: If another RPC error occurs.
        [`errors.RemoteExecutionError`]: If the result contains a remote error record.
    """
    # Serialize function and arguments
    payload = cloudpickle.dumps((func, args, kwargs))

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

    DEFAULT_CONTROLLER_ADDRESS = "nest-tag.tail8e5ba1.ts.net:50051"

    address = os.getenv("NEST_CONTROLLER_ADDRESS") or DEFAULT_CONTROLLER_ADDRESS
    timeout = _rpc_timeout()

    logger.info(
        "Calling controller address=%s gpu=%s cpu=%s payload_bytes=%s",
        address,
        resources.gpu,
        resources.cpu,
        len(payload),
    )

    # Connect to Controller
    try:
        with grpc.insecure_channel(address) as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)
            response = stub.Execute(
                request,
                timeout=timeout,
                metadata=(("x-nest-request-id", current_request_id()),),
            )

    except grpc.RpcError as exc:
        # Errors raised by stub calls are also `grpc.Call`s, which carry the status.
        status = cast(grpc.Call, exc).code()
        logger.error("Controller RPC failed address=%s status=%s", address, status.name)
        error = _controller_error(exc, address, timeout)
        if error is None:
            raise
        raise error from exc

    logger.debug("Result received result_bytes=%s", len(response.result))

    # Deserialze result or raise a remote exception
    return _decode_remote_result(response.result)


def _decode_remote_result(data: bytes) -> Any:
    """
    Deserializes a trusted worker result and recognizes remote error records.

    Args:
        data (`bytes`): Cloudpickle-serialized return value or error record.

    Returns:
        `Any`: The deserialized value when it is not a complete error record.

    Raises:
        [`errors.RemoteExecutionError`]: If the value has the remote error marker and
            string values for its type, message, and traceback.
    """
    result = cloudpickle.loads(data)

    if (
        isinstance(result, dict)
        and result.get("__nest_remote_error_v1__") is True
        and all(isinstance(result.get(key), str) for key in ("type", "message", "traceback"))
    ):
        raise RemoteExecutionError(
            remote_type=result["type"],
            remote_message=result["message"],
            remote_traceback=result["traceback"],
        )

    return result
