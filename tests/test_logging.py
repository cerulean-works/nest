"""Logging regressions across real local RPCs, worker processes and Slurm stubs."""

import hashlib
import logging
import os
import re
import subprocess
import sys
from concurrent import futures
from threading import Barrier
from types import SimpleNamespace
from typing import cast

import cloudpickle
import grpc
import pytest

from nest._logging import (
    configure_logging,
    current_request_id,
    get_logger,
    request_context,
)
from nest.backends.slurm import SlurmBackend, SlurmConfig
from nest.client import execute_remote
from nest.controller import ExecutionService
from nest.errors import ExecutionTimeoutError, RemoteExecutionError, SlurmJobError
from nest.proto import execution_pb2, execution_pb2_grpc
from nest.remote import Resources


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setenv("NEST_EXECUTOR", "local")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(ExecutionService(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    monkeypatch.setenv("NEST_CONTROLLER_ADDRESS", f"127.0.0.1:{port}")
    yield f"127.0.0.1:{port}"
    server.stop(0).wait()


@pytest.fixture
def backend():
    return SlurmBackend(
        SlurmConfig("cluster", "account", work_root="/nest/jobs", worker_workdir="/nest")
    )


def test_import_does_not_configure_application_logging():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
root = logging.getLogger()
handler = logging.StreamHandler()
root.addHandler(handler)
root.setLevel(logging.ERROR)
import nest
assert root.handlers == [handler]
assert root.level == logging.ERROR
nest_logger = logging.getLogger('nest')
assert nest_logger.level == logging.NOTSET
assert nest_logger.propagate
assert all(isinstance(h, logging.NullHandler) for h in nest_logger.handlers)
""",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("level, expected", [("debug", logging.DEBUG), ("invalid", logging.INFO)])
def test_console_configuration_is_scoped_and_idempotent(monkeypatch, capsys, level, expected):
    logger = logging.getLogger("nest")
    original = logger.handlers[:], logger.level, logger.propagate
    root = logging.getLogger()
    root_state = root.handlers[:], root.level
    try:
        monkeypatch.setenv("NEST_LOG_LEVEL", level)
        configure_logging()
        configure_logging()
        assert logger.level == expected
        assert len([h for h in logger.handlers if getattr(h, "_nest_console", False)]) == 1
        with request_context("a" * 32):
            get_logger("nest.test").info("test event")
        # Ordinary loggers in the namespace also format safely without an adapter.
        logging.getLogger("nest.other").info("ordinary event")
        output = capsys.readouterr().err
        assert output.count("test event") == 1
        assert "request_id=" + "a" * 32 in output
        assert "request_id=- ordinary event" in output
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z INFO", output)
        assert (root.handlers, root.level) == root_state
        if level == "invalid":
            assert "Invalid NEST_LOG_LEVEL" in output
    finally:
        for handler in logger.handlers:
            if handler not in original[0]:
                handler.close()
        logger.handlers, logger.level, logger.propagate = original


def test_concurrent_request_contexts_are_isolated_and_reset():
    barrier = Barrier(2)

    def run(value):
        with request_context(value):
            barrier.wait(timeout=5)
            assert current_request_id() == value
            with request_context("bad\ninjected") as nested:
                assert re.fullmatch(r"[0-9a-f]{32}", nested)
            assert current_request_id() == value
        assert current_request_id() == "-"

    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ["a" * 32, "b" * 32]))
    assert current_request_id() == "-"


def test_local_rpc_correlates_client_controller_and_worker(controller, caplog, capfd):
    caplog.set_level(logging.DEBUG, logger="nest")
    secret = "PRIVATE_ARGUMENT_AND_RESULT_6fc8"
    result = execute_remote(lambda value: value, Resources(), None, secret)
    assert result == secret
    records = [r for r in caplog.records if r.name.startswith("nest.")]
    ids = {r.request_id for r in records}
    assert len(ids) == 1
    request_id = ids.pop()
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert any(r.name == "nest.client" for r in records)
    assert any(r.name == "nest.controller" for r in records)
    assert any(r.name == "nest.executor" for r in records)
    worker_output = capfd.readouterr().err
    assert "request_id=" + request_id in worker_output
    assert "Worker execution completed elapsed_ms=" in worker_output
    assert secret not in caplog.text + worker_output
    assert current_request_id() == "-"


def test_remote_user_error_preserves_error_and_logs_traceback(controller, caplog, capfd):
    caplog.set_level(logging.INFO, logger="nest")

    def fail():
        raise ValueError("worker failure marker")

    with pytest.raises(RemoteExecutionError, match="worker failure marker") as error:
        execute_remote(fail, Resources(), None)
    assert error.value.remote_type == "builtins.ValueError"
    assert "raise ValueError" in error.value.remote_traceback
    assert "Remote execution failed elapsed_ms=" in caplog.text
    output = capfd.readouterr().err
    assert "User function failed" in output
    assert "Traceback (most recent call last)" in output
    request_id = next(r.request_id for r in caplog.records if r.name == "nest.client")
    assert "request_id=" + request_id in output


@pytest.mark.parametrize("metadata", [(), (("x-nest-request-id", "invalid-id"),)])
def test_controller_generates_id_for_legacy_or_invalid_metadata(controller, caplog, metadata):
    caplog.set_level(logging.INFO, logger="nest")
    with grpc.insecure_channel(controller) as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        with pytest.raises(grpc.RpcError) as error:
            stub.Execute(
                execution_pb2.ExecuteRequest(environment=execution_pb2.EnvironmentFiles()),
                metadata=metadata,
            )
    assert cast(grpc.Call, error.value).code() == grpc.StatusCode.INVALID_ARGUMENT
    records = [r for r in caplog.records if r.name == "nest.controller"]
    assert len({r.request_id for r in records}) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", records[0].request_id)
    assert "status=INVALID_ARGUMENT" in caplog.text
    assert not any(r.exc_info for r in records)


@pytest.mark.parametrize("failed", [False, True])
def test_isolated_user_interpreter_uses_bootstrap_and_request_id(tmp_path, failed):
    def call():
        if failed:
            raise RuntimeError("isolated failure marker")
        return 42

    input_path, output_path = tmp_path / "input.pkl", tmp_path / "output.pkl"
    input_path.write_bytes(cloudpickle.dumps((call, (), {})))
    env = {**os.environ, "NEST_REQUEST_ID": "c" * 32, "NEST_LOG_LEVEL": "DEBUG"}
    result = subprocess.run(
        [sys.executable, "-m", "nest.worker", str(input_path), str(output_path), sys.executable],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    value = cloudpickle.loads(output_path.read_bytes())
    if failed:
        assert value["__nest_remote_error_v1__"] is True
        assert value["type"] == "builtins.RuntimeError"
        assert "User function failed" in result.stderr
    else:
        assert value == 42
    assert "nest.user_runner" in result.stderr
    assert "request_id=" + "c" * 32 in result.stderr
    assert "User environment execution completed" in result.stderr
    assert all(
        "request_id=" + "c" * 32 in line for line in result.stderr.splitlines() if "pid=" in line
    )
    assert result.stdout == ""


@pytest.mark.parametrize("wait", ["wait_result", "wait_environment"])
def test_slurm_polls_log_only_state_transitions(backend, monkeypatch, caplog, wait):
    caplog.set_level(logging.INFO, logger="nest")
    states = iter(["PENDING", "PENDING", "RUNNING", "RUNNING", "COMPLETED"])
    monkeypatch.setattr(backend, "status", lambda job_id: next(states))
    monkeypatch.setattr("nest.backends.slurm.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "nest.backends.slurm.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"private-result"),
    )
    with request_context("d" * 32):
        if wait == "wait_result":
            assert backend.wait_result(123, "/nest/jobs/execution") == b"private-result"
        else:
            backend.wait_environment(123)
    events = [r for r in caplog.records if "Slurm state changed" in r.message]
    assert len(events) == 3
    assert all(r.request_id == "d" * 32 and "job_id=123" in r.message for r in events)
    assert "private-result" not in caplog.text


@pytest.mark.parametrize(
    "state, timeout, exception",
    [("FAILED", 600, SlurmJobError), ("RUNNING", 0, ExecutionTimeoutError)],
)
def test_slurm_failure_and_timeout_preserve_exception(
    backend, monkeypatch, caplog, state, timeout, exception
):
    caplog.set_level(logging.INFO, logger="nest")
    monkeypatch.setattr(backend, "status", lambda _: state)
    with pytest.raises(exception):
        backend.wait_result(456, "/nest/jobs/execution", timeout=timeout)
    assert "job_id=456" in caplog.text
    assert "Slurm wait_result failed elapsed_ms=" in caplog.text
    assert any(r.exc_info and r.exc_info[0] is exception for r in caplog.records)


def test_slurm_forwards_context_without_payload_in_logs(backend, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="nest")
    captured = {}
    monkeypatch.setenv("NEST_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(backend, "stage_payload", lambda payload: "/nest/jobs/" + "e" * 32)

    def submit(**kwargs):
        captured.update(kwargs)
        return 789

    monkeypatch.setattr(backend, "submit", submit)
    with request_context("f" * 32):
        assert backend.submit_payload(b"private-payload", environment_id="a" * 64)[0] == 789
    assert captured["script_args"] == ("e" * 32, "a" * 64, "/nest", "f" * 32, "DEBUG")
    assert "job_id=789" in caplog.text
    assert "private-payload" not in caplog.text


@pytest.mark.parametrize("cache_hit", [False, True])
def test_environment_cache_logging_omits_dependency_contents(
    backend, monkeypatch, caplog, cache_hit
):
    caplog.set_level(logging.DEBUG, logger="nest")
    pyproject, lock = b"PRIVATE_DEPENDENCIES", b"PRIVATE_LOCK"
    hashes = "\n".join(hashlib.sha256(data).hexdigest() + " file" for data in (pyproject, lock))
    checks = iter([hashes] if cache_hit else ["", hashes])
    monkeypatch.setattr(
        backend, "_run_remote", lambda command: next(checks) if "sha256sum" in command else ""
    )
    uploads = []

    def upload(*args, **kwargs):
        uploads.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr("nest.backends.slurm.subprocess.run", upload)
    monkeypatch.setattr(backend, "environment_ready", lambda _: True)
    monkeypatch.setattr(
        backend,
        "build_environment",
        lambda *a, **k: pytest.fail("ready runtime must not submit a build"),
    )
    env_dir = backend.prepare_environment_bytes(pyproject_toml=pyproject, uv_lock=lock)
    assert env_dir.startswith("/nest/environments/")
    assert uploads == ([] if cache_hit else [pyproject, lock])
    assert "Runtime cache hit environment_id=" in caplog.text
    assert (
        "Environment files cache hit" if cache_hit else "Environment files cache miss"
    ) in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_controller_logs_original_slurm_error_before_rpc_abort(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="nest")
    monkeypatch.setenv("NEST_EXECUTOR", "slurm")

    def fail(*args, **kwargs):
        raise RuntimeError("scheduler failure marker")

    service = ExecutionService(SimpleNamespace(submit_payload=fail))
    # Use a separate server to retain the actual gRPC abort behavior.
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        monkeypatch.setenv("NEST_CONTROLLER_ADDRESS", f"127.0.0.1:{port}")
        with pytest.raises(grpc.RpcError) as error:
            execute_remote(lambda: None, Resources(), None)
    finally:
        server.stop(0).wait()
    assert cast(grpc.Call, error.value).code() == grpc.StatusCode.INTERNAL
    assert "scheduler failure marker" in cast(grpc.Call, error.value).details()
    assert "status=INTERNAL" in caplog.text
    original = next(r for r in caplog.records if r.message == "Slurm execution failed")
    assert original.exc_info[0] is RuntimeError
    assert len({r.request_id for r in caplog.records if r.name.startswith("nest.")}) == 1


def test_unexpected_controller_failure_logs_original_traceback(controller, caplog):
    caplog.set_level(logging.INFO, logger="nest")
    with grpc.insecure_channel(controller) as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        with pytest.raises(grpc.RpcError) as error:
            stub.Execute(execution_pb2.ExecuteRequest(payload=b"invalid pickle"))
    assert cast(grpc.Call, error.value).code() == grpc.StatusCode.UNKNOWN
    failure = next(r for r in caplog.records if "RPC failed method=Execute" in r.message)
    assert failure.exc_info
    assert "cloudpickle.loads" in caplog.text
