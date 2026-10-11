import sys
import grpc

from nestappt.proto import execution_pb2, execution_pb2_grpc

def main():
    job_id = int(sys.argv[1])
    
    with grpc.insecure_channel("127.0.0.1:50051") as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        
        response = stub.GetJobStatus(
            execution_pb2.JobStatusRequest(job_id=job_id),
            timeout=30,
        )
        
    print(f"Job {job_id}: {response.state}")
    
if __name__ == "__main__":
    main()
