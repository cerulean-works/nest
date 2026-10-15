import os
import grpc

from nest.proto import execution_pb2, execution_pb2_grpc

def main():
    address = os.getenv(
        "NEST_CONTROLLER_ADDRESS",
        "127.0.0.1:50051",
    )
    
    with grpc.insecure_channel(address) as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        
        # Submit
        response = stub.SubmitJob(
            execution_pb2.SubmitJobRequest(
                workdir=os.environ["NEST_SLURM_WORKDIR"],
                script="cancel-test.sbatch",
                gpu=1,
                account="ACD115198",
                partition="dev",
                time="00:02:00",
            ),
            timeout=30,
        )
        
        job_id = response.job_id
        print(f"Submitted: {job_id}")
        
        # Status
        try:
            status = stub.GetJobStatus(
                execution_pb2.JobStatusRequest(job_id=job_id),
                timeout=30,
            )
            print(f"Before: {status.state}")
        finally:
            # Cancel the submitted job
            stub.CancelJob(
                execution_pb2.CancelJobRequest(job_id=job_id),
                timeout=30,
            )
            
        print("Cancellation requested")
        
if __name__ == "__main__":
    main()
