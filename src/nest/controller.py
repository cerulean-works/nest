import os
import time
from functools import wraps

import cloudpickle
import grpc
from concurrent import futures

from ._logging import configure_logging, get_logger, request_context
from .executor import execute
from .proto import execution_pb2, execution_pb2_grpc
from .backends.slurm import SlurmBackend, SlurmConfig

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
                        method.__name__, status.name, context.details(),
                        (time.monotonic() - started) * 1000,
                    )
                else:
                    logger.exception(
                        "RPC failed method=%s elapsed_ms=%.1f",
                        method.__name__, (time.monotonic() - started) * 1000,
                    )
                raise
            getattr(logger, level)(
                "RPC completed method=%s elapsed_ms=%.1f",
                method.__name__, (time.monotonic() - started) * 1000,
            )
            return response
    return wrapper


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
        
    @_logged_rpc
    def Execute(self, request, context):
        """
        Executes a serialized call locally or through the configured Slurm backend.

        Selects Slurm only when `NEST_EXECUTOR` is `slurm`; all other values use a
        local subprocess. Slurm execution prepares any supplied environment before
        submitting the worker and waits up to 600 seconds for its result. The request's
        CPU count is not passed to the Slurm backend.

        Args:
            request (`execution_pb2.ExecuteRequest`): Serialized call, resource counts,
                and optional environment files.
            context (`grpc.ServicerContext`): RPC context used to abort with
                `INVALID_ARGUMENT` for empty environment files, `FAILED_PRECONDITION`
                for an absent Slurm backend, or `INTERNAL` for handled Slurm failures.

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
                len(env.pyproject_toml), len(env.uv_lock),
            )
        
        mode = os.getenv("NEST_EXECUTOR", "local")
        logger.info(
            "Execution received executor=%s gpu=%s cpu=%s payload_bytes=%s",
            mode, request.gpu, request.cpu, len(request.payload),
        )
        
        if mode == "slurm":
            if self.slurm_backend is None:
                context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "Slurm backend is not configured",
                )
                
            try:
                environment_id = None
                
                # Prepare Python environment
                if request.HasField("environment"):
                    env = request.environment
                    logger.info("Preparing Python environment")
                    
                    env_dir = self.slurm_backend.prepare_environment_bytes(
                        pyproject_toml=env.pyproject_toml,
                        uv_lock=env.uv_lock,
                        gpu=request.gpu,
                    )
                    
                    environment_id = env_dir.rsplit("/", 1)[-1]
                    
                    logger.info("Environment ready environment_id=%s", environment_id)
                
                # Submit worker with environment ID
                job_id, job_dir = self.slurm_backend.submit_payload(
                    request.payload,
                    gpu=request.gpu,
                    environment_id=environment_id,
                )
                
                logger.info("Worker submitted job_id=%s job_dir=%s", job_id, job_dir)
                
                # Wait for the result
                result_bytes = self.slurm_backend.wait_result(
                    job_id,
                    job_dir,
                    timeout=600
                )
                
                return execution_pb2.ExecuteResponse(result=result_bytes)
                    
            except (RuntimeError, TimeoutError, ValueError) as exc:
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
                Uses `FAILED_PRECONDITION` when the backend is absent and `INTERNAL`
                when a backend status query fails.

        Returns:
            `execution_pb2.JobStatusResponse`: The job operation response.
        """
        if self.slurm_backend is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Slurm backend is not configured",
            )
            
        try:
            state = self.slurm_backend.status(request.job_id)
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
                Uses `INVALID_ARGUMENT` for a negative GPU count and
                `FAILED_PRECONDITION` for an absent backend or a submission runtime error.

        Returns:
            `execution_pb2.SubmitJobResponse`: The job operation response.
        """
        if self.slurm_backend is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Slurm backend is not configured",
            )
        
        if request.gpu < 0:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "GPU count cannot be negative",
            )
            
        try:
            job_id = self.slurm_backend.submit(
                workdir=request.workdir,
                script=request.script,
                gpu=request.gpu,
                account=request.account or None,
                partition=request.partition or None,
                time=request.time or None,
            )
        except RuntimeError as exc:
            logger.exception("Batch job submission failed script=%s", request.script)
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                str(exc),
            )
            
        logger.info("Batch job submitted job_id=%s", job_id)
        return execution_pb2.SubmitJobResponse(job_id=job_id)
    
    @_logged_rpc
    def CancelJob(self, request, context):
        """
        Requests cancellation of a Slurm job.

        Args:
            request (`execution_pb2.CancelJobRequest`): Job operation parameters.
            context (`grpc.ServicerContext`): RPC context for reporting failures.
                Uses `INVALID_ARGUMENT` for a nonpositive job ID and
                `FAILED_PRECONDITION` for an absent backend or a cancellation runtime error.

        Returns:
            `execution_pb2.CancelJobResponse`: The job operation response.
        """
        if self.slurm_backend is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Slurm backend is not configured",
            )
            
        if request.job_id <= 0:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Invalid job ID",
            )
        
        try:
            self.slurm_backend.cancel(request.job_id)
        except RuntimeError as exc:
            logger.exception("Job cancellation failed job_id=%s", request.job_id)
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                str(exc),
            )
            
        logger.info("Job cancellation requested job_id=%s", request.job_id)
        return execution_pb2.CancelJobResponse()
    
def main():
    """
    Starts the controller and waits until it stops.

    Binds to `NEST_CONTROLLER_BIND`, which defaults to `127.0.0.1:50051`, with
    four RPC worker threads and port reuse disabled. Configures a Slurm backend
    when both `NEST_SLURM_HOST` and `NEST_SLURM_ACCOUNT` are nonempty. Uses
    `NEST_SLURM_WORKDIR` for worker files and its `jobs` subdirectory for payloads.
    Stops the server immediately if waiting for termination raises an exception.
    """
    configure_logging()
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.so_reuseport", 0),
        ],
    )
    
    slurm_backend = None
    ssh_host = os.getenv("NEST_SLURM_HOST")
    account = os.getenv("NEST_SLURM_ACCOUNT")
    workdir = os.getenv("NEST_SLURM_WORKDIR")
    bind_address = os.getenv("NEST_CONTROLLER_BIND", "127.0.0.1:50051")
    
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
            )
        )
        
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
        ExecutionService(slurm_backend),
        server,
    )
    
    server.add_insecure_port(bind_address)
    server.start()
    logger.info(
        "Controller listening address=%s executor=%s slurm_configured=%s",
        bind_address, os.getenv("NEST_EXECUTOR", "local"), slurm_backend is not None,
    )
    
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Controller interrupted")
        server.stop(grace=0)
    except BaseException:
        logger.exception("Controller stopped unexpectedly")
        server.stop(grace=0)
    finally:
        logger.info("Controller stopped")

if __name__ == "__main__":
    main()
