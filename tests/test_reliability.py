"""Unit regressions for resource forwarding, timeout classification, and submission safety.

These are `unittest` cases so they also run inside the Controller image.
"""

import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

import grpc

from nest.backends.slurm import (
    PENDING_BLOCKED,
    PENDING_LIMIT,
    PENDING_QUEUED,
    SlurmBackend,
    SlurmConfig,
    SlurmJobInfo,
    classify_pending_reason,
)
from nest.client import _controller_error
from nest.config import load_client_config, load_controller_config
from nest.controller import ExecutionService
from nest.errors import (
    ERROR_KIND_METADATA,
    INFRASTRUCTURE_ERRORS,
    JOB_ID_METADATA,
    PENDING_REASON_METADATA,
    ExecutionTimeoutError,
    InfrastructureError,
    JobBlockedError,
    QueueTimeoutError,
    RemoteCommandError,
    RpcTimeoutError,
    SlurmJobError,
    SSHError,
    SSHTimeoutError,
    SubmissionUnknownError,
)
from nest.proto import execution_pb2
from nest.remote import Resources


def completed(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(["ssh"], returncode, stdout=stdout, stderr=stderr)


def backend(**config):
    return SlurmBackend(
        SlurmConfig(
            ssh_host="hpc",
            account="acct",
            time="00:05:00",
            work_root="/nest/jobs",
            worker_workdir="/nest",
            **config,
        )
    )


class SubmitTests(unittest.TestCase):
    def submit_command(self, result, **kwargs):
        slurm = backend()
        with mock.patch.object(slurm, "_ssh_process", return_value=result) as run:
            job_id = slurm.submit("/work", "job.sbatch", **kwargs)
        run.assert_called_once()
        return job_id, run.call_args.args[0]

    def test_cpu_is_forwarded_as_cpus_per_task(self):
        job_id, command = self.submit_command(completed(stdout=b"42;cluster\n"), gpu=0, cpu=6)
        self.assertEqual(job_id, 42)
        self.assertIn(
            "sbatch --parsable --account=acct --partition=h200_256gpu --cpus-per-task=6 "
            "--time=00:05:00 job.sbatch",
            command,
        )

    def test_inherit_time_false_omits_only_the_default_time(self):
        _, command = self.submit_command(completed(stdout=b"42\n"), gpu=0, inherit_time=False)
        self.assertIn(
            "sbatch --parsable --account=acct --partition=h200_256gpu job.sbatch", command
        )

        _, command = self.submit_command(
            completed(stdout=b"42\n"), gpu=0, time="00:20:00", inherit_time=False
        )
        self.assertIn("--partition=h200_256gpu --time=00:20:00 job.sbatch", command)

    def test_zero_cpu_omits_the_flag(self):
        _, command = self.submit_command(completed(stdout=b"42\n"), gpu=1, cpu=0)
        self.assertNotIn("--cpus-per-task", command)
        self.assertIn("--gpus-per-node=1", command)

    def test_negative_cpu_is_rejected_without_ssh(self):
        slurm = backend()
        with (
            mock.patch.object(slurm, "_ssh_process") as run,
            self.assertRaisesRegex(ValueError, "CPU count cannot be negative"),
        ):
            slurm.submit("/work", cpu=-1)
        run.assert_not_called()

    def test_job_id_is_read_from_the_last_output_line(self):
        output = b"nest-sbatch-started\nWelcome to the cluster\n77;cluster\n"
        job_id, _ = self.submit_command(completed(stdout=output))
        self.assertEqual(job_id, 77)

    def test_timeout_is_ambiguous_and_never_retried(self):
        slurm = backend(ssh_timeout=7)
        timeout = subprocess.TimeoutExpired(["ssh"], 7, output=b"nest-sbatch-started\n")
        with (
            mock.patch.object(slurm, "_ssh_process", side_effect=timeout) as run,
            self.assertRaises(SubmissionUnknownError) as caught,
        ):
            slurm.submit("/work", "job.sbatch")
        run.assert_called_once()
        self.assertIn("timed out after 7 seconds", str(caught.exception))
        self.assertIn("will not resubmit", str(caught.exception))
        self.assertNotIsInstance(caught.exception, TimeoutError)

    def test_ssh_failure_before_the_remote_command_is_not_ambiguous(self):
        result = completed(255, stderr=b"ssh: connect to host hpc port 22: Connection refused")
        slurm = backend()
        with (
            mock.patch.object(slurm, "_ssh_process", return_value=result),
            self.assertRaises(SSHError),
        ):
            slurm.submit("/work")

    def test_ssh_failure_after_sbatch_started_is_ambiguous(self):
        result = completed(
            255, stdout=b"nest-sbatch-started\n", stderr=b"Connection to hpc closed."
        )
        slurm = backend()
        with (
            mock.patch.object(slurm, "_ssh_process", return_value=result) as run,
            self.assertRaises(SubmissionUnknownError),
        ):
            slurm.submit("/work")
        run.assert_called_once()

    def test_rejected_submission_is_a_remote_command_error(self):
        result = completed(1, stdout=b"nest-sbatch-started\n", stderr=b"Invalid account")
        slurm = backend()
        with (
            mock.patch.object(slurm, "_ssh_process", return_value=result),
            self.assertRaises(RemoteCommandError) as caught,
        ):
            slurm.submit("/work")
        self.assertEqual(caught.exception.returncode, 1)
        self.assertIn("Invalid account", str(caught.exception))

    def test_successful_sbatch_without_job_id_is_ambiguous(self):
        slurm = backend()
        with (
            mock.patch.object(
                slurm, "_ssh_process", return_value=completed(stdout=b"nest-sbatch-started\n")
            ),
            self.assertRaisesRegex(SubmissionUnknownError, "Unexpected sbatch output"),
        ):
            slurm.submit("/work")

    def test_submit_payload_names_the_execution_on_ambiguity(self):
        slurm = backend()
        with (
            mock.patch.object(slurm, "stage_payload", return_value="/nest/jobs/" + "a" * 32),
            mock.patch.object(slurm, "submit", side_effect=SubmissionUnknownError("lost")) as sub,
            self.assertRaises(SubmissionUnknownError) as caught,
        ):
            slurm.submit_payload(b"payload", gpu=0, cpu=2)
        sub.assert_called_once()
        self.assertEqual(sub.call_args.kwargs["cpu"], 2)
        self.assertIn("Execution ID: " + "a" * 32, str(caught.exception))
        self.assertIn("/nest/jobs/" + "a" * 32 + "/output.pkl", str(caught.exception))


class SshClassificationTests(unittest.TestCase):
    def test_timeout_raises_ssh_timeout(self):
        slurm = backend(ssh_timeout=3)
        with (
            mock.patch(
                "nest.backends.slurm.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["ssh"], 3),
            ),
            self.assertRaises(SSHTimeoutError) as caught,
        ):
            slurm.status(1)
        error = caught.exception
        # Compatible with callers that caught the earlier generic exceptions.
        self.assertIsInstance(error, TimeoutError)
        self.assertIsInstance(error, RuntimeError)
        self.assertIsInstance(error, grpc.RpcError)
        self.assertEqual(error.code(), grpc.StatusCode.DEADLINE_EXCEEDED)

    def test_uploads_use_the_same_classification(self):
        slurm = backend()
        with (
            mock.patch(
                "nest.backends.slurm.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["ssh"], 60),
            ),
            self.assertRaises(SSHTimeoutError),
        ):
            slurm.stage_payload(b"payload")

    def test_exit_255_is_an_ssh_error_and_others_are_command_errors(self):
        slurm = backend()
        with (
            mock.patch(
                "nest.backends.slurm.subprocess.run", return_value=completed(255, stderr=b"denied")
            ),
            self.assertRaises(SSHError),
        ):
            slurm.cancel(1)
        with (
            mock.patch(
                "nest.backends.slurm.subprocess.run", return_value=completed(1, stderr=b"bad job")
            ),
            self.assertRaises(RemoteCommandError),
        ):
            slurm.cancel(1)

    def test_ssh_timeout_comes_from_config(self):
        slurm = backend(ssh_timeout=12.5)
        with mock.patch(
            "nest.backends.slurm.subprocess.run", return_value=completed(stdout=b"RUNNING\n")
        ) as run:
            self.assertEqual(slurm.status(5), "RUNNING")
        self.assertEqual(run.call_args.kwargs["timeout"], 12.5)


def job(state, reason=None, **fields):
    return SlurmJobInfo(job_id=9, state=state, reason=reason, **fields)


class Polling:
    remote: mock.Mock

    def wait(self, states, timeout=1.0, scontrol="", **config):
        """Polls job 9 through `states`; each poll advances the clock by one second."""
        slurm = backend(**config)
        clock = iter(range(1000))
        if isinstance(states, list):
            states = [job(state) if isinstance(state, str) else state for state in states]
        with (
            mock.patch.object(slurm, "job_info", side_effect=states),
            mock.patch.object(slurm, "_run_remote", return_value=scontrol) as self.remote,
            mock.patch("nest.backends.slurm.time.sleep"),
            mock.patch("nest.backends.slurm.time.monotonic", side_effect=lambda: next(clock)),
        ):
            slurm._wait_job(9, "Slurm job", timeout, 0.1)


class WaitTests(Polling, unittest.TestCase):
    def test_job_that_never_starts_is_a_queue_timeout(self):
        with self.assertRaises(QueueTimeoutError) as caught:
            self.wait(["PENDING"] * 5, timeout=2)
        self.assertEqual(caught.exception.job_id, 9)
        self.assertNotIsInstance(caught.exception, ExecutionTimeoutError)

    def test_job_that_started_is_an_execution_timeout(self):
        with self.assertRaises(ExecutionTimeoutError) as caught:
            self.wait(["PENDING", "RUNNING", "COMPLETING", "COMPLETING"], timeout=3)
        self.assertIn("Last state: COMPLETING", str(caught.exception))

    def test_slurm_timeout_state_is_an_execution_timeout(self):
        with self.assertRaisesRegex(ExecutionTimeoutError, "exceeded its Slurm time limit"):
            self.wait(["RUNNING", "TIMEOUT"], timeout=100)

    def test_failure_state_is_a_job_error(self):
        with self.assertRaises(SlurmJobError) as caught:
            self.wait(["RUNNING", "NODE_FAIL"], timeout=100)
        self.assertEqual(str(caught.exception), "Slurm job 9 failed: NODE_FAIL")
        self.assertNotIsInstance(caught.exception, TimeoutError)

    def test_ssh_failure_while_polling_names_the_submitted_job(self):
        with self.assertRaises(SSHTimeoutError) as caught:
            self.wait(SSHTimeoutError("status query timed out"))
        self.assertEqual(caught.exception.job_id, 9)

    def test_defaults_come_from_config(self):
        slurm = backend(result_timeout=11, environment_timeout=22, poll_interval=0.5)
        with mock.patch.object(slurm, "_wait_job") as wait:
            slurm.wait_environment(3)
        wait.assert_called_once_with(3, "Environment build", 22, 0.5)
        with (
            mock.patch.object(slurm, "_wait_job") as wait,
            mock.patch.object(slurm, "_ssh", return_value=b"result"),
        ):
            self.assertEqual(slurm.wait_result(4, "/nest/jobs/x"), b"result")
        wait.assert_called_once_with(4, "Slurm job", 11, 0.5)


class PendingReasonTests(unittest.TestCase):
    def test_reasons_are_classified_by_whether_waiting_clears_them(self):
        cases = {
            None: PENDING_QUEUED,
            "Priority": PENDING_QUEUED,
            "Resources": PENDING_QUEUED,
            "Dependency": PENDING_QUEUED,
            "SomethingNew": PENDING_QUEUED,
            "MaxGRESPerAccount": PENDING_LIMIT,
            "QOSMaxGRESPerUser": PENDING_LIMIT,
            "AssocGrpGRES": PENDING_LIMIT,
            "AssocGrpCPUMinutesLimit": PENDING_LIMIT,
            "QOSMaxWallDurationPerJobLimit": PENDING_BLOCKED,
            "QOSMinGRES": PENDING_BLOCKED,
            "PartitionConfig": PENDING_BLOCKED,
            "InvalidQOS": PENDING_BLOCKED,
            "JobHeldUser": PENDING_BLOCKED,
        }
        for reason, category in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(classify_pending_reason(reason), category)

    def test_job_info_parses_squeue_fields(self):
        slurm = backend()
        with mock.patch.object(
            slurm, "_run_remote", return_value="PENDING|MaxGRESPerAccount|gpu|normal|acct"
        ) as remote:
            info = slurm.job_info(9)
        self.assertEqual(
            info, job("PENDING", "MaxGRESPerAccount", partition="gpu", qos="normal", account="acct")
        )
        self.assertIn("--format='%T|%r|%P|%q|%a'", remote.call_args.args[0])
        self.assertEqual(info.pending_category, PENDING_LIMIT)

    def test_job_info_treats_none_as_no_reason(self):
        slurm = backend()
        with mock.patch.object(slurm, "_run_remote", return_value="RUNNING|None|gpu|normal|acct"):
            info = slurm.job_info(9)
        self.assertIsNone(info.reason)
        self.assertIsNone(info.pending_category)
        with mock.patch.object(slurm, "_run_remote", return_value="RUNNING|None|gpu|normal|acct"):
            self.assertEqual(slurm.status(9), "RUNNING")

    def test_job_info_falls_back_to_sacct_state(self):
        slurm = backend()
        with mock.patch.object(slurm, "_run_remote", side_effect=["", "COMPLETED"]):
            self.assertEqual(slurm.job_info(9), job("COMPLETED"))


class PendingWaitTests(Polling, unittest.TestCase):
    gpu_limit = job("PENDING", "MaxGRESPerAccount", partition="gpu", qos="normal", account="acct")
    bad_partition = job("PENDING", "PartitionConfig", partition="gpu", qos="normal", account="a")

    def test_normal_congestion_only_ends_in_the_queue_deadline(self):
        with self.assertRaises(QueueTimeoutError) as caught:
            self.wait([job("PENDING", "Priority")] * 3 + [job("PENDING", "Resources")] * 3, 4)
        error = caught.exception
        self.assertNotIsInstance(error, JobBlockedError)
        self.assertEqual(error.pending_reason, "Resources")
        self.assertIn("did not start within 4 seconds. Last state: PENDING.", str(error))
        self.assertIn("Pending reason: Resources", str(error))
        self.assertIn("Pending reasons seen: Priority, Resources.", str(error))

    def test_congestion_shorter_than_the_deadline_completes(self):
        self.wait([job("PENDING", "Priority")] * 3 + [job("RUNNING"), job("COMPLETED")], 10)

    def test_gpu_limit_waits_until_the_deadline_and_explains_why(self):
        with (
            self.assertLogs("nest.backends.slurm", "WARNING") as logs,
            self.assertRaises(QueueTimeoutError) as caught,
        ):
            self.wait([self.gpu_limit] * 5, 3, scontrol="JobId=9 ReqTRES=cpu=1,gres/gpu=8")
        error = caught.exception
        self.assertNotIsInstance(error, JobBlockedError)
        self.assertEqual((error.job_id, error.pending_reason), (9, "MaxGRESPerAccount"))
        message = str(error)
        self.assertIn(
            "Pending reason: MaxGRESPerAccount (partition=gpu qos=normal account=acct "
            "requested=cpu=1,gres/gpu=8)",
            message,
        )
        self.assertIn("QoS or association limit", message)
        self.assertIn("The job was not cancelled.", message)
        # Warned once, when the reason first appeared, with the job's context.
        [warning] = logs.output
        self.assertIn("reason=MaxGRESPerAccount", warning)
        self.assertIn("requested=cpu=1,gres/gpu=8", warning)

    def test_scontrol_is_queried_once_per_reason(self):
        with self.assertRaises(QueueTimeoutError):
            self.wait([self.gpu_limit] * 5, 3, scontrol="ReqTRES=gres/gpu=8")
        self.remote.assert_called_once_with("scontrol show job --oneliner 9")

    def test_scontrol_failure_does_not_hide_the_reason(self):
        slurm = backend()
        clock = iter(range(1000))
        with (
            mock.patch.object(slurm, "job_info", side_effect=[self.gpu_limit] * 3),
            mock.patch.object(slurm, "_run_remote", side_effect=RemoteCommandError("no")),
            mock.patch("nest.backends.slurm.time.sleep"),
            mock.patch("nest.backends.slurm.time.monotonic", side_effect=lambda: next(clock)),
            self.assertRaises(QueueTimeoutError) as caught,
        ):
            slurm._wait_job(9, "Slurm job", 2, 0.1)
        self.assertEqual(caught.exception.pending_reason, "MaxGRESPerAccount")
        self.assertNotIn("requested=", str(caught.exception))

    def test_gpu_limit_stops_early_when_a_limit_timeout_is_set(self):
        with self.assertRaises(JobBlockedError) as caught:
            self.wait([self.gpu_limit] * 10, 100, limit_timeout=2)
        error = caught.exception
        self.assertIsInstance(error, QueueTimeoutError)
        self.assertEqual(error.pending_reason, "MaxGRESPerAccount")
        self.assertIn("after 2 seconds pending on a QoS or association limit", str(error))
        self.assertIn("scancel 9", str(error))

    def test_incompatible_partition_stops_before_the_deadline(self):
        with self.assertRaises(JobBlockedError) as caught:
            self.wait(
                [job("PENDING", "Priority")] + [self.bad_partition] * 10, 600, blocked_timeout=3
            )
        error = caught.exception
        self.assertEqual(error.job_id, 9)
        self.assertEqual(error.pending_reason, "PartitionConfig")
        self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
        message = str(error)
        self.assertIn(
            "after 3 seconds pending for a reason it cannot outwait: PartitionConfig", message
        )
        self.assertIn("does not fit partition gpu", message)
        self.assertIn("The job was not cancelled", message)

    def test_blocked_timer_restarts_when_the_job_moves_on(self):
        states = [self.bad_partition, self.bad_partition, job("PENDING", "Priority")]
        states += [self.bad_partition, self.bad_partition, job("RUNNING"), job("COMPLETED")]
        self.wait(states, 100, blocked_timeout=2)

    def test_blocked_job_is_cancelled_only_when_configured(self):
        slurm = backend(blocked_timeout=0, cancel_blocked=True)
        with (
            mock.patch.object(slurm, "job_info", return_value=self.bad_partition),
            mock.patch.object(slurm, "_run_remote", return_value="") as remote,
            self.assertRaises(JobBlockedError) as caught,
        ):
            slurm._wait_job(9, "Slurm job", 100, 0.1)
        remote.assert_any_call("scancel 9")
        self.assertIn("requested its cancellation and will not resubmit it", str(caught.exception))

    def test_failed_cancellation_keeps_the_diagnostic(self):
        slurm = backend(blocked_timeout=0, cancel_blocked=True)
        with (
            mock.patch.object(slurm, "job_info", return_value=self.bad_partition),
            mock.patch.object(slurm, "_with_details", side_effect=lambda info: info),
            mock.patch.object(slurm, "cancel", side_effect=SSHError("unreachable")),
            self.assertRaises(JobBlockedError) as caught,
        ):
            slurm._wait_job(9, "Slurm job", 100, 0.1)
        self.assertEqual(caught.exception.pending_reason, "PartitionConfig")
        self.assertIn("Cancelling it failed, so it may still be queued", str(caught.exception))

    def test_pending_reasons_are_kept_when_execution_times_out(self):
        with self.assertRaises(ExecutionTimeoutError) as caught:
            self.wait([self.gpu_limit, job("RUNNING"), job("RUNNING"), job("RUNNING")], 3)
        self.assertIn("Pending reasons seen: MaxGRESPerAccount.", str(caught.exception))

    def test_negative_pending_timeouts_are_rejected(self):
        with self.assertRaises(ValueError):
            backend(blocked_timeout=-1)
        with self.assertRaises(ValueError):
            backend(limit_timeout=-1)


class FakeCall(grpc.RpcError):
    def __init__(self, code, details="", metadata=()):
        self._code, self._details, self._metadata = code, details, metadata

    def code(self):
        return self._code

    def details(self):
        return self._details

    def trailing_metadata(self):
        return self._metadata


class ClientClassificationTests(unittest.TestCase):
    def test_every_kind_round_trips(self):
        for kind, error_class in INFRASTRUCTURE_ERRORS.items():
            with self.subTest(kind=kind):
                call = FakeCall(
                    error_class.status,
                    "message",
                    ((ERROR_KIND_METADATA, kind), (JOB_ID_METADATA, "123")),
                )
                error = _controller_error(call, "addr", 900)
                self.assertIs(type(error), error_class)
                assert isinstance(error, InfrastructureError)
                self.assertEqual(error.job_id, 123)
                self.assertEqual(str(error), "message")
                self.assertEqual(error.code(), error_class.status)

    def test_pending_reason_round_trips(self):
        call = FakeCall(
            grpc.StatusCode.FAILED_PRECONDITION,
            "blocked",
            (
                (ERROR_KIND_METADATA, "job_blocked"),
                (JOB_ID_METADATA, "5"),
                (PENDING_REASON_METADATA, "MaxGRESPerAccount"),
            ),
        )
        error = _controller_error(call, "addr", 900)
        assert isinstance(error, JobBlockedError)
        self.assertEqual((error.job_id, error.pending_reason), (5, "MaxGRESPerAccount"))
        queued = FakeCall(
            grpc.StatusCode.DEADLINE_EXCEEDED, "q", ((ERROR_KIND_METADATA, "queue_timeout"),)
        )
        error = _controller_error(queued, "addr", 900)
        assert isinstance(error, QueueTimeoutError)
        self.assertIsNone(error.pending_reason)

    def test_deadline_without_kind_is_an_rpc_timeout(self):
        error = _controller_error(FakeCall(grpc.StatusCode.DEADLINE_EXCEEDED), "addr", 12)
        self.assertIsInstance(error, RpcTimeoutError)
        self.assertIn("within 12 seconds", str(error))

    def test_unavailable_without_kind_is_a_controller_connection_error(self):
        error = _controller_error(FakeCall(grpc.StatusCode.UNAVAILABLE), "addr", 900)
        self.assertIs(type(error), ConnectionError)

    def test_unclassified_errors_are_reraised(self):
        self.assertIsNone(_controller_error(FakeCall(grpc.StatusCode.INTERNAL), "addr", 900))

    def test_rpc_timeout_setting(self):
        def timeout(value):
            return load_client_config({"NEST_RPC_TIMEOUT": value}).rpc_timeout

        self.assertEqual(timeout(""), 900)
        self.assertEqual(timeout("2.5"), 2.5)
        for bad in ("0", "-1", "soon", "inf", "nan"):
            with self.assertRaises(ValueError):
                timeout(bad)


class Aborted(Exception):
    pass


class FakeContext:
    def __init__(self):
        self.metadata = ()
        self.status = None

    def invocation_metadata(self):
        return ()

    def set_trailing_metadata(self, metadata):
        self.metadata = metadata

    def abort(self, code, details):
        self.status, self._details = code, details
        raise Aborted

    def code(self):
        return self.status

    def details(self):
        return getattr(self, "_details", None)


class ControllerTests(unittest.TestCase):
    def execute(self, backend, **fields):
        context = FakeContext()
        service = ExecutionService(backend)
        with mock.patch.dict("os.environ", {"NEST_EXECUTOR": "slurm"}):
            try:
                result = service.Execute(execution_pb2.ExecuteRequest(**fields), context)
            except Aborted:
                result = None
        return result, context

    def test_cpu_and_gpu_are_forwarded_to_worker_only(self):
        calls = {}

        def submit_payload(payload, **kwargs):
            calls["submit"] = kwargs
            return 5, "/nest/jobs/x"

        def prepare(**kwargs):
            calls["prepare"] = kwargs
            return "/nest/environments/" + "b" * 64

        fake = SimpleNamespace(
            submit_payload=submit_payload,
            prepare_environment_bytes=prepare,
            wait_result=lambda job_id, job_dir: b"result",
        )
        environment = execution_pb2.EnvironmentFiles(pyproject_toml=b"p", uv_lock=b"l")
        result, _ = self.execute(fake, payload=b"x", gpu=2, cpu=4, environment=environment)
        assert result is not None
        self.assertEqual(result.result, b"result")
        self.assertEqual((calls["submit"]["gpu"], calls["submit"]["cpu"]), (2, 4))
        self.assertEqual(calls["prepare"], {"pyproject_toml": b"p", "uv_lock": b"l"})

    def test_negative_resources_are_invalid_arguments(self):
        fake = SimpleNamespace(submit_payload=mock.Mock())
        _, context = self.execute(fake, payload=b"x", cpu=-1)
        self.assertEqual(context.status, grpc.StatusCode.INVALID_ARGUMENT)
        fake.submit_payload.assert_not_called()

    def test_infrastructure_errors_carry_status_kind_and_job_id(self):
        cases = [
            (SSHTimeoutError("ssh"), grpc.StatusCode.DEADLINE_EXCEEDED),
            (QueueTimeoutError("queued", job_id=8), grpc.StatusCode.DEADLINE_EXCEEDED),
            (ExecutionTimeoutError("running", job_id=8), grpc.StatusCode.DEADLINE_EXCEEDED),
            (SubmissionUnknownError("unknown"), grpc.StatusCode.UNKNOWN),
            (SSHError("unreachable"), grpc.StatusCode.UNAVAILABLE),
            (SlurmJobError("failed", job_id=8), grpc.StatusCode.INTERNAL),
        ]
        for error, status in cases:
            with self.subTest(kind=error.kind):
                fake = SimpleNamespace(submit_payload=mock.Mock(side_effect=error))
                _, context = self.execute(fake, payload=b"x")
                self.assertEqual(context.status, status)
                metadata = dict(context.metadata)
                self.assertEqual(metadata[ERROR_KIND_METADATA], error.kind)
                if error.job_id is None:
                    self.assertNotIn(JOB_ID_METADATA, metadata)
                else:
                    self.assertEqual(metadata[JOB_ID_METADATA], "8")
                fake.submit_payload.assert_called_once()

    def test_pending_reason_is_sent_as_metadata(self):
        error = JobBlockedError("blocked", job_id=8, pending_reason="PartitionConfig")
        fake = SimpleNamespace(submit_payload=mock.Mock(side_effect=error))
        _, context = self.execute(fake, payload=b"x")
        self.assertEqual(context.status, grpc.StatusCode.FAILED_PRECONDITION)
        metadata = dict(context.metadata)
        self.assertEqual(metadata[ERROR_KIND_METADATA], "job_blocked")
        self.assertEqual(metadata[PENDING_REASON_METADATA], "PartitionConfig")

    def test_unprintable_pending_reason_is_not_sent(self):
        error = QueueTimeoutError("queued", job_id=8, pending_reason="bad\nreason")
        fake = SimpleNamespace(submit_payload=mock.Mock(side_effect=error))
        _, context = self.execute(fake, payload=b"x")
        self.assertNotIn(PENDING_REASON_METADATA, dict(context.metadata))

    def test_pending_settings(self):
        config = load_controller_config({})
        self.assertEqual(config.pending_blocked_timeout, 120)
        self.assertIsNone(config.pending_limit_timeout)
        self.assertFalse(config.cancel_blocked_jobs)
        config = load_controller_config(
            {
                "NEST_PENDING_BLOCKED_TIMEOUT": "30",
                "NEST_PENDING_LIMIT_TIMEOUT": "900",
                "NEST_CANCEL_BLOCKED_JOBS": "true",
            }
        )
        self.assertEqual(config.pending_blocked_timeout, 30)
        self.assertEqual(config.pending_limit_timeout, 900)
        self.assertTrue(config.cancel_blocked_jobs)
        for name in ("NEST_PENDING_BLOCKED_TIMEOUT", "NEST_PENDING_LIMIT_TIMEOUT"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                load_controller_config({name: "-1"})

    def test_timeout_settings_are_validated(self):
        def timeout(value):
            return load_controller_config({"NEST_SSH_TIMEOUT": value}).ssh_timeout

        self.assertEqual(timeout(""), 60)
        self.assertEqual(timeout("0.25"), 0.25)
        for bad in ("0", "-5", "ten", "inf"):
            with self.assertRaisesRegex(ValueError, "NEST_SSH_TIMEOUT"):
                timeout(bad)


class ResourcesTests(unittest.TestCase):
    def test_counts_are_validated(self):
        self.assertEqual(Resources(gpu=0, cpu=0), Resources(0, 0))
        with self.assertRaises(ValueError):
            Resources(cpu=-1)
        with self.assertRaises(ValueError):
            Resources(gpu=-1)
        with self.assertRaises(TypeError):
            Resources(cpu=1.5)  # ty: ignore[invalid-argument-type]
        with self.assertRaises(TypeError):
            Resources(cpu=True)


if __name__ == "__main__":
    unittest.main()
