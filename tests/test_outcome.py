"""The worker's outcome sidecar (`outcome.json`) and how the Controller reads it (#73)."""

import json
import os
import subprocess
import sys
from unittest import mock

import cloudpickle
import pytest

from nest import outcome
from nest.admission import completion
from nest.backends.slurm import SlurmBackend, SlurmConfig
from nest.errors import RemoteCommandError, SSHError


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
    _, _, message = completion(caught.value)
    assert SENTINEL not in str(caught.value)
    assert message is not None and SENTINEL not in message
    # No chained JSON error carries the content into a logged traceback either.
    assert caught.value.__cause__ is None


def test_parse_rejects_repeated_keys():
    with pytest.raises(outcome.InvalidOutcomeError, match="repeats a key"):
        outcome.parse(b'{"nest_outcome": 1, "status": "raised", "status": "returned"}')


def test_completion_semantics():
    assert completion(None) == ("succeeded", None, None)
    assert completion(outcome.RemoteOutcome("returned")) == ("succeeded", None, None)
    status, kind, message = completion(outcome.RemoteOutcome("raised", "builtins.KeyError"))
    assert (status, kind) == ("failed", "remote_application_error")
    assert message is not None and "builtins.KeyError" in message
    status, kind, _ = completion(outcome.InvalidOutcomeError("bad"))
    assert (status, kind) == ("failed", "remote_outcome_invalid")


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


def test_staging_removes_a_stale_record():
    with mock.patch.object(SlurmBackend, "_ssh", return_value=b"") as ssh:
        job_dir = backend().stage_payload(b"payload", "b" * 32)
    [command] = ssh.call_args.args
    assert f"rm -f {job_dir}/outcome.json" in command


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
