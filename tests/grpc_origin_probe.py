"""One credential-free `ListEligibleProjects` call straight to a Controller (h2c).

Used only by `tests/smoke_public_grpc_networks.sh`, from containers on the networks
around the Controller. It prints the status code, the `nest-error-kind` trailer
and whether the `nest login` guidance came back, as one JSON line, and exits 0
whatever the answer is: the smoke test compares it.

    python grpc_origin_probe.py HOST:PORT [--forwarded-for ADDRESS]
"""

import argparse
import json
from typing import cast

import grpc

from nest.proto import execution_pb2, execution_pb2_grpc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("--forwarded-for", help="send this x-forwarded-for (a spoof attempt)")
    args = parser.parse_args()
    metadata = [("x-forwarded-for", args.forwarded_for)] if args.forwarded_for else None
    with grpc.insecure_channel(args.address) as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        try:
            stub.ListEligibleProjects(
                execution_pb2.ListEligibleProjectsRequest(), timeout=10, metadata=metadata
            )
            result = {"code": "OK"}
        except grpc.RpcError as error:
            call = cast(grpc.Call, error)
            trailers = dict(call.trailing_metadata() or ())
            details = call.details() or ""
            result = {
                "code": call.code().name,
                "kind": trailers.get("nest-error-kind", ""),
                "login_guidance": "nest login" in details,
            }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
