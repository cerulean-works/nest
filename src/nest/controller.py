import os
import signal
import cloudpickle
import grpc
from concurrent import futures

from .executor import execute
from .proto import execution_pb2, execution_pb2_grpc
from .backends.slurm import SlurmBackend, SlurmConfig

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
            
            print(
                "Received environment: "
                f"pyproject.toml={len(env.pyproject_toml)} bytes, "
                f"uv.lock={len(env.uv_lock)} bytes",
                flush=True
            )
        
        mode = os.getenv("NEST_EXECUTOR", "local")
        
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
                    print("Preparing Python environment...", flush=True)
                    
                    env_dir = self.slurm_backend.prepare_environment_bytes(
                        pyproject_toml=env.pyproject_toml,
                        uv_lock=env.uv_lock,
                        gpu=request.gpu,
                    )
                    
                    environment_id = env_dir.rsplit("/", 1)[-1]
                    
                    print(f"Environment ready: {env_dir}", flush=True)
                    print(f"Environment ID: {environment_id}", flush=True)
                
                # Submit worker with environment ID
                job_id, job_dir = self.slurm_backend.submit_payload(
                    request.payload,
                    gpu=request.gpu,
                    environment_id=environment_id,
                )
                
                print(f"Submitted Slurm job: {job_id}", flush=True)
                
                # Wait for the result
                result_bytes = self.slurm_backend.wait_result(
                    job_id,
                    job_dir,
                    timeout=600
                )
                
                return execution_pb2.ExecuteResponse(result=result_bytes)
                    
            except (RuntimeError, TimeoutError, ValueError) as exc:
                context.abort(
                    grpc.StatusCode.INTERNAL,
                    str(exc),
                )

        # Original local executor
        func, args, kwargs = cloudpickle.loads(request.payload)
        result = execute(func, *args, **kwargs)
        return execution_pb2.ExecuteResponse(result=cloudpickle.dumps(result))
    
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
            context.abort(
                grpc.StatusCode.INTERNAL,
                str(exc),
            )
        
        return execution_pb2.JobStatusResponse(state=state)
    
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
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                str(exc),
            )
            
        return execution_pb2.SubmitJobResponse(job_id=job_id)
    
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
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                str(exc),
            )
            
        return execution_pb2.CancelJobResponse()
    
def main():
    """
    Starts the controller and waits until it stops.

    Binds to `NEST_CONTROLLER_BIND`, which defaults to `127.0.0.1:50051`, with
    four RPC worker threads and port reuse disabled. Configures a Slurm backend
    when both `NEST_SLURM_HOST` and `NEST_SLURM_ACCOUNT` are nonempty. Uses
    `NEST_SLURM_WORKDIR` for worker files and its `jobs` subdirectory for payloads.
    Slurm mode requires a host, account, and absolute work directory. Handles
    SIGTERM and SIGINT by allowing active RPCs up to ten seconds to finish.
    """
    ssh_host = os.getenv("NEST_SLURM_HOST")
    account = os.getenv("NEST_SLURM_ACCOUNT")
    workdir = os.getenv("NEST_SLURM_WORKDIR")
    bind_address = os.getenv("NEST_CONTROLLER_BIND", "127.0.0.1:50051")

    if os.getenv("NEST_EXECUTOR", "local") == "slurm":
        missing = [
            name for name, value in (
                ("NEST_SLURM_HOST", ssh_host),
                ("NEST_SLURM_ACCOUNT", account),
                ("NEST_SLURM_WORKDIR", workdir),
            ) if not value or not value.strip()
        ]
        if missing:
            raise ValueError("Slurm execution requires: " + ", ".join(missing))
        if not workdir.startswith("/"):
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
            )
        )
        
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
        ExecutionService(slurm_backend),
        server,
    )
    
    def stop_server(signum, frame):
        """Stops accepting RPCs and gives active calls ten seconds to finish."""
        server.stop(grace=10)

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)

    try:
        if not server.add_insecure_port(bind_address):
            raise RuntimeError(f"Cannot bind Controller to {bind_address}")
        server.start()
        print(f"nest Controller listening on {bind_address}", flush=True)
        server.wait_for_termination()
    finally:
        server.stop(grace=0)

if __name__ == "__main__":
    main()
