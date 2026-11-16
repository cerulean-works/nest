import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import contextmanager

import cloudpickle
import grpc

from nest.controller import (
    MAX_STATUS_DETAILS_BYTES,
    _abort_infrastructure,
    _encoded_size,
    _status_details,
)
from nest.errors import ERROR_KIND_METADATA, JOB_ID_METADATA, RemoteCommandError
from nest.proto import execution_pb2, execution_pb2_grpc


def controller_environment(**settings):
    env = os.environ.copy()
    for name in ("NEST_SLURM_HOST", "NEST_SLURM_ACCOUNT", "NEST_SLURM_WORKDIR"):
        env.pop(name, None)
    for name in [n for n in env if n.startswith(("NEST_DATABASE_", "NEST_EMAIL_", "NEST_SMTP_"))]:
        env.pop(name)
    env.update(NEST_EXECUTOR="local", PYTHONUNBUFFERED="1")
    env.update(settings)
    return env


@contextmanager
def running_controller(**settings):
    with controller_at_free_port(**settings) as (process, stub, _):
        yield process, stub


@contextmanager
def controller_at_free_port(**settings):
    """Starts `nest.controller` and yields its process, stub, and address."""
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
                yield process, execution_pb2_grpc.ExecutionServiceStub(channel), address
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


class StatusDetailsTest(unittest.TestCase):
    def test_short_message_is_unchanged(self):
        message = "Remote command failed:\nsbatch: error: Invalid account"
        self.assertEqual(_status_details(message), message)

    def test_long_message_keeps_head_and_tail_within_limit(self):
        for filler in ("x", "é", "日本"):
            with self.subTest(filler=filler):
                message = "Remote command failed:\n" + filler * 30_000 + "\nthe actual cause"
                details = _status_details(message)
                self.assertLessEqual(_encoded_size(details), MAX_STATUS_DETAILS_BYTES)
                self.assertTrue(details.startswith("Remote command failed:\n"))
                self.assertTrue(details.endswith("\nthe actual cause"))
                self.assertRegex(
                    details, r"\[\.\.\. \d+ characters omitted; see the Controller log"
                )

    def test_omitted_count_matches_dropped_characters(self):
        message = "a" * 5_000 + "é" * 5_000
        details = _status_details(message)
        omitted = int(details.split("[... ", 1)[1].split(" characters", 1)[0])
        marker = f"\n[... {omitted} characters omitted; see the Controller log ...]\n"
        head, tail = details.split(marker)
        self.assertEqual(message, head + message[len(head) : len(head) + omitted] + tail)

    def test_abort_keeps_status_kind_and_job_id_for_long_messages(self):
        context = unittest.mock.Mock()
        error = RemoteCommandError("Remote command failed:\n" + "é" * 30_000, job_id=1234)
        _abort_infrastructure(context, error, grpc.StatusCode.FAILED_PRECONDITION)
        context.set_trailing_metadata.assert_called_once_with(
            ((ERROR_KIND_METADATA, "remote_command_error"), (JOB_ID_METADATA, "1234"))
        )
        [(status, details)] = [call.args for call in context.abort.call_args_list]
        self.assertEqual(status, grpc.StatusCode.FAILED_PRECONDITION)
        self.assertLessEqual(_encoded_size(details), MAX_STATUS_DETAILS_BYTES)

    def test_encoded_size_counts_percent_encoded_bytes(self):
        self.assertEqual(_encoded_size("ab"), 2)
        self.assertEqual(_encoded_size("%\n"), 6)
        self.assertEqual(_encoded_size("é"), 6)


if __name__ == "__main__":
    unittest.main()
