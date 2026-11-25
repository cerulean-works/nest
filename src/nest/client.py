import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import cloudpickle
import grpc

from ._logging import current_request_id, get_logger, operation, request_context
from .config import (
    DEFAULT_CONTROLLER_ADDRESS,
    HOSTED_CONTROLLER_ADDRESS,
    ClientConfig,
    load_client_config,
)
from .errors import (
    AUTHORIZATION_METADATA,
    ERROR_KIND_METADATA,
    INFRASTRUCTURE_ERRORS,
    JOB_ID_METADATA,
    PENDING_REASON_METADATA,
    PROJECT_METADATA,
    AuthenticationRequiredError,
    QueueTimeoutError,
    RemoteAccessDeniedError,
    RemoteExecutionError,
    RpcTimeoutError,
)
from .project_selection import ProjectChoice, forget_refused, is_interactive, select_project
from .proto import execution_pb2, execution_pb2_grpc

if TYPE_CHECKING:
    from .login import NestSession

logger = get_logger(__name__)


def channel_options(config: ClientConfig) -> list[tuple[str, int]]:
    """
    The channel options of a Controller connection.

    Retries are off, so the channel never sends a call twice: an `Execute` reaches
    the Controller at most once per attempt the SDK makes. With a keepalive (the
    hosted profile, or `NEST_CONTROLLER_KEEPALIVE`), HTTP/2 PINGs keep NAT and
    firewall state alive during a silent call, at an interval the Controller and
    the gateway accept, and never while no call is open.
    """
    options = [("grpc.enable_retries", 0)]
    if config.keepalive > 0:
        options += [
            ("grpc.keepalive_time_ms", int(config.keepalive * 1000)),
            ("grpc.keepalive_timeout_ms", 20_000),
            ("grpc.keepalive_permit_without_calls", 0),
            ("grpc.http2.max_pings_without_data", 0),
        ]
    return options


def _channel(config: ClientConfig) -> grpc.Channel:
    """
    Opens a plaintext or TLS channel to the configured Controller.

    A TLS channel always verifies the certificate chain and that it names the
    address's host; nothing overrides the expected name, and a failed handshake is
    an error, never a reason to try plaintext.

    Args:
        config ([`config.ClientConfig`]): Validated client configuration.

    Returns:
        `grpc.Channel`: The channel; close it after use.
    """
    options = channel_options(config)
    if not config.tls:
        return grpc.insecure_channel(config.address, options=options)
    roots = config.tls_root_certificates
    if config.tls_ca_file is not None:
        with open(config.tls_ca_file, "rb") as handle:
            roots = handle.read()
    return grpc.secure_channel(config.address, grpc.ssl_channel_credentials(roots), options=options)


def _controller_error(
    exc: grpc.RpcError,
    address: str,
    timeout: float,
    *,
    auth_url: str | None = None,
    hosted: bool = False,
) -> Exception | None:
    """
    Converts a Controller RPC error into a typed Nest exception when possible.

    Args:
        exc (`grpc.RpcError`): Error raised by the stub call.
        address (`str`): Controller address, used in messages.
        timeout (`float`): RPC deadline in seconds, used in messages.
        auth_url (`str`, *optional*): The sign-in origin whose session was used,
            used in messages.
        hosted (`bool`): Whether `address` is the hosted profile's gateway.

    Returns:
        `Exception | None`: The exception to raise, or `None` to re-raise `exc`.
    """
    call = cast(grpc.Call, exc)
    metadata = {key: value for key, value in call.trailing_metadata() or ()}
    error_class = INFRASTRUCTURE_ERRORS.get(str(metadata.get(ERROR_KIND_METADATA, "")))

    if error_class is not None:
        job_id = str(metadata.get(JOB_ID_METADATA, ""))
        message = call.details() or ""
        parsed_id = int(job_id) if job_id.isdecimal() else None
        if issubclass(error_class, QueueTimeoutError):
            reason = str(metadata.get(PENDING_REASON_METADATA, "")) or None
            return error_class(message, job_id=parsed_id, pending_reason=reason)
        return error_class(message, job_id=parsed_id)

    status = call.code()
    if status == grpc.StatusCode.UNAUTHENTICATED:
        if auth_url is None:
            return AuthenticationRequiredError(
                f"Nest Controller at {address} requires a Nest session. The SDK sends one "
                f"only to the hosted Controller ({HOSTED_CONTROLLER_ADDRESS}) or to the Nest "
                "that NEST_AUTH_URL names: set NEST_AUTH_URL to this Controller's sign-in "
                "address and run `nest login`."
            )
        return AuthenticationRequiredError(
            f"Nest Controller at {address} requires a Nest session from {auth_url}. "
            f"Run `nest login --url {auth_url}` and try again."
        )
    if status in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.NOT_FOUND):
        return RemoteAccessDeniedError(
            f"Nest Controller at {address} refused the call: {call.details() or status.name}. "
            "Check NEST_PROJECT_ID and your role in that Project. Nothing was run."
        )

    if status == grpc.StatusCode.DEADLINE_EXCEEDED:
        return RpcTimeoutError(
            f"Nest Controller at {address} did not reply within {timeout:g} seconds. "
            "Any Slurm job it submitted was not cancelled. Set NEST_RPC_TIMEOUT to "
            "allow longer calls."
        )

    if status == grpc.StatusCode.UNAVAILABLE and hosted:
        return ConnectionError(
            f"Cannot connect to Cerulean-hosted Nest at {address} over verified TLS.\n\n"
            "Please check that your network allows outgoing HTTPS (port 443), and try "
            "again later. Nest never falls back to an unencrypted connection. To use "
            "another Controller, set NEST_CONTROLLER_ADDRESS (and NEST_CONTROLLER_TLS)."
        )

    if status == grpc.StatusCode.UNAVAILABLE:
        return ConnectionError(
            f"Cannot connect to Nest Controller at {address}.\n\n"
            "Please check:\n"
            "  - NEST_CONTROLLER_ADDRESS names the Controller "
            f"(default {DEFAULT_CONTROLLER_ADDRESS}).\n"
            "  - Your network can reach it, for example through a VPN or Tailnet.\n"
            "  - NEST_CONTROLLER_TLS matches the Controller's TLS setting.\n"
            "  - The Controller is running."
        )

    return None


def _session(config: ClientConfig, *, interactive: bool) -> NestSession | None:
    """
    The stored Nest session of `config.auth_url`, refreshed when needed, or `None`
    when no sign-in origin applies to this Controller (see [`config.ClientConfig`])
    or no one is signed in and `interactive` is `False`. Only the session of
    `config.auth_url` is read, so a Controller never receives another Nest's session.
    """
    if config.auth_url is None:
        return None
    from .config import load_cli_auth_config
    from .login import NotSignedInError, ensure_session

    try:
        return ensure_session(load_cli_auth_config(url=config.auth_url), interactive=interactive)
    except NotSignedInError:
        return None


def _access_token(config: ClientConfig, *, interactive: bool) -> str | None:
    """The access token of [`client._session`], or `None`."""
    session = _session(config, interactive=interactive)
    return session.access_token if session is not None else None


def call_metadata(
    config: ClientConfig, token: str | None, project_id: str | None = None
) -> tuple[tuple[str, str], ...]:
    """
    Request metadata for a Controller call: the request ID, the Project
    (`project_id`, else `NEST_PROJECT_ID`), and the Nest session's bearer token
    when there is one.
    """
    metadata = [("x-nest-request-id", current_request_id())]
    project = project_id or config.project_id
    if project is not None:
        metadata.append((PROJECT_METADATA, project))
    if token is not None:
        metadata.append((AUTHORIZATION_METADATA, f"Bearer {token}"))
    return tuple(metadata)


def session_metadata(
    config: ClientConfig | None = None, *, interactive: bool = True
) -> tuple[tuple[str, str], ...]:
    """
    Metadata for a direct gRPC call to the Controller, such as `SubmitJob`: the
    request ID, the Project, and, when a sign-in origin applies (`NEST_AUTH_URL`,
    or the hosted origin for the hosted Controller), the Nest session's bearer token,
    signing in first when `interactive` and no session is stored.

    The Project is `NEST_PROJECT_ID`, else the one
    [`project_selection.select_project`] chooses (asking on a terminal only when
    `interactive`).

    Args:
        config ([`config.ClientConfig`], *optional*): Defaults to the environment.
        interactive (`bool`): Sign in through the browser, and choose among several
            Projects, when needed.

    Raises:
        [`errors.ProjectSelectionError`]: If no Project can be chosen.
    """
    config = config or load_client_config()
    session = _session(config, interactive=interactive)
    token = session.access_token if session is not None else None
    choice = None
    if config.project_id is None and session is not None:
        with _channel(config) as channel:
            try:
                choice = select_project(
                    config,
                    channel,
                    token,
                    session.user_id,
                    interactive=interactive and is_interactive(),
                )
            except grpc.RpcError as exc:
                error = _controller_error(
                    exc,
                    config.address,
                    config.rpc_timeout,
                    auth_url=config.auth_url,
                    hosted=config.profile == "hosted",
                )
                if error is None:
                    raise
                raise error from exc
    return call_metadata(config, token, choice.project_id if choice else None)


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

    Reads its settings with [`config.load_client_config`]: the hosted connection
    profile (`NEST_PROFILE=hosted`, verified TLS to [`config.HOSTED_GRPC_ADDRESS`]),
    or `NEST_CONTROLLER_ADDRESS`, else [`config.DEFAULT_CONTROLLER_ADDRESS`]. The call
    has a 900-second deadline unless `NEST_RPC_TIMEOUT` sets another positive number
    of seconds. Non-loopback custom addresses require an explicit
    `NEST_CONTROLLER_TLS` setting or `NEST_CONTROLLER_TLS_CA_FILE`. A TLS failure is
    an error; the SDK never retries in plaintext, and the channel never retries.

    With the hosted profile, with `NEST_AUTH_URL` set, or when
    `NEST_CONTROLLER_ADDRESS` names a hosted Controller (see
    [`config.is_hosted_controller`]), the stored Nest session of that origin (see
    [`login.ensure_session`]) is sent as a bearer token. Any other Controller
    receives no session. The call runs in `NEST_PROJECT_ID`, or, when it is unset,
    in the Project [`project_selection.select_project`] chooses for this account
    and Controller; the Controller authorizes it on every call. When the
    Controller requires a session and none is stored, an interactive terminal
    signs in through the browser and the call is sent once more; nothing ran the
    first time. Otherwise the call is never retried. Only send trusted functions
    and accept results from trusted hosts: deserializing Python objects can
    execute code.

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
        [`config.ConfigError`]: If a client setting is invalid. It is a `ValueError`.
        `ConnectionError`: If gRPC reports that the controller is unavailable.
        [`errors.AuthenticationRequiredError`]: If the Controller requires a Nest
            session and none was accepted.
        [`errors.RemoteAccessDeniedError`]: If the Controller's Project policy or
            Provider refused the call. A refused saved Project choice is forgotten.
        [`errors.ProjectSelectionError`]: If `NEST_PROJECT_ID` is unset and no
            Project can be chosen.
        [`login.AuthError`]: If signing in fails.
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

    config = load_client_config()
    address, timeout = config.address, config.rpc_timeout

    logger.info(
        "Calling controller address=%s profile=%s tls=%s gpu=%s cpu=%s payload_bytes=%s",
        address,
        config.profile,
        config.tls,
        resources.gpu,
        resources.cpu,
        len(payload),
    )

    session = _session(config, interactive=False)
    choice: ProjectChoice | None = None

    def attempt(stub, session) -> execution_pb2.ExecuteResponse:
        nonlocal choice
        token = session.access_token if session is not None else None
        user_id = session.user_id if session is not None else None
        # The Project is chosen before every call; the Controller authorizes it anew.
        choice = select_project(config, channel, token, user_id)
        project = choice.project_id if choice is not None else None
        if choice is not None and choice.source != "NEST_PROJECT_ID":
            logger.info("Running in Project project_id=%s source=%s", project, choice.source)
        return stub.Execute(
            request, timeout=timeout, metadata=call_metadata(config, token, project)
        )

    # Connect to Controller
    try:
        with _channel(config) as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)
            try:
                response = attempt(stub, session)
            except grpc.RpcError as exc:
                # Refused before anything ran, so one retry after signing in is safe.
                if not (
                    cast(grpc.Call, exc).code() == grpc.StatusCode.UNAUTHENTICATED
                    and session is None
                    and config.auth_url is not None
                    and sys.stdin.isatty()
                ):
                    raise
                session = _session(config, interactive=True)
                response = attempt(stub, session)

    except grpc.RpcError as exc:
        # Errors raised by stub calls are also `grpc.Call`s, which carry the status.
        status = cast(grpc.Call, exc).code()
        logger.error("Controller RPC failed address=%s status=%s", address, status.name)
        error = _controller_error(
            exc, address, timeout, auth_url=config.auth_url, hosted=config.profile == "hosted"
        )
        if error is None:
            raise
        if (
            isinstance(error, RemoteAccessDeniedError)
            and choice is not None
            and choice.source != "NEST_PROJECT_ID"
        ):
            forgotten = forget_refused(
                config, session.user_id if session is not None else None, choice
            )
            error = RemoteAccessDeniedError(
                f"{error} The Project was {choice.project_id} ({choice.source})"
                + (
                    "; that saved choice was forgotten, so the next call chooses again."
                    if forgotten
                    else "."
                )
            )
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
