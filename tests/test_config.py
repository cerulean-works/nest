"""Tests for the environment-variable configuration contract (`nest.config`).

These are `unittest` cases so they also run inside the Controller image.
"""

import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent import futures
from pathlib import Path
from typing import cast
from unittest import mock

import grpc

from nest import _logging, healthcheck
from nest.backends.slurm import SlurmConfig
from nest.client import _channel
from nest.config import (
    DEFAULT_CONTROLLER_ADDRESS,
    ConfigError,
    LoginNode,
    Secret,
    load_client_config,
    load_controller_config,
    load_executor,
    materialize_ssh_credentials,
    redact_url,
)
from nest.controller import ExecutionService, _add_port, build_backend
from nest.proto import execution_pb2, execution_pb2_grpc

PRIVATE_KEY = (
    "-----BEGIN OPENSSH "
    "PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAAc2VjcmV0LW1hcmtlci1ub3QtYS1yZWFsLWtleQ\n"
    "-----END OPENSSH "
    "PRIVATE KEY-----"
)
"""A placeholder with the PEM framing the validator expects; not a real key. The
framing is split so the repository credential scan does not match it."""

TAILSCALE_SETTINGS = {
    "NEST_TAILSCALE_AUTHKEY_FILE": "/run/secrets/tailscale_authkey",
    "NEST_TAILSCALE_HOSTNAME": "nest-controller",
    "NEST_TAILSCALE_IMAGE": "tailscale/tailscale:v1.104.4",
    "NEST_TAILSCALE_SERVE_DIR": "./docker/tailscale-serve",
    "TS_AUTHKEY": "file:/run/secrets/tailscale_authkey",
    "ALL_PROXY": "socks5h://tailscale:1055",
}


class Files:
    """Creates private Mode B credential files in a temporary directory."""

    def __init__(self, test: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.root)
        self.known_hosts = self.write("known_hosts", "login1.example.edu ssh-ed25519 AAAA\n")
        self.identity = self.write("id_ed25519", PRIVATE_KEY + "\n")

    def write(self, name: str, text: str, mode: int = 0o600) -> str:
        path = self.root / name
        path.write_text(text)
        path.chmod(mode)
        return str(path)


def managed(files: Files, **extra: str) -> dict[str, str]:
    """Settings for one Mode B Provider Profile with file credentials."""
    return {
        "NEST_EXECUTOR": "slurm",
        "NEST_PROVIDERS": "campus",
        "NEST_PROVIDER_CAMPUS_SSH_MODE": "B",
        "NEST_PROVIDER_CAMPUS_LOGIN_NODES": "login1.example.edu, login2.example.edu:2222",
        "NEST_PROVIDER_CAMPUS_SSH_USER": "svc-nest",
        "NEST_PROVIDER_CAMPUS_ACCOUNT": "physics",
        "NEST_PROVIDER_CAMPUS_PARTITION": "gpu",
        "NEST_PROVIDER_CAMPUS_TIME": "1-00:00:00",
        "NEST_PROVIDER_CAMPUS_WORKDIR": "/scratch/nest",
        "NEST_SSH_KNOWN_HOSTS": files.known_hosts,
        "NEST_SSH_IDENTITY_FILE": files.identity,
        **extra,
    }


LEGACY = {
    "NEST_EXECUTOR": "slurm",
    "NEST_SLURM_HOST": "hpc",
    "NEST_SLURM_ACCOUNT": "acct",
    "NEST_SLURM_WORKDIR": "/home/u/nest",
}


class DefaultsTests(unittest.TestCase):
    def test_empty_environment_is_a_local_controller(self):
        config = load_controller_config({})
        self.assertEqual(config.bind, "127.0.0.1:50051")
        self.assertEqual(config.executor, "local")
        self.assertEqual(config.log_level, "INFO")
        self.assertEqual((config.ssh_timeout, config.result_timeout), (60, 600))
        self.assertEqual((config.environment_timeout, config.poll_interval), (1800, 5))
        self.assertEqual(config.providers, {})
        self.assertIsNone(config.tls)

    def test_empty_values_mean_unset(self):
        settings = dict.fromkeys(
            ["NEST_EXECUTOR", "NEST_SLURM_HOST", "NEST_SSH_TIMEOUT", "NEST_LOG_LEVEL"], " "
        )
        self.assertEqual(load_controller_config(settings), load_controller_config({}))

    def test_client_defaults_to_localhost_not_a_tailnet(self):
        config = load_client_config({})
        self.assertEqual(config.address, DEFAULT_CONTROLLER_ADDRESS)
        self.assertEqual(config.address, "127.0.0.1:50051")
        self.assertNotIn("ts.net", config.address)
        self.assertFalse(config.tls)
        self.assertEqual(config.rpc_timeout, 900)


class ValidationTests(unittest.TestCase):
    def problems(self, settings):
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(settings)
        self.assertIsInstance(caught.exception, ValueError)
        return "\n".join(caught.exception.problems)

    def test_every_problem_is_reported_at_once(self):
        text = self.problems(
            {
                "NEST_EXECUTOR": "slurm",
                "NEST_SSH_TIMEOUT": "soon",
                "NEST_CONTROLLER_BIND": "nowhere",
                "NEST_LOG_LEVEL": "LOUD",
            }
        )
        for name in (
            "NEST_SLURM_HOST",
            "NEST_SLURM_ACCOUNT",
            "NEST_SLURM_WORKDIR",
            "NEST_SSH_TIMEOUT",
            "NEST_CONTROLLER_BIND",
            "NEST_LOG_LEVEL",
        ):
            self.assertIn(name, text)
        self.assertIn("NEST_PROVIDERS", text, "the error points at the alternative")

    def test_executor_must_be_known(self):
        self.assertIn("NEST_EXECUTOR", self.problems({"NEST_EXECUTOR": "Slurm"}))
        with self.assertRaises(ConfigError):
            load_executor({"NEST_EXECUTOR": "remote"})
        self.assertEqual(load_executor({}), "local")

    def test_legacy_settings_become_a_mode_a_profile(self):
        config = load_controller_config(
            {**LEGACY, "NEST_SLURM_PARTITION": "short", "NEST_SLURM_TIME": "30"}
        )
        profile = config.provider
        assert profile is not None
        self.assertEqual(profile.id, "default")
        self.assertEqual(profile.ssh_mode, "A")
        self.assertEqual(profile.login_nodes, (LoginNode("hpc"),))
        self.assertEqual((profile.partition, profile.time), ("short", "30"))

    def test_single_profile_can_use_managed_ssh_with_slurm_prefix(self):
        files = Files(self)
        config = load_controller_config(
            {
                **LEGACY,
                "NEST_SLURM_HOST": "",
                "NEST_SLURM_LOGIN_NODES": "login1.example.edu:2200",
                "NEST_SLURM_SSH_MODE": "B",
                "NEST_SLURM_SSH_USER": "svc",
                "NEST_SSH_KNOWN_HOSTS": files.known_hosts,
                "NEST_SSH_IDENTITY_FILE": files.identity,
            }
        )
        profile = config.provider
        assert profile is not None
        self.assertEqual((profile.id, profile.ssh_mode, profile.ssh_user), ("default", "B", "svc"))
        self.assertEqual(profile.login_nodes, (LoginNode("login1.example.edu", 2200),))
        text = self.problems({**LEGACY, "NEST_SLURM_LOGIN_NODES": "login1"})
        self.assertIn("only one of NEST_SLURM_HOST and NEST_SLURM_LOGIN_NODES", text)
        self.assertIn("NEST_SLURM_SSH_USER", self.problems({**LEGACY, "NEST_SLURM_SSH_MODE": "B"}))

    def test_partition_and_time_keep_their_old_defaults(self):
        profile = load_controller_config(LEGACY).provider
        assert profile is not None
        self.assertEqual((profile.partition, profile.time), ("dev", "00:05:00"))

    def test_slurm_names_and_times_are_validated(self):
        text = self.problems(
            {**LEGACY, "NEST_SLURM_TIME": "five minutes", "NEST_SLURM_PARTITION": "a b"}
        )
        self.assertIn("NEST_SLURM_TIME", text)
        self.assertIn("NEST_SLURM_PARTITION", text)
        self.assertIn("absolute", self.problems({**LEGACY, "NEST_SLURM_WORKDIR": "nest"}))

    def test_local_mode_keeps_a_partial_legacy_backend(self):
        # Job status RPCs work in local mode with only a host and account.
        config = load_controller_config({"NEST_SLURM_HOST": "hpc", "NEST_SLURM_ACCOUNT": "a"})
        assert config.provider is not None
        self.assertIsNone(config.provider.workdir)
        self.assertEqual(load_controller_config({"NEST_SLURM_HOST": "hpc"}).providers, {})

    def test_profiles_and_legacy_settings_are_exclusive(self):
        files = Files(self)
        text = self.problems({**managed(files), "NEST_SLURM_HOST": "hpc"})
        self.assertIn("unset NEST_SLURM_HOST", text)

    def test_profile_ids_and_default_are_checked(self):
        files = Files(self)
        self.assertIn("invalid ID", self.problems(managed(files, NEST_PROVIDERS="Campus!")))
        self.assertIn("twice", self.problems(managed(files, NEST_PROVIDERS="campus,campus")))
        text = self.problems(managed(files, NEST_DEFAULT_PROVIDER="other"))
        self.assertIn("NEST_DEFAULT_PROVIDER", text)
        self.assertIn("needs NEST_PROVIDERS", self.problems({"NEST_DEFAULT_PROVIDER": "x"}))

    def test_login_node_allowlist_is_parsed_and_validated(self):
        files = Files(self)
        profile = load_controller_config(managed(files)).provider
        assert profile is not None
        self.assertEqual(
            profile.login_nodes,
            (LoginNode("login1.example.edu"), LoginNode("login2.example.edu", 2222)),
        )
        for bad in ("", "-oProxyCommand=x", "login:0", "login:ssh", "a b", ","):
            with self.subTest(bad=bad):
                text = self.problems(managed(files, NEST_PROVIDER_CAMPUS_LOGIN_NODES=bad))
                self.assertIn("NEST_PROVIDER_CAMPUS_LOGIN_NODES", text)

    def test_mode_b_requires_user_host_keys_and_key(self):
        files = Files(self)
        settings = managed(files)
        for name in (
            "NEST_PROVIDER_CAMPUS_SSH_USER",
            "NEST_SSH_KNOWN_HOSTS",
            "NEST_SSH_IDENTITY_FILE",
        ):
            with self.subTest(missing=name):
                text = self.problems({**settings, name: ""})
                self.assertIn(name, text)

    def test_credential_files_must_exist_and_be_private(self):
        files = Files(self)
        text = self.problems(managed(files, NEST_SSH_KNOWN_HOSTS="/does/not/exist"))
        self.assertIn("not a readable file", text)
        self.assertIn("absolute", self.problems(managed(files, NEST_SSH_KNOWN_HOSTS="kh")))
        loose = files.write("loose_key", PRIVATE_KEY, mode=0o644)
        self.assertIn("chmod 600", self.problems(managed(files, NEST_SSH_IDENTITY_FILE=loose)))

    def test_ssh_mode_policy_limits_profiles(self):
        files = Files(self)
        text = self.problems({**LEGACY, "NEST_SSH_MODES_ALLOWED": "B"})
        self.assertIn("does not allow", text)
        load_controller_config(managed(files, NEST_SSH_MODES_ALLOWED="b"))
        self.assertIn("A and/or B", self.problems({"NEST_SSH_MODES_ALLOWED": "C"}))

    def test_tls_needs_both_files(self):
        files = Files(self)
        self.assertIn("both", self.problems({"NEST_TLS_CERT_FILE": files.known_hosts}))
        config = load_controller_config(
            {"NEST_TLS_CERT_FILE": files.known_hosts, "NEST_TLS_KEY_FILE": files.identity}
        )
        self.assertIsNotNone(config.tls)

    def test_client_tls_settings(self):
        files = Files(self)
        config = load_client_config({"NEST_CONTROLLER_TLS_CA_FILE": files.known_hosts})
        self.assertTrue(config.tls)
        self.assertTrue(load_client_config({"NEST_CONTROLLER_TLS": "yes"}).tls)
        for bad in ({"NEST_CONTROLLER_TLS": "maybe"}, {"NEST_CONTROLLER_ADDRESS": "host"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_client_config(bad)

    def test_public_url_is_validated(self):
        load_controller_config({"NEST_PUBLIC_URL": "grpcs://nest.example.edu:443"})
        self.assertIn("NEST_PUBLIC_URL", self.problems({"NEST_PUBLIC_URL": "nest.example.edu"}))
        self.assertIn(
            "credentials", self.problems({"NEST_PUBLIC_URL": "https://u:p@nest.example.edu"})
        )

    def test_reserved_settings_fail_instead_of_being_ignored(self):
        for name, value in (
            ("NEST_DATABASE_URL", "postgresql://db/nest"),
            ("NEST_AUTH_TOKEN_FILE", "/run/secrets/token"),
            ("NEST_RETENTION_DAYS", "30"),
            ("NEST_INTEGRATIONS", "slack"),
        ):
            with self.subTest(name=name):
                text = self.problems({name: value})
                self.assertIn(f"{name} is reserved", text)


class RedactionTests(unittest.TestCase):
    def test_database_password_never_appears_in_errors(self):
        url = "postgresql://nest:hunter2-secret@db.internal/nest?sslpassword=pw-secret"
        with self.assertRaises(ConfigError) as caught:
            load_controller_config({"NEST_DATABASE_URL": url})
        self.assertNotIn("hunter2-secret", str(caught.exception))
        self.assertNotIn("pw-secret", str(caught.exception))
        self.assertEqual(redact_url(url), "postgresql://nest:***@db.internal/nest?***")
        self.assertEqual(redact_url("postgresql://db/nest"), "postgresql://db/nest")

    def test_inline_private_key_never_appears_in_repr_or_summary(self):
        files = Files(self)
        settings = managed(files, NEST_SSH_IDENTITY_FILE="", NEST_SSH_PRIVATE_KEY=PRIVATE_KEY)
        config = load_controller_config(settings)
        assert config.ssh.private_key is not None
        self.assertEqual(config.ssh.private_key.reveal().strip(), PRIVATE_KEY)
        for text in (repr(config), str(config.summary()), str(config.ssh.private_key)):
            self.assertNotIn("c2VjcmV0", text)
        self.assertEqual(config.summary()["ssh_private_key"], "***")
        self.assertEqual(repr(Secret("x")), "Secret('***')")

    def test_invalid_private_key_is_rejected_without_echoing_it(self):
        files = Files(self)
        settings = managed(files, NEST_SSH_IDENTITY_FILE="", NEST_SSH_PRIVATE_KEY="tok-123456")
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(settings)
        self.assertNotIn("tok-123456", str(caught.exception))

    def test_registered_secrets_are_masked_by_the_log_handler(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(_logging._RedactingFormatter("%(message)s"))
        log = logging.getLogger("nest.test_redaction")
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        _logging.register_secret(PRIVATE_KEY)
        try:
            raise RuntimeError("leaked " + PRIVATE_KEY.splitlines()[1])
        except RuntimeError:
            log.exception("failure")
        self.assertNotIn("c2VjcmV0", stream.getvalue())
        self.assertIn("leaked ***", stream.getvalue())

    def test_controller_exits_with_all_problems_and_no_secrets(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("NEST_SLURM", "NEST_PROV"))}
        env.update(
            NEST_EXECUTOR="slurm",
            NEST_DATABASE_URL="postgresql://nest:hunter2-secret@db/nest",
            NEST_SSH_TIMEOUT="0",
        )
        result = subprocess.run(
            [sys.executable, "-m", "nest.controller"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        for name in ("NEST_SLURM_HOST", "NEST_SSH_TIMEOUT", "NEST_DATABASE_URL"):
            self.assertIn(name, result.stderr)
        self.assertNotIn("hunter2-secret", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class ModeSeparationTests(unittest.TestCase):
    """The optional Mode A Tailscale setup never becomes a Mode B prerequisite."""

    def test_mode_b_ignores_tailscale_settings(self):
        files = Files(self)
        without = load_controller_config(managed(files))
        with_tailscale = load_controller_config({**managed(files), **TAILSCALE_SETTINGS})
        self.assertEqual(without, with_tailscale)

    def test_mode_a_works_without_tailscale(self):
        config = load_controller_config(LEGACY)
        self.assertEqual(config, load_controller_config({**LEGACY, **TAILSCALE_SETTINGS}))

    def test_client_never_reads_tailscale_settings(self):
        self.assertEqual(load_client_config(TAILSCALE_SETTINGS), load_client_config({}))

    def test_mode_b_command_line_ignores_ssh_config_and_proxies(self):
        files = Files(self)
        backend = build_backend(load_controller_config(managed(files)))
        assert backend is not None
        argv = backend._ssh_argv()
        self.assertEqual(argv[:3], ["ssh", "-F", "none"])
        options = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
        self.assertIn("StrictHostKeyChecking=yes", options)
        self.assertIn(f"UserKnownHostsFile={files.known_hosts}", options)
        self.assertIn(f"GlobalKnownHostsFile={files.known_hosts}", options)
        self.assertIn("IdentitiesOnly=yes", options)
        self.assertIn("IdentityAgent=none", options)
        self.assertEqual(argv[-5:], ["-p", "22", "-l", "svc-nest", "login1.example.edu"])
        self.assertIn(files.identity, argv)
        self.assertFalse(any("proxy" in a.lower() or "tailscale" in a for a in argv))

    def test_mode_a_command_line_is_unchanged(self):
        backend = build_backend(load_controller_config(LEGACY))
        assert backend is not None
        self.assertEqual(backend._ssh_argv(), ["ssh", "-o", "BatchMode=yes", "hpc"])

    def test_backend_profile_settings_reach_slurm(self):
        files = Files(self)
        backend = build_backend(load_controller_config(managed(files)))
        assert backend is not None
        config = backend.config
        self.assertEqual((config.account, config.partition), ("physics", "gpu"))
        self.assertEqual((config.time, config.worker_workdir), ("1-00:00:00", "/scratch/nest"))
        self.assertEqual(config.work_root, "/scratch/nest/jobs")

    def test_backend_refuses_hosts_outside_the_allowlist(self):
        with self.assertRaisesRegex(ValueError, "approved login node"):
            SlurmConfig(ssh_host="evil.example", account="a", allowed_hosts=("login1",))
        with self.assertRaisesRegex(ValueError, "requires"):
            SlurmConfig(ssh_host="login1", account="a", ssh_mode="B")
        with self.assertRaises(ValueError):
            SlurmConfig(ssh_host="-oProxyCommand=x", account="a")

    def test_inline_credentials_are_written_privately(self):
        files = Files(self)
        settings = managed(
            files,
            NEST_SSH_IDENTITY_FILE="",
            NEST_SSH_PRIVATE_KEY=PRIVATE_KEY.replace("\n", "\\n"),
            NEST_SSH_KNOWN_HOSTS="",
            NEST_SSH_KNOWN_HOSTS_DATA="login1 ssh-ed25519 AAAA\\nlogin2 ssh-ed25519 BBBB",
        )
        config = load_controller_config(settings)
        identity, known_hosts = materialize_ssh_credentials(config.ssh)
        assert identity is not None and known_hosts is not None
        self.addCleanup(shutil.rmtree, os.path.dirname(identity))
        self.assertEqual(os.stat(identity).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(identity)).st_mode & 0o777, 0o700)
        self.assertEqual(Path(identity).read_text(), PRIVATE_KEY + "\n")
        self.assertEqual(len(Path(known_hosts).read_text().splitlines()), 2)


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command")
class TLSTests(unittest.TestCase):
    def test_tls_controller_and_client_round_trip(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                "-keyout", str(key), "-out", str(cert),
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip
        key.chmod(0o600)
        config = load_controller_config(
            {
                "NEST_CONTROLLER_BIND": "localhost:0",
                "NEST_TLS_CERT_FILE": str(cert),
                "NEST_TLS_KEY_FILE": str(key),
            }
        )
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
            ExecutionService(executor="local"), server
        )
        port = _add_port(server, config)
        server.start()
        self.addCleanup(server.stop, 0)

        client = load_client_config(
            {
                "NEST_CONTROLLER_ADDRESS": f"localhost:{port}",
                "NEST_CONTROLLER_TLS_CA_FILE": str(cert),
            }
        )
        with _channel(client) as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)
            with self.assertRaises(grpc.RpcError) as caught:
                stub.Execute(execution_pb2.ExecuteRequest(gpu=-1), timeout=5)
            self.assertEqual(
                cast(grpc.Call, caught.exception).code(), grpc.StatusCode.INVALID_ARGUMENT
            )

        tls_env = {"NEST_CONTROLLER_BIND": f"0.0.0.0:{port}", "NEST_TLS_CERT_FILE": str(cert)}
        with mock.patch.dict(os.environ, tls_env):
            self.assertEqual(healthcheck.main(), 0)

        plaintext = load_client_config({"NEST_CONTROLLER_ADDRESS": f"localhost:{port}"})
        with _channel(plaintext) as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)
            with self.assertRaises(grpc.RpcError) as caught:
                stub.Execute(execution_pb2.ExecuteRequest(gpu=-1), timeout=5)
            self.assertEqual(cast(grpc.Call, caught.exception).code(), grpc.StatusCode.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
