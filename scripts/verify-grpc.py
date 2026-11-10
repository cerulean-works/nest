#!/usr/bin/env python3
"""Probe Nest's real RPC handler without executing code, SSH, or a Slurm job."""

import argparse
from typing import cast

import grpc

from nest.proto import execution_pb2, execution_pb2_grpc


def verify(address: str, timeout: float = 5) -> None:
    with grpc.insecure_channel(address) as channel:
        grpc.channel_ready_future(channel).result(timeout=timeout)
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        # An explicitly present, empty environment is rejected before either
        # executor is selected, before payload unpickling and before any SSH.
        request = execution_pb2.ExecuteRequest(
            environment=execution_pb2.EnvironmentFiles(),
        )
        try:
            stub.Execute(request, timeout=timeout)
        except grpc.RpcError as error:
            call = cast(grpc.Call, error)
            if (
                call.code() == grpc.StatusCode.INVALID_ARGUMENT
                and call.details() == "Environment requires pyproject.toml and uv.lock"
            ):
                return
            raise
        raise RuntimeError("Expected Nest to reject the empty environment")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="Controller Tailnet IP or full DNS name, with :50051")
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    try:
        verify(args.address, args.timeout)
    except (grpc.RpcError, grpc.FutureTimeoutError, RuntimeError) as error:
        parser.exit(1, f"FAIL: Nest RPC probe: {type(error).__name__}: {error}\n")
    print("PASS: Nest RPC handler reached; no code, SSH, or Slurm job executed")


if __name__ == "__main__":
    main()
