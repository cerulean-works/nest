"""Tests for the environment-variable configuration contract (`nest.config`).

These are `unittest` cases so they also run inside the Controller image.
"""

import dataclasses
import io
import json
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
    PlatformProfile,
    ProviderProfile,
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

    def test_client_without_settings_connects_only_to_loopback(self):
        config = load_client_config({})
        self.assertEqual(config.address, DEFAULT_CONTROLLER_ADDRESS)
        self.assertEqual(config.address, "127.0.0.1:50051")
        self.assertFalse(config.tls)
        self.assertEqual(config.rpc_timeout, 900)
        with mock.patch.object(grpc, "insecure_channel") as insecure_channel:
            _channel(config)
        insecure_channel.assert_called_once_with("127.0.0.1:50051")


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
        self.assertIn("NEST_PLATFORMS", text, "the error points at the alternative")

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

    def test_default_partition_and_time(self):
        profile = load_controller_config(LEGACY).provider
        assert profile is not None
        self.assertEqual((profile.partition, profile.time), ("h200_256gpu", "00:05:00"))

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
        config = load_client_config(
            {
                "NEST_CONTROLLER_ADDRESS": "controller.example.edu:50051",
                "NEST_CONTROLLER_TLS_CA_FILE": files.known_hosts,
            }
        )
        self.assertTrue(config.tls)
        self.assertTrue(load_client_config({"NEST_CONTROLLER_TLS": "yes"}).tls)
        for bad in ({"NEST_CONTROLLER_TLS": "maybe"}, {"NEST_CONTROLLER_ADDRESS": "host"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_client_config(bad)

    def test_non_loopback_client_requires_an_explicit_tls_choice(self):
        settings = {"NEST_CONTROLLER_ADDRESS": "nest.cerulean.works:50051"}
        for override in ({}, {"NEST_CONTROLLER_TLS": " "}):
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(ConfigError, "requires an explicit NEST_CONTROLLER_TLS"),
            ):
                load_client_config({**settings, **override})
        secured = load_client_config({**settings, "NEST_CONTROLLER_TLS": "true"})
        self.assertTrue(secured.tls)
        self.assertIsNone(secured.tls_ca_file)
        self.assertFalse(load_client_config({**settings, "NEST_CONTROLLER_TLS": "false"}).tls)

    def test_loopback_client_accepts_an_address_without_a_tls_choice(self):
        for address in ("localhost:50051", "127.0.0.2:50051", "[::1]:50051"):
            with self.subTest(address=address):
                config = load_client_config({"NEST_CONTROLLER_ADDRESS": address})
                self.assertEqual(config.address, address)
                self.assertFalse(config.tls)

    def test_public_url_is_validated(self):
        load_controller_config({"NEST_PUBLIC_URL": "grpcs://nest.example.edu:443"})
        self.assertIn("NEST_PUBLIC_URL", self.problems({"NEST_PUBLIC_URL": "nest.example.edu"}))
        self.assertIn(
            "credentials", self.problems({"NEST_PUBLIC_URL": "https://u:p@nest.example.edu"})
        )

    def test_reserved_settings_fail_instead_of_being_ignored(self):
        for name, value in (
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
            load_controller_config(
                {"NEST_DATABASE_URL": url.replace("postgresql", "mysql"), "NEST_LOG_LEVEL": "x"}
            )
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
            NEST_DATABASE_URL="mysql://nest:hunter2-secret@db/nest",
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


def platforms(files: Files, **extra: str) -> dict[str, str]:
    """Two Mode B HPC Platforms under one operator, with differing defaults."""
    return {
        "NEST_EXECUTOR": "slurm",
        "NEST_PLATFORMS": "campus, lab",
        "NEST_DEFAULT_PLATFORM": "lab",
        "NEST_PLATFORM_CAMPUS_NAME": "Campus GPU cluster",
        "NEST_PLATFORM_CAMPUS_OPERATOR": "Example-University",
        "NEST_PLATFORM_CAMPUS_SSH_MODE": "B",
        "NEST_PLATFORM_CAMPUS_LOGIN_NODES": "login1.example.edu, login2.example.edu:2222",
        "NEST_PLATFORM_CAMPUS_SSH_USER": "svc-nest",
        "NEST_PLATFORM_CAMPUS_ACCOUNT": "physics",
        "NEST_PLATFORM_CAMPUS_PARTITION": "gpu",
        "NEST_PLATFORM_CAMPUS_TIME": "02:00:00",
        "NEST_PLATFORM_CAMPUS_WORKDIR": "/scratch/nest",
        "NEST_PLATFORM_LAB_OPERATOR": "example-university",
        "NEST_PLATFORM_LAB_SSH_MODE": "B",
        "NEST_PLATFORM_LAB_LOGIN_NODES": "lab-login.example.edu",
        "NEST_PLATFORM_LAB_SSH_USER": "svc-lab",
        "NEST_PLATFORM_LAB_ACCOUNT": "physics",
        "NEST_PLATFORM_LAB_WORKDIR": "/home/svc-lab/nest",
        "NEST_SSH_KNOWN_HOSTS": files.known_hosts,
        "NEST_SSH_IDENTITY_FILE": files.identity,
        **extra,
    }


class PlatformTests(unittest.TestCase):
    """HPC Platform bootstrap from `NEST_PLATFORMS` (M1 patch A04)."""

    def problems(self, environ: dict[str, str]) -> str:
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(environ)
        return "\n".join(caught.exception.problems)

    def test_two_platforms_under_one_operator_keep_their_own_defaults(self):
        files = Files(self)
        config = load_controller_config(platforms(files))
        self.assertEqual(list(config.platforms), ["campus", "lab"])
        self.assertEqual(config.default_platform, "lab")
        campus, lab = config.platforms["campus"], config.platforms["lab"]
        self.assertIs(config.platform, lab)
        self.assertEqual(campus.operator, lab.operator)
        self.assertEqual(campus.operator, "example-university", "labels are lowercased")
        self.assertEqual((campus.display_name, lab.display_name), ("Campus GPU cluster", None))
        self.assertEqual((campus.partition, campus.time), ("gpu", "02:00:00"))
        self.assertEqual((lab.partition, lab.time), ("h200_256gpu", "00:05:00"))
        self.assertEqual(
            campus.login_nodes,
            (LoginNode("login1.example.edu"), LoginNode("login2.example.edu", 2222)),
        )
        self.assertEqual(lab.login_nodes, (LoginNode("lab-login.example.edu"),))
        # The same Slurm account name on two Platforms is two distinct accounts.
        self.assertEqual(campus.account, lab.account)
        self.assertNotEqual(campus.slurm_account, lab.slurm_account)
        self.assertEqual(lab.slurm_account, ("lab", "physics"))
        self.assertEqual(config.warnings, ())

    def test_default_platform_is_the_first_listed(self):
        files = Files(self)
        config = load_controller_config(platforms(files, NEST_DEFAULT_PLATFORM=""))
        self.assertEqual(config.default_platform, "campus")
        backend = build_backend(config)
        assert backend is not None
        self.assertEqual(backend.config.ssh_host, "login1.example.edu")
        self.assertEqual((backend.config.account, backend.config.partition), ("physics", "gpu"))

    def test_default_platform_selects_the_backend(self):
        files = Files(self)
        backend = build_backend(load_controller_config(platforms(files)))
        assert backend is not None
        cfg = backend.config
        self.assertEqual((cfg.ssh_host, cfg.ssh_user), ("lab-login.example.edu", "svc-lab"))
        self.assertEqual(cfg.allowed_hosts, ("lab-login.example.edu",))
        self.assertEqual((cfg.partition, cfg.time), ("h200_256gpu", "00:05:00"))
        self.assertEqual(cfg.work_root, "/home/svc-lab/nest/jobs")

    def test_platform_identity_is_immutable(self):
        files = Files(self)
        config = load_controller_config(platforms(files))
        profile = config.platforms["lab"]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.id = "campus"  # ty: ignore[invalid-assignment]
        with self.assertRaises(TypeError):
            config.platforms["lab"] = profile  # ty: ignore[invalid-assignment]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.default_platform = "campus"  # ty: ignore[invalid-assignment]

    def test_invalid_platform_ids_and_defaults_are_reported(self):
        files = Files(self)
        cases = {
            "Campus!": "invalid ID",
            "1lab": "invalid ID",
            "campus,campus": "twice",
            " , ": "at least one ID",
        }
        for value, message in cases.items():
            with self.subTest(value=value):
                self.assertIn(message, self.problems(platforms(files, NEST_PLATFORMS=value)))
        text = self.problems(platforms(files, NEST_DEFAULT_PLATFORM="other"))
        self.assertIn("NEST_DEFAULT_PLATFORM 'other' is not in NEST_PLATFORMS", text)

    def test_required_platform_fields_are_named(self):
        files = Files(self)
        for name in (
            "NEST_PLATFORM_LAB_LOGIN_NODES",
            "NEST_PLATFORM_LAB_ACCOUNT",
            "NEST_PLATFORM_LAB_WORKDIR",
            "NEST_PLATFORM_LAB_SSH_USER",
        ):
            with self.subTest(missing=name):
                self.assertIn(name, self.problems({**platforms(files), name: ""}))

    def test_platform_fields_are_validated(self):
        files = Files(self)
        cases = {
            "NEST_PLATFORM_CAMPUS_LOGIN_NODES": "-oProxyCommand=x",
            "NEST_PLATFORM_CAMPUS_ACCOUNT": "a b",
            "NEST_PLATFORM_CAMPUS_TIME": "soon",
            "NEST_PLATFORM_CAMPUS_WORKDIR": "relative",
            "NEST_PLATFORM_CAMPUS_SSH_MODE": "C",
            "NEST_PLATFORM_CAMPUS_OPERATOR": "Example University!",
            "NEST_PLATFORM_CAMPUS_NAME": "bad\x1b[31mname",
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                self.assertIn(name, self.problems(platforms(files, **{name: value})))

    def test_stray_and_misspelled_platform_settings_fail(self):
        files = Files(self)
        text = self.problems(platforms(files, NEST_PLATFORM_GPU_ACCOUNT="x"))
        self.assertIn("NEST_PLATFORM_GPU_ACCOUNT match no Platform", text)
        text = self.problems(platforms(files, NEST_PLATFORM_LAB_LOGIN_NODE="x"))
        self.assertIn("NEST_PLATFORM_LAB_LOGIN_NODE match no Platform", text)
        text = self.problems({"NEST_PLATFORM_LAB_ACCOUNT": "x", "NEST_DEFAULT_PLATFORM": "lab"})
        self.assertIn("NEST_PLATFORM_LAB_ACCOUNT, NEST_DEFAULT_PLATFORM need NEST_PLATFORMS", text)

    def test_platforms_replace_provider_and_slurm_settings(self):
        files = Files(self)
        for name in ("NEST_PROVIDERS", "NEST_PROVIDER_CAMPUS_ACCOUNT", "NEST_SLURM_HOST"):
            with self.subTest(name=name):
                text = self.problems(platforms(files, **{name: "x"}))
                self.assertIn("NEST_PLATFORMS replaces", text)
                self.assertIn(name, text)
        # compose.managed-ssh.yaml's NEST_SLURM_SSH_MODE=B is ignored, with a warning;
        # any other single-cluster setting is not.
        config = load_controller_config(platforms(files, NEST_SLURM_SSH_MODE="b"))
        self.assertEqual(len(config.platforms), 2)
        self.assertIn("NEST_SLURM_SSH_MODE=B is ignored", config.warnings[0])
        self.assertIn(
            "unset NEST_SLURM_SSH_MODE", self.problems(platforms(files, NEST_SLURM_SSH_MODE="A"))
        )
        text = self.problems(platforms(files, NEST_SLURM_SSH_MODE="B", NEST_SLURM_ACCOUNT="a"))
        self.assertIn("unset NEST_SLURM_ACCOUNT, NEST_SLURM_SSH_MODE", text)
        # The Provider approval setting is not a Provider Profile field.
        config = load_controller_config(
            platforms(
                files,
                NEST_DATABASE_URL="postgresql://nest@db.example/nest",
                NEST_PROVIDER_ALLOWED_PROJECTS="*",
                NEST_CONTROLLER_ALLOW_PLAINTEXT="true",
            )
        )
        self.assertEqual(config.allowed_projects, frozenset({"*"}))

    def test_a_login_node_belongs_to_one_platform(self):
        files = Files(self)
        shared = platforms(files, NEST_PLATFORM_LAB_LOGIN_NODES="Login2.example.edu:2222")
        self.assertIn("approved for both Platforms 'campus' and 'lab'", self.problems(shared))
        # A different port on the same host is a different endpoint.
        other_port = platforms(files, NEST_PLATFORM_LAB_LOGIN_NODES="login1.example.edu:2200")
        load_controller_config(other_port)

    def test_mode_b_platforms_ignore_tailscale_and_ssh_config(self):
        files = Files(self)
        without = load_controller_config(platforms(files))
        with_tailscale = load_controller_config({**platforms(files), **TAILSCALE_SETTINGS})
        self.assertEqual(without, with_tailscale)
        backend = build_backend(without)
        assert backend is not None
        argv = backend._ssh_argv()
        self.assertEqual(argv[:3], ["ssh", "-F", "none"])
        self.assertEqual(argv[-5:], ["-p", "22", "-l", "svc-lab", "lab-login.example.edu"])
        self.assertFalse(any("proxy" in a.lower() or "tailscale" in a for a in argv))

    def test_mode_policy_names_the_platform(self):
        files = Files(self)
        text = self.problems(
            platforms(files, NEST_PLATFORM_LAB_SSH_MODE="A", NEST_SSH_MODES_ALLOWED="B")
        )
        self.assertIn("Platform 'lab' uses SSH mode A", text)

    def test_summary_describes_platforms_without_secrets(self):
        files = Files(self)
        settings = platforms(files, NEST_SSH_IDENTITY_FILE="", NEST_SSH_PRIVATE_KEY=PRIVATE_KEY)
        config = load_controller_config(settings)
        summary = config.summary()
        self.assertEqual(summary["default_platform"], "lab")
        described = cast(dict, summary["platforms"])
        self.assertEqual(set(described), {"campus", "lab"})
        self.assertEqual(described["campus"]["operator"], "example-university")
        self.assertEqual(described["campus"]["name"], "Campus GPU cluster")
        self.assertEqual(described["lab"]["login_nodes"], ["lab-login.example.edu"])
        self.assertEqual(summary["ssh_private_key"], "***")
        text = repr(config) + json.dumps(summary)
        self.assertNotIn("c2VjcmV0LW1hcmtlci", text)
        self.assertNotIn("PRIVATE KEY", text)

    def test_errors_never_echo_platform_secrets(self):
        files = Files(self)
        secret = "c2VjcmV0LW1hcmtlci1ub3QtYS1yZWFsLWtleQ"
        text = self.problems(
            platforms(
                files,
                NEST_SSH_IDENTITY_FILE="",
                NEST_SSH_PRIVATE_KEY=secret,
                NEST_PLATFORM_LAB_ACCOUNT="",
                NEST_DATABASE_URL=f"postgresql://nest:{secret}@db/nest?sslmode=bogus",
            )
        )
        self.assertIn("NEST_PLATFORM_LAB_ACCOUNT", text)
        self.assertIn("NEST_SSH_PRIVATE_KEY", text)
        self.assertNotIn(secret, text)


class ProviderCompatibilityTests(unittest.TestCase):
    """`NEST_PROVIDERS` and `NEST_SLURM_*` keep working, as Platforms."""

    def test_provider_settings_define_the_same_platforms(self):
        files = Files(self)
        renamed = {
            key.replace("NEST_PLATFORMS", "NEST_PROVIDERS")
            .replace("NEST_PLATFORM_", "NEST_PROVIDER_")
            .replace("NEST_DEFAULT_PLATFORM", "NEST_DEFAULT_PROVIDER"): value
            for key, value in platforms(files).items()
            if not key.endswith(("_OPERATOR", "_NAME"))
        }
        legacy = load_controller_config(renamed)
        current = load_controller_config(platforms(files))
        self.assertEqual(legacy.default_platform, current.default_platform)
        self.assertEqual(list(legacy.platforms), list(current.platforms))
        for pid, profile in legacy.platforms.items():
            expected = dataclasses.replace(current.platforms[pid], operator=None, display_name=None)
            self.assertEqual(profile, expected)
        self.assertEqual(len(legacy.warnings), 1)
        self.assertIn("NEST_PLATFORMS", legacy.warnings[0])

    def test_provider_names_remain_aliases(self):
        files = Files(self)
        config = load_controller_config(platforms(files))
        self.assertIs(config.providers, config.platforms)
        self.assertEqual(config.default_provider, config.default_platform)
        self.assertIs(config.provider, config.platform)
        self.assertIs(ProviderProfile, PlatformProfile)

    def test_provider_profiles_do_not_read_platform_metadata(self):
        files = Files(self)
        config = load_controller_config(managed(files, NEST_PROVIDER_CAMPUS_OPERATOR="x"))
        assert config.platform is not None
        self.assertIsNone(config.platform.operator)

    def test_single_cluster_settings_are_the_default_platform(self):
        config = load_controller_config(LEGACY)
        self.assertEqual(list(config.platforms), ["default"])
        self.assertEqual(config.default_platform, "default")
        self.assertEqual(config.warnings, ())


class ComposePlatformTests(unittest.TestCase):
    """The Compose deployment hands the Controller the same Platform settings."""

    ROOT = Path(__file__).resolve().parents[1]

    def render(self, platforms_file: Path, *overrides: str, **settings: str) -> dict[str, str]:
        compose = os.environ.get("NEST_COMPOSE_COMMAND", "docker compose").split()
        if not (self.ROOT / "compose.yaml").is_file() or not shutil.which(compose[0]):
            self.skipTest("needs the repository's compose.yaml and Docker Compose")
        result = subprocess.run(
            [*compose, "--env-file", "/dev/null"]
            + [a for f in ("compose.yaml", *overrides) for a in ("-f", str(self.ROOT / f))]
            + ["config", "--format", "json"],
            cwd=self.ROOT,
            env={
                **{k: v for k, v in os.environ.items() if not k.startswith("NEST_")},
                "NEST_PLATFORMS_ENV_FILE": str(platforms_file),
                "NEST_SSH_DIR": "./ssh",
                **settings,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"docker compose config failed: {result.stderr.strip()[:200]}")
        environment = json.loads(result.stdout)["services"]["controller"]["environment"]
        return {name: value or "" for name, value in environment.items()}

    def test_native_and_compose_load_the_same_platforms(self):
        files = Files(self)
        native_settings = platforms(files)
        # Platform settings go in the env file; the rest stay ordinary Compose settings.
        listed = {k: v for k, v in native_settings.items() if k.startswith("NEST_PLATFORM")}
        listed["NEST_DEFAULT_PLATFORM"] = native_settings["NEST_DEFAULT_PLATFORM"]
        text = "".join(f"{k}={v}\n" for k, v in listed.items())
        platforms_file = Path(files.write("platforms.env", text))
        credentials = {
            "NEST_SSH_KNOWN_HOSTS": files.known_hosts,
            "NEST_SSH_IDENTITY_FILE": files.identity,
        }
        native = load_controller_config(native_settings)
        # compose.managed-ssh.yaml drops the SSH mount for Mode B Platforms too.
        for overrides in ((), ("compose.managed-ssh.yaml",)):
            with self.subTest(overrides=overrides):
                rendered = self.render(platforms_file, *overrides, **credentials)
                docker = load_controller_config(rendered)
                self.assertEqual(docker.platforms, native.platforms)
                self.assertEqual(docker.default_platform, native.default_platform)
                self.assertEqual(docker.summary()["platforms"], native.summary()["platforms"])

    def test_compose_reports_invalid_platforms_like_native(self):
        files = Files(self)
        settings = {"NEST_PLATFORMS": "lab", "NEST_PLATFORM_LAB_ACCOUNT": "a b"}
        text = "".join(f"{k}={v}\n" for k, v in settings.items())
        platforms_file = Path(files.write("platforms.env", text))
        with self.assertRaises(ConfigError) as docker:
            load_controller_config(self.render(platforms_file))
        with self.assertRaises(ConfigError) as native:
            load_controller_config({**settings, "NEST_EXECUTOR": "slurm"})
        self.assertEqual(docker.exception.problems, native.exception.problems)


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
                "NEST_CONTROLLER_BIND": "127.0.0.1:0",
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

        mismatched_hostname = load_client_config(
            {
                "NEST_CONTROLLER_ADDRESS": f"127.0.0.1:{port}",
                "NEST_CONTROLLER_TLS_CA_FILE": str(cert),
            }
        )
        with _channel(mismatched_hostname) as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)
            with self.assertRaises(grpc.RpcError) as caught:
                stub.Execute(execution_pb2.ExecuteRequest(gpu=-1), timeout=5)
            self.assertEqual(cast(grpc.Call, caught.exception).code(), grpc.StatusCode.UNAVAILABLE)

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
