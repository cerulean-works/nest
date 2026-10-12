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
        # Deserialize function and arguments
        func, args, kwargs = cloudpickle.loads(request.payload)
        print(f"Received job: GPU={request.gpu}, CPU={request.cpu}", flush=True)
        
        # Execute using exeisting subprocess executor
        result = execute(func, *args, **kwargs)
        
        # Serialize result
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
    ssh_host = os.getenv("NESTAPPT_SLURM_HOST")
    account = os.getenv("NESTAPPT_SLURM_ACCOUNT")
    
    if ssh_host and account:
        slurm_backend = SlurmBackend(
            SlurmConfig(
                ssh_host=ssh_host,
                account=account,
                partition="dev",
            )
        )
        
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
        ExecutionService(slurm_backend),
        server,
    )
    
    server.add_insecure_port("127.0.0.1:50051")
    server.start()
    print("nestappt Controller listening on 127.0.0.1:50051")
    
    try:
        server.wait_for_termination()
    except:
        server.stop(grace=0)

if __name__ == "__main__":
    main()
