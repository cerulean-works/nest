from concurrent import futures

import cloudpickle
import grpc

from .executor import execute
from .proto import execution_pb2, execution_pb2_grpc

class ExecutionService(execution_pb2_grpc.ExecutionServiceServicer):
    def Execute(self, request, context):
        # Deserialize function and arguments
        func, args, kwargs = cloudpickle.loads(request.payload)
        print(f"Received job: GPU={request.gpu}, CPU={request.cpu}", flush=True)
        
        # Execute using exeisting subprocess executor
        result = execute(func, *args, **kwargs)
        
        # Serialize result
        return execution_pb2.ExecuteResponse(result=cloudpickle.dumps(result))

def main():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
        ExecutionService(),
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
