"""The worker's outcome sidecar (`outcome.json`) and how the Controller reads it (#73)."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import cloudpickle
import pytest

from nest import outcome
from nest.admission import completion
from nest.backends.slurm import SlurmBackend, SlurmConfig
from nest.errors import RemoteCommandError, SSHError

REPOSITORY = Path(__file__).resolve().parent.parent


def backend() -> SlurmBackend:
    return SlurmBackend(
        SlurmConfig(ssh_host="hpc", account="acct", work_root="/nest/jobs", worker_workdir="/nest")
    )


# Records ---------------------------------------------------------------------


def test_write_and_parse_round_trip(tmp_path):
    outcome.write(tmp_path, outcome.RETURNED)
    assert outcome.parse((tmp_path / "outcome.json").read_bytes()) == outcome.RemoteOutcome(
        "returned"
    )
    outcome.write(tmp_path, outcome.RAISED, "torch.cuda.OutOfMemoryError")
    parsed = outcome.parse((tmp_path / "outcome.json").read_bytes())
    assert parsed.raised and parsed.error_type == "torch.cuda.OutOfMemoryError"
    # Atomic: no partial file is left beside it.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["outcome.json"]


def test_write_never_records_an_unsafe_type_name(tmp_path):
    outcome.write(tmp_path, outcome.RAISED, "evil\nname; rm -rf /")
    assert outcome.parse((tmp_path / "outcome.json").read_bytes()).error_type == "unknown"
    outcome.write(tmp_path, outcome.RAISED, "__main__.train.<locals>.Boom")
    parsed = outcome.parse((tmp_path / "outcome.json").read_bytes())
    assert parsed.error_type == "__main__.train.<locals>.Boom"


@pytest.mark.parametrize(
    "name",
    ["mod." + "é" * 200, "x" * 1000, "a." + "Ω" * 300, "pkg." + "A" * 251],
)
def test_written_records_always_parse(tmp_path, name):
    outcome.write(tmp_path, outcome.RAISED, name)
    data = (tmp_path / "outcome.json").read_bytes()
    assert len(data) <= outcome.MAX_OUTCOME_BYTES
    parsed = outcome.parse(data)
    assert parsed.raised
    assert parsed.error_type in ("unknown", name[:256])


def test_clear_removes_a_stale_record(tmp_path):
    outcome.clear(tmp_path)  # nothing there: fine
    outcome.write(tmp_path, outcome.RETURNED)
    outcome.clear(tmp_path)
    assert not (tmp_path / "outcome.json").exists()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x80\x05cloudpickle",
        b"[]",
        b'{"status": "returned"}',
        b'{"nest_outcome": 2, "status": "returned"}',
        b'{"nest_outcome": true, "status": "returned"}',
        b'{"nest_outcome": 1, "status": "succeeded"}',
        b'{"nest_outcome": 1, "status": "returned", "error_type": "x"}',
        b'{"nest_outcome": 1, "status": "raised"}',
        b'{"nest_outcome": 1, "status": "raised", "error_type": 3}',
        b'{"nest_outcome": 1, "status": "raised", "error_type": "a b"}',
        b'{"nest_outcome": 1, "status": "raised", "error_type": "x", "traceback": "t"}',
        b'{"nest_outcome": 1, "status": "returned", "pad": "' + b"x" * 1100 + b'"}',
    ],
)
def test_parse_rejects_anything_but_the_two_record_shapes(data):
    with pytest.raises(outcome.InvalidOutcomeError):
        outcome.parse(data)


SENTINEL = "sk-live-SECRET-SENTINEL"


@pytest.mark.parametrize(
    "data",
    [
        b'{"nest_outcome": "%s", "status": "returned"}',
        b'{"nest_outcome": {"k": "%s"}, "status": "returned"}',
        b'{"nest_outcome": 1, "status": "%s"}',
        b'{"nest_outcome": 1, "status": "raised", "error_type": "%s!"}',
        b'{"nest_outcome": 1, "status": "returned", "%s": 1}',
        b"%s",
    ],
)
def test_invalid_records_never_echo_their_content(data):
    with pytest.raises(outcome.InvalidOutcomeError) as caught:
        outcome.parse(data.replace(b"%s", SENTINEL.encode()))
    decided = completion(caught.value, worker_protocol=2)
    assert decided is not None
    _, _, message = decided
    assert SENTINEL not in str(caught.value)
    assert message is not None and SENTINEL not in message
    # No chained JSON error carries the content into a logged traceback either.
    assert caught.value.__cause__ is None


def test_parse_rejects_repeated_keys():
    with pytest.raises(outcome.InvalidOutcomeError, match="repeats a key"):
        outcome.parse(b'{"nest_outcome": 1, "status": "raised", "status": "returned"}')


def test_completion_semantics():
    raised = outcome.RemoteOutcome("raised", "builtins.KeyError")
    for protocol in (None, 1, 2):
        assert completion(outcome.RemoteOutcome("returned"), worker_protocol=protocol) == (
            "succeeded",
            None,
            None,
        )
        decided = completion(raised, worker_protocol=protocol)
        assert decided is not None
        status, kind, message = decided
        assert (status, kind) == ("failed", "remote_application_error")
        assert message is not None and "builtins.KeyError" in message
        invalid = completion(outcome.InvalidOutcomeError("bad"), worker_protocol=protocol)
        assert invalid is not None and invalid[:2] == ("failed", "remote_outcome_invalid")
        unreadable = completion(outcome.OutcomeUnreadableError("x"), worker_protocol=protocol)
        assert unreadable is not None and unreadable[:2] == ("failed", "remote_outcome_unreadable")
    # No record: an unverified success only for a legacy (or unpinned) worker; for
    # protocol 2 it is undecided, never a success.
    assert completion(None, worker_protocol=None) == ("succeeded", None, None)
    assert completion(None, worker_protocol=1) == ("succeeded", None, None)
    assert completion(None, worker_protocol=2) is None
    assert completion(None, worker_protocol=3) is None


# Reading over SSH --------------------------------------------------------------


def test_read_outcome_is_bounded_plain_json_and_never_touches_the_result():
    slurm = backend()
    reply = b'nest-outcome-present\n{"nest_outcome":1,"status":"raised","error_type":"x.E"}'
    with mock.patch.object(SlurmBackend, "_ssh", return_value=reply) as ssh:
        assert slurm.read_outcome("/nest/jobs/" + "a" * 32) == outcome.RemoteOutcome(
            "raised", "x.E"
        )
    [command] = ssh.call_args.args
    assert f"/nest/jobs/{'a' * 32}/outcome.json" in command
    assert f"head -c {outcome.MAX_OUTCOME_BYTES + 1}" in command
    assert "output.pkl" not in command


def test_read_outcome_without_a_record_is_none():
    with mock.patch.object(SlurmBackend, "_ssh", return_value=b"nest-outcome-absent\n"):
        assert backend().read_outcome("/nest/jobs/x") is None


def test_read_outcome_reports_a_path_it_cannot_read():
    with (
        mock.patch.object(SlurmBackend, "_ssh", return_value=b"nest-outcome-no-access\n"),
        pytest.raises(outcome.OutcomeUnreadableError),
    ):
        backend().read_outcome("/nest/jobs/x")


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("absent", None),
        ("returned", outcome.RemoteOutcome("returned")),
        ("directory", outcome.OutcomeUnreadableError),
        ("dangling", outcome.OutcomeUnreadableError),
        ("no-directory", None),
    ],
)
def test_read_outcome_command_distinguishes_absent_from_unreadable(tmp_path, setup, expected):
    job_dir = tmp_path / "attempts" / "1"
    if setup != "no-directory":
        job_dir.mkdir(parents=True)
    record = job_dir / "outcome.json"
    if setup == "returned":
        outcome.write(job_dir, outcome.RETURNED)
    elif setup == "directory":
        record.mkdir()
    elif setup == "dangling":
        record.symlink_to(tmp_path / "gone")

    def run(command):
        return subprocess.run(["bash", "-c", command], capture_output=True, check=True).stdout

    with mock.patch.object(SlurmBackend, "_ssh", side_effect=run):
        if isinstance(expected, type):
            with pytest.raises(expected):
                backend().read_outcome(str(job_dir))
        else:
            assert backend().read_outcome(str(job_dir)) == expected


def test_attempt_directories_are_per_attempt_under_the_execution():
    slurm = backend()
    execution = "c" * 32
    assert slurm.attempt_dir(execution, None) == f"/nest/jobs/{execution}"
    assert slurm.attempt_dir(execution, 3) == f"/nest/jobs/{execution}/attempts/3"
    for bad in (0, -1, 10**9, True, "1"):
        with pytest.raises(ValueError):
            slurm.attempt_dir(execution, bad)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("script", "protocol"),
    [
        ("#!/bin/bash\n# nest-worker-protocol: 2\nset -e\n", 2),
        ("#!/bin/bash\nset -e\n", 1),
        ("#!/bin/bash\n#  nest-worker-protocol: 2\n", 1),
        ("#!/bin/bash\n# nest-worker-protocol: two\n", 1),
    ],
)
def test_worker_protocol_is_read_from_the_deployed_script(tmp_path, script, protocol):
    (tmp_path / "project" / "scripts").mkdir(parents=True)
    (tmp_path / "project" / "scripts" / "worker.sbatch").write_text(script)
    slurm = SlurmBackend(
        SlurmConfig(
            ssh_host="hpc", account="acct", work_root="/nest/jobs", worker_workdir=str(tmp_path)
        )
    )

    def run(command):
        return subprocess.run(
            ["bash", "-c", command], check=True, capture_output=True, text=True
        ).stdout.strip()

    with mock.patch.object(SlurmBackend, "_run_remote", side_effect=run):
        assert slurm.worker_protocol() == protocol


def test_the_repository_worker_script_declares_this_protocol():
    script = (REPOSITORY / "scripts" / "worker.sbatch").read_text()
    assert f"\n# nest-worker-protocol: {outcome.WORKER_PROTOCOL}\n" in script


def test_read_outcome_reports_an_invalid_or_oversized_record():
    oversized = b"nest-outcome-present\n" + b"{" * (outcome.MAX_OUTCOME_BYTES + 1)
    for reply in (b"nest-outcome-present\n", oversized):
        with (
            mock.patch.object(SlurmBackend, "_ssh", return_value=reply),
            pytest.raises(outcome.InvalidOutcomeError),
        ):
            backend().read_outcome("/nest/jobs/x")


def test_read_outcome_failures_are_infrastructure_errors():
    with (
        mock.patch.object(SlurmBackend, "_ssh", return_value=b"motd banner\n"),
        pytest.raises(RemoteCommandError),
    ):
        backend().read_outcome("/nest/jobs/x")
    with (
        mock.patch.object(SlurmBackend, "_ssh", side_effect=SSHError("down")),
        pytest.raises(SSHError),
    ):
        backend().read_outcome("/nest/jobs/x")


def test_staging_uses_the_attempt_directory_and_removes_a_stale_record():
    with mock.patch.object(SlurmBackend, "_ssh", return_value=b"") as ssh:
        job_dir = backend().stage_payload(b"payload", "b" * 32, attempt=2)
    assert job_dir == f"/nest/jobs/{'b' * 32}/attempts/2"
    [command] = ssh.call_args.args
    assert f"mkdir -p {job_dir} " in command
    assert f"rm -f {job_dir}/outcome.json" in command
    assert f"cat > {job_dir}/input.pkl" in command


def test_publish_replaces_atomically_and_leaves_nothing_on_failure(tmp_path):
    target = tmp_path / "output.pkl"
    target.write_bytes(b"old")
    with pytest.raises(RuntimeError), outcome.publish(target) as f:
        f.write(b"partial")
        raise RuntimeError("pickling failed")
    assert target.read_bytes() == b"old"
    with outcome.publish(target) as f:
        f.write(b"new")
    assert target.read_bytes() == b"new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["output.pkl"]


# The worker --------------------------------------------------------------------


def run_worker(tmp_path, func, *, user_python: bool, env=None):
    input_path, output_path = tmp_path / "input.pkl", tmp_path / "output.pkl"
    input_path.write_bytes(cloudpickle.dumps((func, (), {})))
    argv = [sys.executable, "-m", "nest.worker", str(input_path), str(output_path)]
    if user_python:
        argv.append(sys.executable)
    subprocess.run(argv, env={**os.environ, **(env or {})}, capture_output=True, check=True)
    record = json.loads((tmp_path / "outcome.json").read_text())
    return cloudpickle.loads(output_path.read_bytes()), record


@pytest.mark.parametrize("user_python", [False, True])
def test_worker_writes_the_outcome_after_the_result(tmp_path, user_python):
    def ok():
        return 42

    value, record = run_worker(tmp_path, ok, user_python=user_python)
    assert value == 42
    assert record == {"nest_outcome": 1, "status": "returned"}


@pytest.mark.parametrize("user_python", [False, True])
def test_worker_reports_a_raised_exception_by_type_only(tmp_path, user_python):
    # A stale record from an earlier attempt is never left in place.
    outcome.write(tmp_path, outcome.RETURNED)

    def boom():
        raise KeyError("secret detail")

    value, record = run_worker(tmp_path, boom, user_python=user_python)
    assert value["__nest_remote_error_v1__"] is True
    assert "secret detail" in value["message"]
    assert record == {"nest_outcome": 1, "status": "raised", "error_type": "builtins.KeyError"}


def test_unusable_gpus_are_reported_as_raised(tmp_path):
    def never():
        raise AssertionError("must not run")

    value, record = run_worker(
        tmp_path,
        never,
        user_python=False,
        env={"NEST_GPU_REQUESTED": "1", "CUDA_VISIBLE_DEVICES": ""},
    )
    assert value["type"] == "nest.gpu.GpuUnavailableError"
    assert record == {
        "nest_outcome": 1,
        "status": "raised",
        "error_type": "nest.gpu.GpuUnavailableError",
    }
