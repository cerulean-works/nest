"""The live deployment probe must never reach execution or the Slurm backend."""

import importlib.util
import unittest
from concurrent import futures
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import grpc

from nest.controller import ExecutionService
from nest.proto import execution_pb2_grpc

spec = importlib.util.spec_from_file_location(
    "verify_grpc",
    Path(__file__).resolve().parents[1] / "scripts/verify-grpc.py",
)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class GrpcProbeTests(unittest.TestCase):
    def test_probe_never_executes_or_contacts_slurm(self):
        for mode in ("local", "slurm"):
            with self.subTest(mode=mode), patch.dict("os.environ", NEST_EXECUTOR=mode):
                backend = Mock()
                server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
                execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
                    ExecutionService(backend),
                    server,
                )
                port = server.add_insecure_port("127.0.0.1:0")
                server.start()
                try:
                    with (
                        patch("nest.controller.execute") as execute,
                        patch(
                            "nest.controller.cloudpickle.loads",
                        ) as unpickle,
                    ):
                        probe.verify(f"127.0.0.1:{port}")
                        execute.assert_not_called()
                        unpickle.assert_not_called()
                    self.assertEqual(backend.mock_calls, [])
                finally:
                    server.stop(0).wait()

    def test_probe_rejects_a_non_nest_grpc_server(self):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with self.assertRaises(grpc.RpcError) as caught:
                probe.verify(f"127.0.0.1:{port}")
            self.assertEqual(
                cast(grpc.Call, caught.exception).code(), grpc.StatusCode.UNIMPLEMENTED
            )
        finally:
            server.stop(0).wait()
