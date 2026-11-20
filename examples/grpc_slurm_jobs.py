import os

from nest.client import _channel, session_metadata
from nest.config import load_client_config
from nest.proto import execution_pb2, execution_pb2_grpc


def main():
    """
    Submits, queries, and requests cancellation of a Slurm job over gRPC.

    Uses the SDK's Controller address and TLS settings, and sends the Nest session
    and `NEST_PROJECT_ID` when the Controller requires them. Reads the remote work
    directory from `NEST_SLURM_WORKDIR` and requests cancellation in a `finally`
    block after querying the submitted job.
    """
    config = load_client_config()
    metadata = session_metadata(config)
    with _channel(config) as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)

        # Submit
        response = stub.SubmitJob(
            execution_pb2.SubmitJobRequest(
                workdir=os.environ["NEST_SLURM_WORKDIR"],
                script="cancel-test.sbatch",
                gpu=1,
                account="ACD115198",
                time="00:02:00",
            ),
            timeout=30,
            metadata=metadata,
        )

        job_id = response.job_id
        print(f"Submitted: {job_id}")

        # Status
        try:
            status = stub.GetJobStatus(
                execution_pb2.JobStatusRequest(job_id=job_id),
                timeout=30,
                metadata=metadata,
            )
            print(f"Before: {status.state}")
        finally:
            # Cancel the submitted job
            stub.CancelJob(
                execution_pb2.CancelJobRequest(job_id=job_id),
                timeout=30,
                metadata=metadata,
            )

        print("Cancellation requested")


if __name__ == "__main__":
    main()
