import os
import signal
import time
from concurrent import futures
from functools import wraps

import cloudpickle
import grpc

from ._logging import configure_logging, get_logger, request_context
from .backends.slurm import SlurmBackend, SlurmConfig
from .errors import ERROR_KIND_METADATA, JOB_ID_METADATA, InfrastructureError, RemoteCommandError
from .executor import execute
from .proto import execution_pb2, execution_pb2_grpc

logger = get_logger("nest.controller")


def _logged_rpc(method):
    @wraps(method)
    def wrapper(self, request, context):
        incoming_id = next(
            (value for key, value in context.invocation_metadata() if key == "x-nest-request-id"),
            None,
        )
        level = "debug" if method.__name__ == "GetJobStatus" else "info"
        with request_context(incoming_id):
            started = time.monotonic()
            getattr(logger, level)("RPC started method=%s", method.__name__)
            try:
                response = method(self, request, context)
            except Exception:
                status = context.code()
                if status is not None and status != grpc.StatusCode.OK:
                    logger.warning(
                        "RPC aborted method=%s status=%s details=%s elapsed_ms=%.1f",
                        method.__name__,
                        status.name,
                        context.details(),
                        (time.monotonic() - started) * 1000,
                    )
                else:
                    logger.exception(
                        "RPC failed method=%s elapsed_ms=%.1f",
                        method.__name__,
                        (time.monotonic() - started) * 1000,
                    )
                raise
            getattr(logger, level)(
                "RPC completed method=%s elapsed_ms=%.1f",
                method.__name__,
                (time.monotonic() - started) * 1000,
            )
            return response

    return wrapper


def _abort_infrastructure(context, exc: InfrastructureError, status=None):
    """
    Aborts an RPC with an infrastructure error's status, kind, and job ID.

    Args:
        context (`grpc.ServicerContext`): RPC context to abort.
        exc ([`errors.InfrastructureError`]): Error to report.
        status (`grpc.StatusCode`, *optional*): Overrides the error's own status.
    """
    metadata = [(ERROR_KIND_METADATA, exc.kind)]
    if exc.job_id is not None:
        metadata.append((JOB_ID_METADATA, str(exc.job_id)))
    context.set_trailing_metadata(tuple(metadata))
    context.abort(status or exc.status, str(exc))


def _positive_seconds(name: str, default: float) -> float:
    """
    Reads a positive number of seconds from an environment variable.

    Args:
        name (`str`): Environment variable name.
        default (`float`): Value used when the variable is unset or empty.

    Returns:
        `float`: The configured number of seconds.

    Raises:
        `ValueError`: If the value is not a positive finite number.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not 0 < value < float("inf"):
        raise ValueError(f"{name} must be a positive number of seconds, got {raw!r}")
    return value


class ExecutionService(execution_pb2_grpc.ExecutionServiceServicer):
    """Handles execution requests and Slurm job operations for the controller."""

    def __init__(self, slurm_backend=None):
        """
        Stores the optional Slurm backend used by the RPC handlers.

        Args:
            slurm_backend ([`backends.slurm.SlurmBackend`], *optional*): Backend for
                Slurm execution and job operations. Local execution needs no backend.
        """
        self.slurm_backend = slurm_backend

    def _require_slurm_backend(self, context) -> SlurmBackend:
        """
        Returns the Slurm backend, aborting the RPC when none is configured.

        Args:
            context (`grpc.ServicerContext`): RPC context, aborted with
                `FAILED_PRECONDITION` when no backend is configured.

        Returns:
            [`backends.slurm.SlurmBackend`]: The configured backend.
        """
        if self.slurm_backend is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Slurm backend is not configured",
            )
            raise AssertionError("context.abort raises")
        return self.slurm_backend

    @_logged_rpc
    def Execute(self, request, context):
        """
        Executes a serialized call locally or through the configured Slurm backend.

        Selects Slurm only when `NEST_EXECUTOR` is `slurm`; all other values use a
        local subprocess. Slurm execution prepares any supplied environment before
        submitting the worker once and waits for its result (600 seconds by default).
        The request's `gpu` and `cpu` counts are forwarded to both the environment
        build and the worker job; a count of `0` omits the corresponding `sbatch` flag.

        Args:
            request (`execution_pb2.ExecuteRequest`): Serialized call, resource counts,
                and optional environment files.
            context (`grpc.ServicerContext`): RPC context used to abort with
                `INVALID_ARGUMENT` for empty environment files or negative resource
                counts, `FAILED_PRECONDITION` for an absent Slurm backend, and the
                status of each [`errors.InfrastructureError`] with its kind in the
                `nest-error-kind` trailing metadata.

        Returns:
            `execution_pb2.ExecuteResponse`: Serialized return value or worker error record.
        """
        if request.HasField("environment"):
            env = request.environment

            if not env.pyproject_toml or not env.uv_lock:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Environment requires pyproject.toml and uv.lock",
                )

            logger.debug(
                "Environment received pyproject_bytes=%s lock_bytes=%s",
                len(env.pyproject_toml),
                len(env.uv_lock),
            )

        if request.gpu < 0 or request.cpu < 0:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Resource counts cannot be negative",
            )

        mode = os.getenv("NEST_EXECUTOR", "local")
        logger.info(
            "Execution received executor=%s gpu=%s cpu=%s payload_bytes=%s",
            mode,
            request.gpu,
            request.cpu,
            len(request.payload),
        )

        if mode == "slurm":
            backend = self._require_slurm_backend(context)

            try:
                environment_id = None

                # Prepare Python environment
                if request.HasField("environment"):
                    env = request.environment
                    logger.info("Preparing Python environment")

                    env_dir = backend.prepare_environment_bytes(
                        pyproject_toml=env.pyproject_toml,
                        uv_lock=env.uv_lock,
                        gpu=request.gpu,
                        cpu=request.cpu,
                    )

                    environment_id = env_dir.rsplit("/", 1)[-1]

                    logger.info("Environment ready environment_id=%s", environment_id)

                # Submit worker with environment ID
                job_id, job_dir = backend.submit_payload(
                    request.payload,
                    gpu=request.gpu,
                    cpu=request.cpu,
                    environment_id=environment_id,
                )

                logger.info("Worker submitted job_id=%s job_dir=%s", job_id, job_dir)

                # Wait for the result
                result_bytes = backend.wait_result(job_id, job_dir)

                return execution_pb2.ExecuteResponse(result=result_bytes)

            except InfrastructureError as exc:
                logger.exception("Slurm execution failed kind=%s", exc.kind)
                _abort_infrastructure(context, exc)
            except (RuntimeError, ValueError) as exc:
                logger.exception("Slurm execution failed")
                context.abort(
                    grpc.StatusCode.INTERNAL,
                    str(exc),
                )

        # Original local executor
        func, args, kwargs = cloudpickle.loads(request.payload)
        result = execute(func, *args, **kwargs)
        return execution_pb2.ExecuteResponse(result=cloudpickle.dumps(result))

    @_logged_rpc
    def GetJobStatus(self, request, context):
        """
        Returns the current or recorded Slurm state of a job.

        Args:
            request (`execution_pb2.JobStatusRequest`): Job operation parameters.
            context (`grpc.ServicerContext`): RPC context for reporting failures.
                Uses `FAILED_PRECONDITION` when the backend is absent, the status and
                kind of an [`errors.InfrastructureError`], and `INTERNAL` otherwise.

        Returns:
            `execution_pb2.JobStatusResponse`: The job operation response.
        """
        backend = self._require_slurm_backend(context)

        try:
            state = backend.status(request.job_id)
        except InfrastructureError as exc:
            logger.exception("Job status query failed job_id=%s", request.job_id)
            _abort_infrastructure(context, exc)
        except Exception as exc:
            logger.exception("Job status query failed job_id=%s", request.job_id)
            context.abort(
                grpc.StatusCode.INTERNAL,
                str(exc),
            )

        logger.debug("Job status job_id=%s state=%s", request.job_id, state)
        return execution_pb2.JobStatusResponse(state=state)

    @_logged_rpc
    def SubmitJob(self, request, context):
        """
        Submits a batch script through the Slurm backend.

        Args:
            request (`execution_pb2.SubmitJobRequest`): Job operation parameters.
            context (`grpc.ServicerContext`): RPC context for reporting failures.
                Uses `INVALID_ARGUMENT` for a negative GPU count,
                `FAILED_PRECONDITION` for an absent backend or a rejected submission, and
                the status and kind of other [`errors.InfrastructureError`] failures,
                such as `UNKNOWN` with kind `submission_unknown` when SSH timed out
                during `sbatch`. Never resubmits.

        Returns:
            `execution_pb2.SubmitJobResponse`: The job operation response.
        """
        backend = self._require_slurm_backend(context)

        if request.gpu < 0:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "GPU count cannot be negative",
            )

        try:
            job_id = backend.submit(
                workdir=request.workdir,
                script=request.script,
                gpu=request.gpu,
                account=request.account or None,
                partition=request.partition or None,
                time=request.time or None,
            )
        except RemoteCommandError as exc:
            logger.exception("Batch job submission failed script=%s", request.script)
            _abort_infrastructure(context, exc, grpc.StatusCode.FAILED_PRECONDITION)
        except InfrastructureError as exc:
            logger.exception(
                "Batch job submission failed script=%s kind=%s", request.script, exc.kind
            )
            _abort_infrastructure(context, exc)

        logger.info("Batch job submitted job_id=%s", job_id)
        return execution_pb2.SubmitJobResponse(job_id=job_id)

    @_logged_rpc
    def CancelJob(self, request, context):
        """
        Requests cancellation of a Slurm job.

        Args:
            request (`execution_pb2.CancelJobRequest`): Job operation parameters.
            context (`grpc.ServicerContext`): RPC context for reporting failures.
                Uses `INVALID_ARGUMENT` for a nonpositive job ID,
                `FAILED_PRECONDITION` for an absent backend or a rejected cancellation,
                and the status and kind of other [`errors.InfrastructureError`] failures.

        Returns:
            `execution_pb2.CancelJobResponse`: The job operation response.
        """
        backend = self._require_slurm_backend(context)

        if request.job_id <= 0:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Invalid job ID",
            )

        try:
            backend.cancel(request.job_id)
        except RemoteCommandError as exc:
            logger.exception("Job cancellation failed job_id=%s", request.job_id)
            _abort_infrastructure(context, exc, grpc.StatusCode.FAILED_PRECONDITION)
        except InfrastructureError as exc:
            logger.exception("Job cancellation failed job_id=%s kind=%s", request.job_id, exc.kind)
            _abort_infrastructure(context, exc)

        logger.info("Job cancellation requested job_id=%s", request.job_id)
        return execution_pb2.CancelJobResponse()


def main():
    """
    Starts the controller and waits until it stops.

    Binds to `NEST_CONTROLLER_BIND`, which defaults to `127.0.0.1:50051`, with
    four RPC worker threads and port reuse disabled. Configures a Slurm backend
    when both `NEST_SLURM_HOST` and `NEST_SLURM_ACCOUNT` are nonempty. Uses
    `NEST_SLURM_WORKDIR` for worker files and its `jobs` subdirectory for payloads.
    Slurm mode requires a host, account, and absolute work directory.
    `NEST_SSH_TIMEOUT`, `NEST_RESULT_TIMEOUT`, `NEST_ENVIRONMENT_TIMEOUT`, and
    `NEST_POLL_INTERVAL` optionally override the backend's 60, 600, 1,800, and
    5 second defaults. Handles SIGTERM and SIGINT by allowing active RPCs up to
    ten seconds to finish.

    Raises:
        `ValueError`: If Slurm settings are missing or invalid, or a timeout
            override is not a positive number.
    """
    configure_logging()
    ssh_host = os.getenv("NEST_SLURM_HOST")
    account = os.getenv("NEST_SLURM_ACCOUNT")
    workdir = os.getenv("NEST_SLURM_WORKDIR")
    bind_address = os.getenv("NEST_CONTROLLER_BIND", "127.0.0.1:50051")
    timeouts = {
        "ssh_timeout": _positive_seconds("NEST_SSH_TIMEOUT", 60),
        "result_timeout": _positive_seconds("NEST_RESULT_TIMEOUT", 600),
        "environment_timeout": _positive_seconds("NEST_ENVIRONMENT_TIMEOUT", 1800),
        "poll_interval": _positive_seconds("NEST_POLL_INTERVAL", 5),
    }

    if os.getenv("NEST_EXECUTOR", "local") == "slurm":
        missing = [
            name
            for name, value in (
                ("NEST_SLURM_HOST", ssh_host),
                ("NEST_SLURM_ACCOUNT", account),
                ("NEST_SLURM_WORKDIR", workdir),
            )
            if not value or not value.strip()
        ]
        if missing:
            raise ValueError("Slurm execution requires: " + ", ".join(missing))
        if workdir is None or not workdir.startswith("/"):
            raise ValueError("NEST_SLURM_WORKDIR must be an absolute HPC path")

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[("grpc.so_reuseport", 0)],
    )

    slurm_backend = None

    if ssh_host and account:
        slurm_backend = SlurmBackend(
            SlurmConfig(
                ssh_host=ssh_host,
                account=account,
                partition="dev",
                time="00:05:00",
                work_root=f"{workdir.rstrip('/')}/jobs" if workdir else None,
                worker_workdir=workdir,
                **timeouts,
            )
        )

    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
        ExecutionService(slurm_backend),
        server,
    )

    def stop_server(signum, frame):
        """Stops accepting RPCs and gives active calls ten seconds to finish."""
        logger.info("Controller stopping signal=%s", signum)
        server.stop(grace=10)

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)

    try:
        if not server.add_insecure_port(bind_address):
            raise RuntimeError(f"Cannot bind Controller to {bind_address}")
        server.start()
        logger.info(
            "Controller listening address=%s executor=%s slurm_configured=%s",
            bind_address,
            os.getenv("NEST_EXECUTOR", "local"),
            slurm_backend is not None,
        )
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Controller interrupted")
    except BaseException:
        logger.exception("Controller stopped unexpectedly")
        raise
    finally:
        server.stop(grace=0)
        logger.info("Controller stopped")


if __name__ == "__main__":
    main()
