import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager

import cloudpickle
import grpc

from nest.proto import execution_pb2, execution_pb2_grpc


def controller_environment(**settings):
    env = os.environ.copy()
    for name in ("NEST_SLURM_HOST", "NEST_SLURM_ACCOUNT", "NEST_SLURM_WORKDIR"):
        env.pop(name, None)
    env.update(NEST_EXECUTOR="local", PYTHONUNBUFFERED="1")
    env.update(settings)
    return env


@contextmanager
def running_controller(**settings):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    address = f"127.0.0.1:{port}"
    env = controller_environment(NEST_CONTROLLER_BIND=address, **settings)
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "nest.controller"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with grpc.insecure_channel(address) as channel:
                try:
                    grpc.channel_ready_future(channel).result(timeout=5)
                except grpc.FutureTimeoutError:
                    log.seek(0)
                    raise AssertionError("Controller did not start:\n" + log.read())
                yield process, execution_pb2_grpc.ExecutionServiceStub(channel)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class ControllerTests(unittest.TestCase):
    def test_local_rpc_executes_a_function(self):
        with running_controller() as (_, stub):
            payload = cloudpickle.dumps((lambda value: value * 2, (21,), {}))
            response = stub.Execute(execution_pb2.ExecuteRequest(payload=payload), timeout=5)
            self.assertEqual(cloudpickle.loads(response.result), 42)

    def test_remote_exception_record_survives_rpc(self):
        def fail():
            raise ValueError("worker failure")

        with running_controller() as (_, stub):
            response = stub.Execute(
                execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((fail, (), {}))),
                timeout=5,
            )
            error = cloudpickle.loads(response.result)
            self.assertTrue(error["__nest_remote_error_v1__"])
            self.assertEqual(error["type"], "builtins.ValueError")
            self.assertEqual(error["message"], "worker failure")

    def test_sigterm_and_sigint_exit_cleanly(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum), running_controller() as (process, _):
                process.send_signal(signum)
                self.assertEqual(process.wait(timeout=5), 0)

    def test_sigterm_allows_an_active_rpc_to_finish(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = os.path.join(temporary, "started")

            def slow_call():
                import time
                from pathlib import Path

                Path(marker).touch()
                time.sleep(1)
                return 42

            with running_controller() as (process, stub):
                future = stub.Execute.future(
                    execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((slow_call, (), {}))),
                    timeout=8,
                )
                deadline = time.monotonic() + 5
                while not os.path.exists(marker) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(marker), "Worker never started")
                process.terminate()
                self.assertEqual(cloudpickle.loads(future.result().result), 42)
                self.assertEqual(process.wait(timeout=5), 0)

    def test_slurm_requires_each_deployment_setting(self):
        settings = {
            "NEST_SLURM_HOST": "hpc",
            "NEST_SLURM_ACCOUNT": "test-account",
            "NEST_SLURM_WORKDIR": "/home/test/nest",
        }
        for missing in settings:
            with self.subTest(missing=missing):
                env = controller_environment(NEST_EXECUTOR="slurm", **settings)
                env[missing] = ""
                result = subprocess.run(
                    [sys.executable, "-m", "nest.controller"],
                    check=False,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stderr)

    def test_slurm_rejects_a_relative_workdir(self):
        env = controller_environment(
            NEST_EXECUTOR="slurm",
            NEST_SLURM_HOST="hpc",
            NEST_SLURM_ACCOUNT="test-account",
            NEST_SLURM_WORKDIR="relative/path",
        )
        result = subprocess.run(
            [sys.executable, "-m", "nest.controller"],
            check=False,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be an absolute HPC path", result.stderr)

    def test_complete_slurm_config_starts_without_contacting_hpc(self):
        with running_controller(
            NEST_EXECUTOR="slurm",
            NEST_SLURM_HOST="hpc",
            NEST_SLURM_ACCOUNT="test-account",
            NEST_SLURM_WORKDIR="/home/test/nest",
        ) as (process, _):
            self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
