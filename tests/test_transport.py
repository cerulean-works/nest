"""Contract tests for the shared Slurm execution core and its connection transports.

The same operations (prepare, submit, status, logs, result, cancel, accounting) run
through the Controller's own SSH (trusted Mode A) and through the client-owned local
OpenSSH transport (for future Mode B), both against `FakeHpc`. An in-memory fake
transport covers error classification and the offline/interactive limitations a
user-owned session advertises.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

from fake_hpc import FakeHpc

from nest.backends.slurm import SlurmAccounting, SlurmBackend, SlurmConfig
from nest.backends.transport import (
    CONTROLLER_SSH_CAPABILITIES,
    LOCAL_OPENSSH_CAPABILITIES,
    CommandResult,
    ControllerSSHTransport,
    ExecutionAuthority,
    LocalOpenSSHTransport,
    Operation,
    SlurmTransport,
    TransportCapabilities,
)
from nest.errors import (
    INFRASTRUCTURE_ERRORS,
    RemoteCommandError,
    SessionUnavailableError,
    SSHError,
    SSHTimeoutError,
    SubmissionUnknownError,
)

CONTRACT_SCRIPT = """#!/bin/bash
#SBATCH --job-name=nest-contract
#SBATCH --output=worker-%j.out
echo contract-output
"""


class FakeTransport(SlurmTransport):
    """An in-memory transport: answers commands from a handler and records them."""

    def __init__(self, handler, *, capabilities=CONTROLLER_SSH_CAPABILITIES, active=True):
        self.handler = handler
        self.capabilities = capabilities
        self.active = active
        self.target = "fake-login"
        self.timeout = 5
        self.commands: list[str] = []

    def session_active(self) -> bool:
        return self.active

    def run(self, command, input=None):
        self.commands.append(command)
        return self.handler(command)

    def for_endpoint(self, host, port):
        moved = FakeTransport(self.handler, capabilities=self.capabilities, active=self.active)
        moved.target = host
        return moved


USER_OWNED = LOCAL_OPENSSH_CAPABILITIES


def _config(**overrides: Any) -> SlurmConfig:
    base = SlurmConfig(
        ssh_host="fake-login", account="acct", work_root="/nest/jobs", worker_workdir="/nest"
    )
    return replace(base, **overrides)


# The contract runs only through its concrete subclasses, which are TestCases.
_ContractBase = unittest.TestCase if TYPE_CHECKING else object


class _FakeHpcContract(_ContractBase):
    """Operations every transport must perform identically against a login node."""

    def make_backend(self, **config) -> SlurmBackend:
        raise NotImplementedError

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        # No worker container runs here, so no fake NVIDIA driver (or C compiler)
        # is needed; the Controller image has none.
        self.hpc = FakeHpc(root, nvidia_driver=False)
        settings = self.hpc.controller_settings()
        patcher = mock.patch.dict(
            os.environ, {k: v for k, v in settings.items() if k.startswith(("PATH", "FAKE_"))}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.hpc.base / "contract.sbatch").write_text(CONTRACT_SCRIPT)
        self.known_hosts = root / "known_hosts"
        self.known_hosts.write_text("fake-hpc ssh-ed25519 AAAA\n")
        self.backend = self.make_backend()

    def submit(self, backend=None) -> int:
        return (backend or self.backend).submit(str(self.hpc.base), "contract.sbatch", gpu=0)

    def test_submit_then_status_logs_and_accounting(self):
        job_id = self.submit()
        self.assertEqual(self.backend.status(job_id), "COMPLETED")
        self.assertEqual(self.backend.read_log(job_id), b"contract-output\n")
        self.assertEqual(self.backend.read_log(job_id, limit=7), b"output\n")
        accounting = self.backend.accounting(job_id)
        assert accounting is not None
        self.assertEqual(accounting.job_id, job_id)
        self.assertEqual(accounting.state, "COMPLETED")
        self.assertEqual(accounting.account, "acct")
        self.assertEqual(accounting.exit_code, "0:0")
        self.assertEqual(accounting.node_list, "fake-node1")

    def test_missing_log_and_accounting_are_none_not_empty(self):
        self.assertIsNone(self.backend.read_log(424242))
        self.assertIsNone(self.backend.accounting(424242))

    def test_result_is_downloaded(self):
        job_id = self.submit()
        job_dir = self.backend.stage_payload(b"payload", "a" * 32)
        self.backend._ssh(f"cat > {job_dir}/output.pkl", input=b"result-bytes")
        self.assertEqual(self.backend.wait_result(job_id, job_dir, timeout=1), b"result-bytes")

    def test_cancel_a_pending_job(self):
        self.hpc.hold_jobs("PENDING", "Priority")
        job_id = self.submit()
        self.assertEqual(self.backend.status(job_id), "PENDING")
        self.backend.cancel(job_id)
        self.assertTrue(self.backend.status(job_id).startswith("CANCELLED"))
        self.assertEqual([c["job_id"] for c in self.hpc.calls("scancel")], [str(job_id)])

    def test_failed_remote_command_is_a_remote_command_error(self):
        with self.assertRaises(RemoteCommandError):
            self.backend.cancel(999999)

    def test_unreachable_login_node_is_an_ssh_error(self):
        backend = self.make_backend(ssh_host="unknown-host")
        with self.assertRaises(SSHError):
            backend.status(1)

    def test_sbatch_timeout_is_unknown_and_never_resubmitted(self):
        backend = self.make_backend(ssh_timeout=1)
        self.hpc.hang_ssh("sbatch --parsable", after=True)
        with self.assertRaises(SubmissionUnknownError):
            self.submit(backend)
        self.assertEqual(len(self.hpc.calls("sbatch")), 1)

    def test_prepare_reads_the_runtime(self):
        self.assertRegex(self.backend.runtime_id(), r"^[0-9a-f]{64}$")


class ControllerSSHContractTests(_FakeHpcContract, unittest.TestCase):
    """The Controller's own SSH, as the Controller builds it today (Mode A)."""

    def make_backend(self, **config) -> SlurmBackend:
        base = _config(
            ssh_host=FakeHpc.host,
            work_root=f"{self.hpc.base}/jobs",
            worker_workdir=str(self.hpc.base),
        )
        return SlurmBackend(replace(base, **config))

    def test_default_transport_is_the_controllers_noninteractive_ssh(self):
        self.assertIsInstance(self.backend.transport, ControllerSSHTransport)
        self.assertEqual(self.backend.capabilities, CONTROLLER_SSH_CAPABILITIES)
        self.backend.status(1)
        argv = self.hpc.calls("ssh")[-1]["argv"]
        self.assertEqual(argv[:3], ["-o", "BatchMode=yes", FakeHpc.host])


class LocalOpenSSHContractTests(_FakeHpcContract, unittest.TestCase):
    """The user's local OpenSSH on the client (for future Mode B), with a terminal."""

    def make_backend(self, **config) -> SlurmBackend:
        timeout = config.pop("ssh_timeout", 30)
        host = config.pop("ssh_host", FakeHpc.host)
        transport = LocalOpenSSHTransport(
            host,
            "alice",
            str(self.known_hosts),
            timeout=timeout,
            login_timeout=timeout,
            control_dir=tempfile.mkdtemp(),
            has_terminal=lambda: True,
        )
        config = _config(
            ssh_host=FakeHpc.host,
            work_root=f"{self.hpc.base}/jobs",
            worker_workdir=str(self.hpc.base),
            **config,
        )
        return SlurmBackend(config, transport)

    def test_commands_run_as_the_user_with_pinned_host_keys_and_no_key(self):
        self.backend.status(1)
        argv = self.hpc.calls("ssh")[-1]["argv"]
        options = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
        self.assertEqual(argv[:2], ["-F", "none"])
        self.assertIn("StrictHostKeyChecking=yes", options)
        self.assertIn(f"UserKnownHostsFile={self.known_hosts}", options)
        self.assertIn("ForwardAgent=no", options)
        self.assertIn("ControlMaster=auto", options)
        self.assertNotIn("BatchMode=yes", options)  # OpenSSH may prompt for password/MFA
        self.assertNotIn("-i", argv)
        self.assertEqual(argv[argv.index("-l") + 1], "alice")
        self.assertEqual(self.backend.capabilities.execution_authority, "user_owned")


class OfflineLimitationTests(unittest.TestCase):
    """A user-owned session that is not active refuses operations before any command."""

    def setUp(self):
        self.transport = FakeTransport(
            lambda command: CommandResult(0, b"RUNNING|None|gpu|normal|acct\n"),
            capabilities=USER_OWNED,
            active=False,
        )
        self.backend = SlurmBackend(_config(), self.transport)

    def assert_refused(self, operation, call, job_id=None):
        with self.assertRaises(SessionUnavailableError) as caught:
            call()
        error = caught.exception
        self.assertEqual(error.operation, operation)
        self.assertTrue(error.interactive_required)
        self.assertEqual(error.job_id, job_id)
        self.assertIn("does not use the Controller's SSH", str(error))
        self.assertEqual(self.transport.commands, [])

    def test_every_operation_is_refused_without_the_session(self):
        backend = self.backend
        self.assert_refused("status", lambda: backend.status(7), 7)
        self.assert_refused("cancel", lambda: backend.cancel(7), 7)
        self.assert_refused("logs", lambda: backend.read_log(7), 7)
        self.assert_refused("accounting", lambda: backend.accounting(7), 7)
        self.assert_refused("result", lambda: backend.wait_result(7, "/nest/jobs/x"), 7)
        self.assert_refused("submit", lambda: backend.submit("/nest", "job.sbatch"))
        self.assert_refused("submit", lambda: backend.submit_payload(b"p", execution_id="b" * 32))
        self.assert_refused(
            "prepare",
            lambda: backend.prepare_environment_bytes(pyproject_toml=b"x", uv_lock=b"y"),
        )

    def test_an_active_session_runs_the_operation(self):
        self.transport.active = True
        self.assertEqual(self.backend.status(7), "RUNNING")
        self.assertEqual(len(self.transport.commands), 1)

    def test_offline_capability_skips_the_session_check(self):
        queries_only = TransportCapabilities(
            execution_authority=ExecutionAuthority.USER_OWNED,
            interactive_required=True,
            can_query_offline=True,
            can_cancel_offline=False,
            can_submit_offline=False,
        )
        self.transport.capabilities = queries_only
        self.assertEqual(self.backend.status(7), "RUNNING")
        with self.assertRaises(SessionUnavailableError):
            self.backend.cancel(7)

    def test_capabilities_are_advertised(self):
        self.assertEqual(self.backend.capabilities, USER_OWNED)
        for operation in Operation:
            self.assertFalse(USER_OWNED.allows_offline(operation))
            self.assertTrue(CONTROLLER_SSH_CAPABILITIES.allows_offline(operation))

    def test_session_unavailable_is_a_known_error_kind(self):
        self.assertIs(INFRASTRUCTURE_ERRORS["session_unavailable"], SessionUnavailableError)

    def test_headless_local_openssh_fails_clearly_without_running_ssh(self):
        transport = LocalOpenSSHTransport(
            "login.example.edu", "alice", "/etc/nest/known_hosts", has_terminal=lambda: False
        )
        backend = SlurmBackend(_config(ssh_host="login.example.edu"), transport)
        with (
            mock.patch("nest.backends.transport.subprocess.run") as run,
            self.assertRaises(SessionUnavailableError) as caught,
        ):
            backend.status(3)
        run.assert_not_called()
        self.assertIn("interactive terminal", str(caught.exception))


class ErrorClassificationTests(unittest.TestCase):
    """The core classifies a transport's outcomes the same way for every transport."""

    def backend(self, handler, **kwargs):
        return SlurmBackend(_config(), FakeTransport(handler, **kwargs))

    def test_exit_255_is_an_ssh_error_and_others_are_command_errors(self):
        with self.assertRaises(SSHError):
            self.backend(lambda c: CommandResult(255, stderr=b"denied")).cancel(1)
        with self.assertRaises(RemoteCommandError) as caught:
            self.backend(lambda c: CommandResult(1, stderr=b"bad job")).cancel(1)
        self.assertEqual(caught.exception.returncode, 1)

    def test_submission_timeout_is_unknown(self):
        def hang(command):
            raise SSHTimeoutError("timed out")

        with self.assertRaises(SubmissionUnknownError):
            self.backend(hang).submit("/nest", "job.sbatch")

    def test_disconnect_after_sbatch_started_is_unknown(self):
        def drop(command):
            return CommandResult(255, b"nest-sbatch-started\n", b"closed")

        with self.assertRaises(SubmissionUnknownError):
            self.backend(drop).submit("/nest", "job.sbatch")

    def test_disconnect_before_sbatch_is_an_ssh_error(self):
        with self.assertRaises(SSHError):
            self.backend(lambda c: CommandResult(255, stderr=b"no route")).submit("/n", "j")

    def test_submission_returns_the_parsed_job_id(self):
        backend = self.backend(lambda c: CommandResult(0, b"nest-sbatch-started\n42;cluster\n"))
        self.assertEqual(backend.submit("/nest", "job.sbatch"), 42)
        command = backend.transport.commands[0]
        self.assertIn("--account=acct", command)

    def test_accounting_keeps_unset_fields_none(self):
        backend = self.backend(lambda c: CommandResult(0, b"5|PENDING|0:0|00:00:00|acct||None|\n"))
        self.assertEqual(
            backend.accounting(5),
            SlurmAccounting(
                job_id=5, state="PENDING", exit_code="0:0", elapsed="00:00:00", account="acct"
            ),
        )

    def test_read_log_rejects_unknown_jobs_and_limits(self):
        backend = self.backend(lambda c: CommandResult(0))
        with self.assertRaises(ValueError):
            backend.read_log(1, job="other")
        with self.assertRaises(ValueError):
            backend.read_log(1, limit=0)


class CredentialIsolationTests(unittest.TestCase):
    """A user-owned transport never reaches Controller (Mode A) SSH credentials."""

    def local(self, **kwargs):
        return LocalOpenSSHTransport("login.example.edu", "alice", "/pins", **kwargs)

    def test_user_owned_transport_refuses_controller_credentials(self):
        managed = {
            "ssh_mode": "B",
            "ssh_user": "svc-nest",
            "identity_file": "/run/nest/id_ed25519",
            "known_hosts_file": "/run/nest/known_hosts",
        }
        for overrides in (managed, {"identity_file": "/run/nest/id"}, {"known_hosts_file": "/k"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                SlurmBackend(_config(**overrides), self.local())

    def test_controller_transport_still_accepts_managed_credentials(self):
        config = _config(
            ssh_mode="B",
            ssh_user="svc-nest",
            identity_file="/run/nest/id",
            known_hosts_file="/run/nest/known_hosts",
        )
        backend = SlurmBackend(config)
        transport = backend.transport
        assert isinstance(transport, ControllerSSHTransport)
        self.assertIn("/run/nest/id", transport.ssh_argv())

    def test_local_openssh_requires_pinned_host_keys_and_a_safe_user(self):
        with self.assertRaises(ValueError):
            LocalOpenSSHTransport("login", "alice", "")
        for user in ("", "-oProxyCommand=x", "a b"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                LocalOpenSSHTransport("login", user, "/pins")

    def test_local_openssh_environment_drops_nest_secrets(self):
        transport = self.local(has_terminal=lambda: True, control_dir=tempfile.mkdtemp())
        with (
            mock.patch.dict(os.environ, {"NEST_DATABASE_PASSWORD": "s3cret"}),
            mock.patch(
                "nest.backends.transport.subprocess.run",
                return_value=mock.Mock(returncode=0, stdout=b"", stderr=b""),
            ) as run,
        ):
            transport.run("true")
        env = run.call_args.kwargs["env"]
        self.assertNotIn("NEST_DATABASE_PASSWORD", env)

    def test_for_endpoint_keeps_the_transport_and_identity(self):
        backend = SlurmBackend(
            _config(ssh_host="login1", allowed_hosts=("login1", "login2")),
            LocalOpenSSHTransport("login1", "alice", "/pins", has_terminal=lambda: True),
        )
        moved = backend.for_endpoint("login2", 2222)
        transport = moved.transport
        assert isinstance(transport, LocalOpenSSHTransport)
        self.assertEqual(
            (transport.target, transport.port, transport.user), ("login2", 2222, "alice")
        )
        with self.assertRaises(ValueError):
            backend.for_endpoint("elsewhere", None)


MACOS_TMPDIR = "/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T"
"""A typical per-user macOS `$TMPDIR`, about 49 bytes long."""


def _control_path(transport: LocalOpenSSHTransport) -> str:
    option = next(a for a in transport.ssh_argv() if a.startswith("ControlPath="))
    return option.split("=", 1)[1]


class LocalSessionLifecycleTests(unittest.TestCase):
    """The control socket fits every platform, prompts reach the user, and runs clean up."""

    def local(self, **kwargs):
        transport = LocalOpenSSHTransport("login.example.edu", "alice", "/pins", **kwargs)
        self.addCleanup(transport.close)
        return transport

    def test_default_control_path_fits_macos_with_a_long_tmpdir(self):
        with (
            mock.patch.dict(os.environ, {"TMPDIR": MACOS_TMPDIR}),
            mock.patch.object(tempfile, "tempdir", None),
        ):
            transport = self.local()
            path = _control_path(transport)
        self.assertTrue(transport.control_dir.startswith("/tmp/nest-"))
        expanded = path.replace("%C", "c" * 40) + "." + "r" * 16
        self.assertLessEqual(len(expanded), 103)  # macOS sun_path, without its NUL
        self.assertEqual(os.stat(transport.control_dir).st_mode & 0o777, 0o700)

    def test_a_control_dir_too_long_for_a_socket_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            LocalOpenSSHTransport("login", "alice", "/pins", control_dir="/" + "d" * 70)
        self.assertIn("too long for a Unix socket", str(caught.exception))

    @unittest.skipUnless(shutil.which("ssh"), "requires OpenSSH")
    def test_openssh_resolves_the_default_control_path(self):
        transport = self.local()
        argv = transport.ssh_argv()
        resolved = subprocess.run(
            [argv[0], "-G", *argv[1:]], capture_output=True, text=True, check=True
        ).stdout
        line = next(l for l in resolved.splitlines() if l.startswith("controlpath "))
        self.assertTrue(line.split(" ", 1)[1].startswith(transport.control_dir + "/"))
        self.assertLessEqual(len(line.split(" ", 1)[1]) + 17, 103)

    def calls_with(self, connected: bool, login=None):
        transport = self.local(has_terminal=lambda: True, timeout=7, login_timeout=99)
        login = login or mock.Mock(returncode=0)
        command = mock.Mock(returncode=0, stdout=b"ok", stderr=b"")

        def run(argv, **kwargs):
            return login if argv[-1] == "true" else command

        with (
            mock.patch.object(transport, "connected", return_value=connected),
            mock.patch("nest.backends.transport.subprocess.run", side_effect=run) as patched,
        ):
            self.assertEqual(transport.check("squeue --me"), b"ok")
        return [(call.args[0][-1], call.kwargs) for call in patched.call_args_list]

    def test_login_shows_openssh_messages_then_commands_capture_output(self):
        (login, login_kwargs), (command, kwargs) = self.calls_with(connected=False)
        self.assertEqual(login, "true")
        self.assertIsNone(login_kwargs["stderr"])  # banner and MFA instructions are shown
        self.assertEqual(login_kwargs["timeout"], 99)
        self.assertEqual(command, "squeue --me")
        self.assertTrue(kwargs["capture_output"])  # remote errors stay classifiable
        self.assertEqual(kwargs["timeout"], 7)

    def test_an_open_connection_is_reused_without_logging_in(self):
        self.assertEqual([c for c, _ in self.calls_with(connected=True)], ["squeue --me"])

    def test_a_failed_or_slow_login_is_an_ssh_error_before_any_command(self):
        for outcome in ([mock.Mock(returncode=255)], subprocess.TimeoutExpired(["ssh"], 99)):
            transport = self.local(has_terminal=lambda: True)
            backend = SlurmBackend(_config(ssh_host="login.example.edu"), transport)
            with (
                self.subTest(outcome=outcome),
                mock.patch.object(transport, "connected", return_value=False),
                mock.patch("nest.backends.transport.subprocess.run", side_effect=outcome) as run,
                self.assertRaises(SSHError) as caught,
            ):
                backend.submit("/nest", "job.sbatch")  # definitely not submitted
            self.assertNotIsInstance(caught.exception, SubmissionUnknownError)
            self.assertIn("no remote command ran", str(caught.exception).lower())
            self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["true"])

    def test_close_ends_the_connection_and_removes_its_own_directory(self):
        with (
            mock.patch(
                "nest.backends.transport.subprocess.run",
                return_value=mock.Mock(returncode=0, stdout=b"", stderr=b""),
            ) as run,
            self.local() as transport,
        ):
            directory = transport.control_dir
            self.assertTrue(os.path.isdir(directory))
        self.assertFalse(os.path.exists(directory))
        self.assertEqual(run.call_args.args[0][-3:], ["-O", "exit", "login.example.edu"])

    def test_close_keeps_a_callers_directory_and_ignores_ssh_failures(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        transport = self.local(control_dir=directory)
        with mock.patch(
            "nest.backends.transport.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ssh"], 1),
        ):
            transport.close()
        self.assertTrue(os.path.isdir(directory))

    def test_close_without_any_command_runs_nothing(self):
        with mock.patch("nest.backends.transport.subprocess.run") as run:
            self.local().close()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
