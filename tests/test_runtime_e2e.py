"""End-to-end regressions for the existing execution runtime.

These tests drive the public SDK (`nest.remote`) through a real Controller
process over gRPC. Slurm tests use `FakeHpc`, so the Controller's SSH commands,
`sbatch` submission, `scripts/worker.sbatch`, the Apptainer invocation, the uv
environment build, and the worker all run unchanged without a cluster.
"""

import re
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import grpc
import pytest
from fake_hpc import FakeHpc
from test_controller import controller_at_free_port

import nest
from nest.errors import (
    ERROR_KIND_METADATA,
    ExecutionTimeoutError,
    NestTimeoutError,
    QueueTimeoutError,
    RemoteExecutionError,
    RpcTimeoutError,
    SlurmJobError,
    SSHError,
    SSHTimeoutError,
    SubmissionUnknownError,
)
from nest.proto import execution_pb2

WORKER_SCRIPT = "project/scripts/worker.sbatch"
BUILD_SCRIPT = "project/scripts/environment-smoke.sbatch"


@pytest.fixture
def hpc(tmp_path):
    return FakeHpc(tmp_path)


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0"\n')
    (directory / "uv.lock").write_text("version = 1\n")
    return directory


@pytest.fixture
def local_controller(monkeypatch):
    with controller_at_free_port() as (_, stub, address):
        monkeypatch.setenv("NEST_CONTROLLER_ADDRESS", address)
        yield stub


@pytest.fixture
def slurm_controller(monkeypatch, hpc):
    with controller_at_free_port(**hpc.controller_settings()) as (_, stub, address):
        monkeypatch.setenv("NEST_CONTROLLER_ADDRESS", address)
        yield stub


@pytest.fixture
def start_slurm_controller(monkeypatch, hpc):
    """Starts a FakeHpc Controller with extra settings, such as short timeouts."""
    with ExitStack() as stack:

        def start(**settings):
            _, stub, address = stack.enter_context(
                controller_at_free_port(**{**hpc.controller_settings(), **settings})
            )
            monkeypatch.setenv("NEST_CONTROLLER_ADDRESS", address)
            return stub

        yield start


FAST_POLLING = {"NEST_POLL_INTERVAL": "0.1"}


def _metadata(error: grpc.Call) -> dict:
    return dict(error.trailing_metadata() or ())


def _worker_jobs(hpc):
    return [call for call in hpc.calls("sbatch") if call["script"] == WORKER_SCRIPT]


def _build_jobs(hpc):
    return [call for call in hpc.calls("sbatch") if call["script"] == BUILD_SCRIPT]


def _assert_meaningful_traceback(error: RemoteExecutionError, *frames: str):
    assert error.remote_traceback.startswith("Traceback (most recent call last)")
    for frame in frames:
        assert f"in {frame}" in error.remote_traceback
    assert error.remote_traceback.rstrip().endswith(
        f"{error.remote_type.rsplit('.', 1)[-1]}: {error.remote_message}"
    )
    # The local exception message carries the remote traceback for display.
    assert "Remote traceback:" in str(error)
    assert error.remote_traceback in str(error)


# Local execution


def test_local_remote_call_returns_result(local_controller):
    @nest.remote(cpu=2)
    def add(a, b, *, scale=1):
        return (a + b) * scale

    assert add.remote(2, 3, scale=10) == 50
    assert add.local(2, 3) == add(2, 3) == 5


def test_local_remote_call_runs_in_a_separate_process(local_controller):
    import os

    @nest.remote()
    def pid():
        import os

        return os.getpid()

    assert pid.remote() != os.getpid()


def test_local_remote_exception_has_meaningful_traceback(local_controller):
    @nest.remote()
    def outer():
        return inner(0)

    def inner(value):
        return 1 / value

    with pytest.raises(RemoteExecutionError) as caught:
        outer.remote()
    assert caught.value.remote_type == "builtins.ZeroDivisionError"
    assert caught.value.remote_message == "division by zero"
    _assert_meaningful_traceback(caught.value, "outer", "inner")


def test_local_executor_accepts_and_ignores_project_environment(local_controller, project):
    @nest.remote(project=project)
    def answer():
        return 42

    assert answer.remote() == 42


# Slurm execution


def test_slurm_job_executes_and_returns_result(slurm_controller, hpc):
    @nest.remote(gpu=1)
    def compute(values):
        import socket

        return {"total": sum(values), "host": socket.gethostname()}

    result = compute.remote([1, 2, 3])
    assert result["total"] == 6

    [job] = _worker_jobs(hpc)
    execution_id, environment_id, workdir, request_id, log_level = job["args"]
    job_dir = hpc.base / "jobs" / execution_id
    assert (job_dir / "input.pkl").is_file()
    assert (job_dir / "output.pkl").stat().st_size > 0
    assert environment_id == ""
    assert workdir == str(hpc.base)
    assert log_level == "INFO"
    assert "Worker finished successfully" in hpc.job_output(job["job_id"])
    assert f"request_id={request_id}" in hpc.job_output(job["job_id"])
    assert _build_jobs(hpc) == []


def test_slurm_remote_exception_has_meaningful_traceback(slurm_controller, hpc):
    @nest.remote()
    def train():
        return load_batch("missing.parquet")

    def load_batch(path):
        raise FileNotFoundError(f"No such dataset: {path}")

    with pytest.raises(RemoteExecutionError) as caught:
        train.remote()
    assert caught.value.remote_type == "builtins.FileNotFoundError"
    assert caught.value.remote_message == "No such dataset: missing.parquet"
    _assert_meaningful_traceback(caught.value, "train", "load_batch")
    # A user error is a successful job that returns an error record.
    assert "Worker finished successfully" in hpc.job_output(_worker_jobs(hpc)[0]["job_id"])


def test_slurm_job_failure_surfaces_as_internal_rpc_error(slurm_controller, hpc):
    @nest.remote()
    def crash():
        import os

        os._exit(3)

    with pytest.raises(grpc.RpcError) as caught:
        crash.remote()
    error = caught.value
    job_id = _worker_jobs(hpc)[0]["job_id"]
    # Still a grpc.RpcError with the original status and details.
    assert isinstance(error, SlurmJobError)
    assert error.code() == grpc.StatusCode.INTERNAL
    assert error.details() == f"Slurm job {job_id} failed: FAILED"
    assert error.job_id == job_id


def test_slurm_unreachable_host_surfaces_ssh_error(start_slurm_controller, hpc):
    start_slurm_controller(NEST_SLURM_HOST="unknown-hpc")

    @nest.remote()
    def noop():
        return None

    with pytest.raises(SSHError) as caught:
        noop.remote()
    error = caught.value
    assert not isinstance(error, TimeoutError)
    assert error.code() == grpc.StatusCode.UNAVAILABLE
    assert "Could not resolve hostname unknown-hpc" in error.details()
    assert _worker_jobs(hpc) == []


# uv dependency environments


def test_slurm_environment_runs_in_user_venv_with_traceback(slurm_controller, hpc, project):
    @nest.remote(project=project)
    def where():
        import os
        import sys

        return sys.prefix, os.environ.get("VIRTUAL_ENV")

    @nest.remote(project=project)
    def fail():
        class DatasetError(Exception):
            pass

        raise DatasetError("bad shard 7")

    prefix, virtual_env = where.remote()
    [build] = _build_jobs(hpc)
    environment_id = build["args"][0]
    assert re.fullmatch(r"[0-9a-f]{64}", environment_id)
    runtime = hpc.base / "environments" / environment_id / "runtimes"
    assert Path(prefix).parent.parent == runtime
    assert virtual_env == prefix
    assert _worker_jobs(hpc)[0]["args"][1] == environment_id

    with pytest.raises(RemoteExecutionError) as caught:
        fail.remote()
    assert caught.value.remote_type.endswith("DatasetError")
    assert caught.value.remote_message == "bad shard 7"
    _assert_meaningful_traceback(caught.value, "fail")


def test_environment_cache_miss_then_hit(slurm_controller, hpc, project):
    @nest.remote(project=project)
    def ping():
        return "pong"

    def uploads():
        return [c for c in hpc.calls("ssh") if ".tmp-" in c["argv"][-1]]

    # First call: files and runtime are both missing.
    assert ping.remote() == "pong"
    assert len(_build_jobs(hpc)) == 1
    assert len(uploads()) == 2
    syncs = [c for c in hpc.calls("uv") if c["argv"][0] == "sync"]
    assert len(syncs) == 1

    # Second call: both caches hit, so nothing is uploaded or rebuilt.
    assert ping.remote() == "pong"
    assert len(_build_jobs(hpc)) == 1
    assert len(uploads()) == 2
    assert len([c for c in hpc.calls("uv") if c["argv"][0] == "sync"]) == 1

    [first, second] = _worker_jobs(hpc)
    assert first["args"][1] == second["args"][1] == _build_jobs(hpc)[0]["args"][0]


def test_environment_changes_when_lock_changes(slurm_controller, hpc, project):
    @nest.remote(project=project)
    def ping():
        return "pong"

    assert ping.remote() == "pong"
    (project / "uv.lock").write_text("version = 1\n# updated\n")
    assert ping.remote() == "pong"

    first, second = (job["args"][0] for job in _build_jobs(hpc))
    assert first != second
    assert [job["args"][1] for job in _worker_jobs(hpc)] == [first, second]


def test_missing_runtime_is_rebuilt_from_cached_files(slurm_controller, hpc, project):
    @nest.remote(project=project)
    def ping():
        return "pong"

    assert ping.remote() == "pong"
    [ready] = (hpc.base / "environments").glob("*/runtimes/*/.ready")
    ready.unlink()
    uploads_before = len([c for c in hpc.calls("ssh") if ".tmp-" in c["argv"][-1]])

    assert ping.remote() == "pong"
    assert len(_build_jobs(hpc)) == 2
    assert len([c for c in hpc.calls("ssh") if ".tmp-" in c["argv"][-1]]) == uploads_before
    assert "Environment cache miss" in hpc.job_output(_build_jobs(hpc)[1]["job_id"])


def test_failed_environment_build_aborts_without_running_worker(slurm_controller, hpc, project):
    (hpc.bin / "uv").write_text("#!/bin/sh\necho 'resolution failed' >&2\nexit 1\n")

    @nest.remote(project=project)
    def ping():
        return "pong"

    with pytest.raises(grpc.RpcError) as caught:
        ping.remote()
    error = cast(grpc.Call, caught.value)
    assert error.code() == grpc.StatusCode.INTERNAL
    assert re.fullmatch(r"Environment build \d+ failed: FAILED", error.details())
    assert _worker_jobs(hpc) == []


# Controller-owned SSH backward compatibility


def test_controller_ssh_and_sbatch_commands_are_stable(slurm_controller, hpc, project):
    @nest.remote(gpu=2, project=project)
    def ping():
        return "pong"

    @nest.remote(cpu=0)
    def ping_cpu():
        return "pong"

    assert ping.remote() == "pong"
    assert ping_cpu.remote() == "pong"

    # Every cluster operation is one noninteractive SSH command to the configured host.
    ssh_calls = hpc.calls("ssh")
    assert ssh_calls
    for call in ssh_calls:
        assert call["argv"][:3] == ["-o", "BatchMode=yes", hpc.host]
        assert len(call["argv"]) == 4

    (build,) = _build_jobs(hpc)
    gpu_job, cpu_job = _worker_jobs(hpc)
    common = ["--parsable", "--account=test-account", "--partition=dev"]
    resources = ["--gpus-per-node=2", "--cpus-per-task=1"]
    assert build["flags"] == [*common, *resources, "--time=00:05:00"]
    assert gpu_job["flags"] == [*common, *resources, "--time=00:05:00"]
    # cpu=0 and gpu=0 leave both counts to the deployed worker script.
    assert cpu_job["flags"] == [*common, "--time=00:05:00"]
    for job in (build, gpu_job, cpu_job):
        assert job["cwd"] == str(hpc.base)

    # Positional contract with scripts/environment-smoke.sbatch and worker.sbatch.
    assert build["args"] == [build["args"][0], str(hpc.base)]
    for job, environment_id in ((gpu_job, build["args"][0]), (cpu_job, "")):
        execution_id, env, workdir, request_id, log_level = job["args"]
        assert re.fullmatch(r"[0-9a-f]{32}", execution_id)
        assert re.fullmatch(r"[0-9a-f]{32}", request_id)
        assert (env, workdir, log_level) == (environment_id, str(hpc.base), "INFO")

    # The worker runs in the deployed image with a clean environment.
    for call in hpc.calls("apptainer"):
        assert call["image"] == str(hpc.image)
        assert call["clean"] is True


def test_batch_job_rpcs_submit_query_and_cancel(slurm_controller, hpc, tmp_path):
    workdir = tmp_path / "batch"
    workdir.mkdir()
    (workdir / "hello.sbatch").write_text("#!/bin/bash\necho hello > hello.out\n")
    (workdir / "pending.sbatch").write_text("#!/bin/bash\nexit 0\n")
    stub = slurm_controller

    submitted = stub.SubmitJob(
        execution_pb2.SubmitJobRequest(
            workdir=str(workdir),
            script="hello.sbatch",
            gpu=1,
            account="other-account",
            partition="gpu",
            time="00:01:00",
        ),
        timeout=30,
    )
    assert (workdir / "hello.out").read_text() == "hello\n"
    assert hpc.calls("sbatch")[-1]["flags"] == [
        "--parsable",
        "--account=other-account",
        "--partition=gpu",
        "--gpus-per-node=1",
        "--time=00:01:00",
    ]
    status = stub.GetJobStatus(execution_pb2.JobStatusRequest(job_id=submitted.job_id), timeout=30)
    assert status.state == "COMPLETED"

    pending = stub.SubmitJob(
        execution_pb2.SubmitJobRequest(workdir=str(workdir), script="pending.sbatch"), timeout=30
    )
    # Request defaults fall back to the Controller's Slurm configuration.
    assert hpc.calls("sbatch")[-1]["flags"] == [
        "--parsable",
        "--account=test-account",
        "--partition=dev",
        "--time=00:05:00",
    ]
    assert (
        stub.GetJobStatus(execution_pb2.JobStatusRequest(job_id=pending.job_id), timeout=30).state
        == "PENDING"
    )
    stub.CancelJob(execution_pb2.CancelJobRequest(job_id=pending.job_id), timeout=30)
    assert hpc.calls("scancel")[-1]["job_id"] == str(pending.job_id)
    assert stub.GetJobStatus(
        execution_pb2.JobStatusRequest(job_id=pending.job_id), timeout=30
    ).state.startswith("CANCELLED")

    with pytest.raises(grpc.RpcError) as caught:
        stub.CancelJob(execution_pb2.CancelJobRequest(job_id=999999), timeout=30)
    assert cast(grpc.Call, caught.value).code() == grpc.StatusCode.FAILED_PRECONDITION


# CPU resource forwarding


def test_cpu_request_is_forwarded_to_worker_and_build(slurm_controller, hpc, project):
    @nest.remote(cpu=8, project=project)
    def threads():
        return "ok"

    @nest.remote(cpu=3)
    def plain():
        return "ok"

    assert threads.remote() == "ok"
    assert plain.remote() == "ok"
    (build,) = _build_jobs(hpc)
    with_env, without_env = _worker_jobs(hpc)
    assert "--cpus-per-task=8" in build["flags"]
    assert "--cpus-per-task=8" in with_env["flags"]
    assert "--cpus-per-task=3" in without_env["flags"]
    # GPU defaults to zero, so no GPU flag accompanies a CPU-only request.
    assert not any(flag.startswith("--gpus") for flag in without_env["flags"])


def test_negative_resources_are_rejected_before_submission(slurm_controller, hpc):
    with pytest.raises(ValueError, match="cpu cannot be negative"):
        nest.remote(cpu=-1)

    with pytest.raises(grpc.RpcError) as caught:
        slurm_controller.Execute(
            execution_pb2.ExecuteRequest(payload=b"unused", cpu=-2), timeout=30
        )
    assert cast(grpc.Call, caught.value).code() == grpc.StatusCode.INVALID_ARGUMENT
    assert hpc.calls("ssh") == []


# Timeout classification


def test_ssh_timeout_while_staging_is_an_ssh_timeout(start_slurm_controller, hpc):
    start_slurm_controller(NEST_SSH_TIMEOUT="1")
    hpc.hang_ssh("input.pkl")

    @nest.remote()
    def noop():
        return None

    with pytest.raises(SSHTimeoutError) as caught:
        noop.remote()
    error = caught.value
    assert isinstance(error, TimeoutError)
    assert error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert "timed out after 1 seconds" in error.details()
    assert hpc.calls("sbatch") == []


def test_queue_timeout_when_job_never_starts(start_slurm_controller, hpc):
    start_slurm_controller(NEST_RESULT_TIMEOUT="0.5", **FAST_POLLING)
    hpc.hold_jobs("PENDING")

    @nest.remote()
    def noop():
        return None

    with pytest.raises(QueueTimeoutError) as caught:
        noop.remote()
    error = caught.value
    [job] = _worker_jobs(hpc)
    assert not isinstance(error, ExecutionTimeoutError)
    assert error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert error.job_id == job["job_id"]
    assert "did not start within 0.5 seconds. Last state: PENDING" in error.details()
    # A timeout never cancels the job.
    assert hpc.calls("scancel") == []
    assert hpc.job_state(job["job_id"]) == "PENDING"


def test_execution_timeout_when_running_job_does_not_finish(start_slurm_controller, hpc):
    start_slurm_controller(NEST_RESULT_TIMEOUT="0.5", **FAST_POLLING)
    hpc.hold_jobs("RUNNING")

    @nest.remote()
    def noop():
        return None

    with pytest.raises(ExecutionTimeoutError) as caught:
        noop.remote()
    [job] = _worker_jobs(hpc)
    assert caught.value.job_id == job["job_id"]
    assert "did not finish within 0.5 seconds. Last state: RUNNING" in caught.value.details()
    assert hpc.calls("scancel") == []


def test_slurm_time_limit_is_an_execution_timeout(slurm_controller, hpc):
    hpc.hold_jobs("TIMEOUT")

    @nest.remote()
    def noop():
        return None

    with pytest.raises(ExecutionTimeoutError) as caught:
        noop.remote()
    job_id = _worker_jobs(hpc)[0]["job_id"]
    assert caught.value.details() == f"Slurm job {job_id} exceeded its Slurm time limit: TIMEOUT"


def test_environment_build_queue_timeout(start_slurm_controller, hpc, project):
    start_slurm_controller(NEST_ENVIRONMENT_TIMEOUT="0.5", **FAST_POLLING)
    hpc.hold_jobs("PENDING")

    @nest.remote(project=project)
    def noop():
        return None

    with pytest.raises(QueueTimeoutError) as caught:
        noop.remote()
    [build] = _build_jobs(hpc)
    assert caught.value.job_id == build["job_id"]
    assert caught.value.details().startswith(f"Environment build {build['job_id']} did not start")
    assert _worker_jobs(hpc) == []


def test_client_rpc_deadline_is_an_rpc_timeout(start_slurm_controller, hpc, monkeypatch):
    start_slurm_controller(NEST_RESULT_TIMEOUT="3", **FAST_POLLING)
    hpc.hold_jobs("PENDING")
    monkeypatch.setenv("NEST_RPC_TIMEOUT", "0.5")

    @nest.remote()
    def noop():
        return None

    with pytest.raises(RpcTimeoutError) as caught:
        noop.remote()
    error = caught.value
    assert isinstance(error, TimeoutError)
    assert isinstance(error.__cause__, grpc.RpcError)
    assert error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert "did not reply within 0.5 seconds" in str(error)


def test_timeout_kinds_are_distinct():
    kinds = [SSHTimeoutError, QueueTimeoutError, ExecutionTimeoutError, RpcTimeoutError]
    assert len({kind.kind for kind in kinds}) == len(kinds)
    for kind in kinds:
        assert issubclass(kind, NestTimeoutError)
        assert issubclass(kind, TimeoutError)
        assert [other for other in kinds if issubclass(kind, other)] == [kind]
    # An ambiguous submission is not a plain timeout: it must not be retried blindly.
    assert not issubclass(SubmissionUnknownError, TimeoutError)


# Ambiguous submissions


def test_ssh_timeout_during_sbatch_is_reported_and_not_resubmitted(start_slurm_controller, hpc):
    start_slurm_controller(NEST_SSH_TIMEOUT="1", **FAST_POLLING)
    hpc.hang_ssh("sbatch --parsable", after=True)

    @nest.remote()
    def noop():
        return None

    with pytest.raises(SubmissionUnknownError) as caught:
        noop.remote()
    error = caught.value
    # sbatch really submitted the job, but the Controller never received its ID.
    [job] = _worker_jobs(hpc)
    assert hpc.job_state(job["job_id"]) == "COMPLETED"
    assert error.code() == grpc.StatusCode.UNKNOWN
    assert error.job_id is None
    execution_id = job["args"][0]
    assert "status of project/scripts/worker.sbatch is unknown" in error.details()
    assert f"Execution ID: {execution_id}" in error.details()
    # Exactly one submission, and no polling or download afterward.
    assert len(hpc.calls("sbatch")) == 1
    last_ssh = hpc.calls("ssh")[-1]["argv"][-1]
    assert "sbatch" in last_ssh


def test_ssh_drop_after_sbatch_started_is_ambiguous(slurm_controller, hpc):
    hpc.drop_ssh("sbatch --parsable")

    @nest.remote()
    def noop():
        return None

    with pytest.raises(SubmissionUnknownError) as caught:
        noop.remote()
    assert "SSH disconnected during sbatch" in caught.value.details()
    assert "closed by remote host" in caught.value.details()
    assert len(hpc.calls("sbatch")) == 1


def test_ambiguous_submit_job_rpc_reports_unknown_kind(start_slurm_controller, hpc, tmp_path):
    stub = start_slurm_controller(NEST_SSH_TIMEOUT="1")
    (tmp_path / "hello.sbatch").write_text("#!/bin/bash\nexit 0\n")
    hpc.hang_ssh("sbatch --parsable", after=True)

    with pytest.raises(grpc.RpcError) as caught:
        stub.SubmitJob(
            execution_pb2.SubmitJobRequest(workdir=str(tmp_path), script="hello.sbatch"),
            timeout=30,
        )
    error = cast(grpc.Call, caught.value)
    assert error.code() == grpc.StatusCode.UNKNOWN
    assert _metadata(error)[ERROR_KIND_METADATA] == "submission_unknown"
    assert len(hpc.calls("sbatch")) == 1


def test_rejected_submit_job_rpc_keeps_failed_precondition(slurm_controller, hpc, tmp_path):
    with pytest.raises(grpc.RpcError) as caught:
        slurm_controller.SubmitJob(
            execution_pb2.SubmitJobRequest(workdir=str(tmp_path / "missing"), script="x.sbatch"),
            timeout=30,
        )
    error = cast(grpc.Call, caught.value)
    assert error.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert _metadata(error)[ERROR_KIND_METADATA] == "remote_command_error"
    assert hpc.calls("sbatch") == []


def test_managed_ssh_profile_runs_without_any_ssh_config(start_slurm_controller, hpc, tmp_path):
    """Mode B: a Provider Profile from ENV alone, with no SSH config or Tailscale setting."""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"{hpc.host} ssh-ed25519 AAAA\n")
    start_slurm_controller(
        NEST_SLURM_HOST="",
        NEST_SLURM_ACCOUNT="",
        NEST_SLURM_WORKDIR="",
        NEST_PROVIDERS="fake",
        NEST_PROVIDER_FAKE_SSH_MODE="B",
        NEST_PROVIDER_FAKE_LOGIN_NODES=f"{hpc.host}:2222",
        NEST_PROVIDER_FAKE_SSH_USER="svc-nest",
        NEST_PROVIDER_FAKE_ACCOUNT="managed-account",
        NEST_PROVIDER_FAKE_PARTITION="gpu",
        NEST_PROVIDER_FAKE_TIME="00:10:00",
        NEST_PROVIDER_FAKE_WORKDIR=str(hpc.base),
        NEST_SSH_KNOWN_HOSTS=str(known_hosts),
        # Split so the repository credential scan does not match the placeholder.
        NEST_SSH_PRIVATE_KEY="-----BEGIN OPENSSH "
        "PRIVATE KEY-----\\nplaceholder\\n"
        "-----END OPENSSH "
        "PRIVATE KEY-----",
    )

    @nest.remote()
    def ping():
        return "pong"

    assert ping.remote() == "pong"

    for call in hpc.calls("ssh"):
        argv = call["argv"]
        assert argv[:2] == ["-F", "none"]
        assert "StrictHostKeyChecking=yes" in argv
        assert f"UserKnownHostsFile={known_hosts}" in argv
        assert argv[-6:-2] == ["-p", "2222", "-l", "svc-nest"]
        assert argv[-2] == hpc.host
    (job,) = _worker_jobs(hpc)
    assert job["flags"] == [
        "--parsable",
        "--account=managed-account",
        "--partition=gpu",
        "--cpus-per-task=1",
        "--time=00:10:00",
    ]
