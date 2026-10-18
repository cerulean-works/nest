import os
import cloudpickle
import grpc
from concurrent import futures

from .executor import execute
from .proto import execution_pb2, execution_pb2_grpc
from .backends.slurm import SlurmBackend, SlurmConfig

class ExecutionService(execution_pb2_grpc.ExecutionServiceServicer):
    def __init__(self, slurm_backend=None):
        self.slurm_backend = slurm_backend
        
    def Execute(self, request, context):
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
                # Prepare Python environment
                if request.HasField("environment"):
                    env = request.environment
                    print("Preparing Python environment...", flush=True)
                    
                    env_dir = self.slurm_backend.prepare_environment_bytes(
                        pyproject_toml=env.pyproject_toml,
                        uv_lock=env.uv_lock,
                        gpu=request.gpu,
                    )
                    
                    print(f"Environment ready: {env_dir}", flush=True)
                
                # Upload payload and submit Slurm job
                job_id, job_dir = self.slurm_backend.submit_payload(
                    request.payload,
                    gpu=request.gpu,
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
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    
    slurm_backend = None
    ssh_host = os.getenv("NEST_SLURM_HOST")
    account = os.getenv("NEST_SLURM_ACCOUNT")
    workdir = os.getenv("NEST_SLURM_WORKDIR")
    
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
    
    server.add_insecure_port("127.0.0.1:50051")
    server.start()
    print("nest Controller listening on 127.0.0.1:50051")
    
    try:
        server.wait_for_termination()
    except:
        server.stop(grace=0)

if __name__ == "__main__":
    main()
