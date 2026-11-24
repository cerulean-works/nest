"""Environment-first deployment: direct and file credential sources, generated files.

Covers the shared source rules (unset, empty, conflicting, multiline, invalid), the
private runtime files written for OpenSSH and libpq, redaction, the credentials kept
out of child processes, and `NEST_DATABASE_PASSWORD` against a real PostgreSQL.
These are `unittest` cases so they also run inside the Controller image.
"""

import io
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent import futures
from contextlib import redirect_stderr
from pathlib import Path
from typing import ClassVar, cast
from unittest import mock

import grpc
from postgres_fixture import (
    ADMIN_URL,
    TLS_CA_FILE,
    TLS_OTHER_CA_FILE,
    TLS_URL,
    PostgresTestCase,
    PostgresTLSTestCase,
    connect,
    temporary_database,
)
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from nest import _logging, healthcheck, runtime_files
from nest.backends.slurm import SlurmBackend, SlurmConfig
from nest.client import _channel
from nest.config import (
    CHILD_EXCLUDED_SETTINGS,
    SECRET_SETTINGS,
    ConfigError,
    child_environ,
    load_client_config,
    load_controller_config,
    load_database_config,
    materialize_ssh_credentials,
)
from nest.controller import ExecutionService, _add_port
from nest.db import Database, DatabaseError
from nest.mail import tls_context
from nest.proto import execution_pb2, execution_pb2_grpc

ROOT = Path(__file__).resolve().parents[1]

# PEM framing split so the repository credential scan does not match these
# placeholders. None of them is a real key or certificate.
BEGIN, END = "-----BEGIN ", "-----END "
PRIVATE_KEY = (
    f"{BEGIN}OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAAbm90LWEtcmVhbC1rZXk\n"
    f"{END}OPENSSH PRIVATE KEY-----"
)
CERTIFICATE = f"{BEGIN}CERTIFICATE-----\nTUlJQm5vdC1hLXJlYWwtY2VydGlmaWNhdGU\n{END}CERTIFICATE-----"
KNOWN_HOSTS = "login1.example.edu ssh-ed25519 AAAAC3Nza1\nlogin2.example.edu ssh-ed25519 AAAAC3Nza2"
DB_PASSWORD = "p@ss:w/rd?#%&='\"\\ ;x"
"""Every character that breaks naive URL or conninfo concatenation."""
SECRET_MARKER = "s3cr3t-value-that-must-never-appear"
WEAK = "StrictHostKeyChecking " + "no"


def write(directory: Path, name: str, text: str, mode: int = 0o600) -> str:
    path = directory / name
    path.write_text(text)
    path.chmod(mode)
    return str(path)


def problems(environ: dict[str, str]) -> str:
    with unittest.TestCase().assertRaises(ConfigError) as caught:
        load_controller_config(environ)
    return str(caught.exception)


def mode_of(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def openssl_pair(directory: Path) -> tuple[str, str]:
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert.read_text(), key.read_text()


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)


class DatabasePasswordTests(TempDirTestCase):
    URL = "postgresql://nest@db.example.internal:5432/nest"

    def test_direct_password_is_passed_as_its_own_keyword(self):
        config = load_database_config(
            {"NEST_DATABASE_URL": self.URL, "NEST_DATABASE_PASSWORD": DB_PASSWORD}
        )
        self.assertIsNotNone(config.password)
        params = conninfo_to_dict(Database(config).conninfo())
        self.assertEqual(params["password"], DB_PASSWORD)
        self.assertEqual(params["host"], "db.example.internal")
        self.assertEqual(params["dbname"], "nest")

    def test_direct_password_is_redacted_everywhere(self):
        config = load_database_config(
            {"NEST_DATABASE_URL": self.URL, "NEST_DATABASE_PASSWORD": SECRET_MARKER}
        )
        for text in (repr(config), str(config.summary()), config.redacted_url):
            self.assertNotIn(SECRET_MARKER, text)
        self.assertEqual(config.summary()["password"], "***")
        database = Database(config)
        self.assertNotIn(SECRET_MARKER, database.scrub(f'password "{SECRET_MARKER}" rejected'))

    def test_password_sources_are_exclusive_and_never_echoed(self):
        secret = write(self.root, "password", SECRET_MARKER + "\n")
        for extra in (
            {"NEST_DATABASE_PASSWORD_FILE": secret},
            {"NEST_DATABASE_URL": f"postgresql://nest:{SECRET_MARKER}@db/nest"},
            {"NEST_DATABASE_URL": f"postgresql://db/nest?password={SECRET_MARKER}"},
        ):
            with self.subTest(extra=list(extra)):
                env = {
                    "NEST_DATABASE_URL": self.URL,
                    "NEST_DATABASE_PASSWORD": SECRET_MARKER,
                    **extra,
                }
                with self.assertRaises(ConfigError) as caught:
                    load_database_config(env)
                self.assertRegex(str(caught.exception), "only one of|Set only one of")
                self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_empty_values_mean_unset(self):
        config = load_database_config(
            {
                "NEST_DATABASE_URL": self.URL,
                "NEST_DATABASE_PASSWORD": "  ",
                "NEST_DATABASE_PASSWORD_FILE": "",
            }
        )
        self.assertIsNone(config.password)
        self.assertIsNone(config.password_file)

    def test_multiline_and_empty_file_passwords_are_rejected(self):
        cases = {
            "single line": {"NEST_DATABASE_PASSWORD": SECRET_MARKER + "\nsecond"},
            "is empty": {"NEST_DATABASE_PASSWORD_FILE": write(self.root, "empty", "\n")},
            "on one line": {"NEST_DATABASE_PASSWORD_FILE": write(self.root, "two", "a\nb\n")},
            "not a readable file": {"NEST_DATABASE_PASSWORD_FILE": str(self.root / "missing")},
        }
        for expected, extra in cases.items():
            with self.subTest(expected=expected):
                with self.assertRaises(ConfigError) as caught:
                    load_database_config({"NEST_DATABASE_URL": self.URL, **extra})
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_password_file_still_works(self):
        secret = write(self.root, "password", DB_PASSWORD + "\n")
        config = load_database_config(
            {"NEST_DATABASE_URL": self.URL, "NEST_DATABASE_PASSWORD_FILE": secret}
        )
        self.assertIsNone(config.password)
        self.assertEqual(conninfo_to_dict(Database(config).conninfo())["password"], DB_PASSWORD)

    def test_stray_password_without_url_is_reported(self):
        with self.assertRaises(ConfigError) as caught:
            load_database_config({"NEST_DATABASE_PASSWORD": SECRET_MARKER})
        self.assertIn("NEST_DATABASE_PASSWORD", str(caught.exception))
        self.assertNotIn(SECRET_MARKER, str(caught.exception))


class DatabaseTLSDataTests(TempDirTestCase):
    def test_inline_tls_material_becomes_private_files(self):
        config = load_database_config(
            {
                "NEST_DATABASE_URL": "postgresql://nest@db.example.internal/nest",
                "NEST_DATABASE_SSLROOTCERT_DATA": CERTIFICATE.replace("\n", "\\n"),
                "NEST_DATABASE_SSLCERT_DATA": CERTIFICATE,
                "NEST_DATABASE_SSLKEY_DATA": PRIVATE_KEY,
            }
        )
        self.assertEqual(config.sslmode, "verify-full", "A CA implies verify-full")
        self.assertNotIn("PRIVATE", repr(config) + str(config.summary()))
        params = conninfo_to_dict(Database(config).conninfo())
        for key, expected in (
            ("sslrootcert", CERTIFICATE),
            ("sslcert", CERTIFICATE),
            ("sslkey", PRIVATE_KEY),
        ):
            with self.subTest(key=key):
                path = str(params[key])
                self.assertEqual(Path(path).read_text(), expected + "\n")
                self.assertEqual(mode_of(path), 0o600)
                self.assertEqual(mode_of(os.path.dirname(path)), 0o700)

    def test_inline_and_file_sources_conflict(self):
        ca = write(self.root, "ca.crt", CERTIFICATE, 0o644)
        with self.assertRaises(ConfigError) as caught:
            load_database_config(
                {
                    "NEST_DATABASE_URL": "postgresql://nest@db/nest",
                    "NEST_DATABASE_SSLROOTCERT": ca,
                    "NEST_DATABASE_SSLROOTCERT_DATA": CERTIFICATE,
                }
            )
        self.assertIn(
            "Set only one of NEST_DATABASE_SSLROOTCERT_DATA and NEST_DATABASE_SSLROOTCERT",
            str(caught.exception),
        )

    def test_malformed_inline_key_is_rejected_without_echo(self):
        with self.assertRaises(ConfigError) as caught:
            load_database_config(
                {
                    "NEST_DATABASE_URL": "postgresql://nest@db/nest",
                    "NEST_DATABASE_SSLCERT_DATA": CERTIFICATE,
                    "NEST_DATABASE_SSLKEY_DATA": SECRET_MARKER,
                }
            )
        self.assertIn("does not look like a PEM private key", str(caught.exception))
        self.assertNotIn(SECRET_MARKER, str(caught.exception))


class ServerTLSTests(TempDirTestCase):
    def test_inline_certificate_and_key_serve_tls(self):
        cert, key = openssl_pair(self.root)
        config = load_controller_config(
            {
                "NEST_CONTROLLER_BIND": "127.0.0.1:0",
                "NEST_TLS_CERT": cert,
                "NEST_TLS_KEY": key.replace("\n", "\\n"),
            }
        )
        assert config.tls is not None
        self.assertNotIn("PRIVATE", repr(config) + str(config.summary()))
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
            ExecutionService(executor="local"), server
        )
        port = _add_port(server, config)
        server.start()
        self.addCleanup(server.stop, 0)
        ca = write(self.root, "ca.pem", cert, 0o644)
        client = load_client_config(
            {"NEST_CONTROLLER_ADDRESS": f"localhost:{port}", "NEST_CONTROLLER_TLS_CA_FILE": ca}
        )
        with _channel(client) as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)
            with self.assertRaises(grpc.RpcError) as caught:
                stub.Execute(execution_pb2.ExecuteRequest(gpu=-1), timeout=5)
            self.assertEqual(
                cast(grpc.Call, caught.exception).code(), grpc.StatusCode.INVALID_ARGUMENT
            )
        with mock.patch.dict(
            os.environ, {"NEST_CONTROLLER_BIND": f"0.0.0.0:{port}", "NEST_TLS_CERT": cert}
        ):
            self.assertEqual(healthcheck.main(), 0, "The healthcheck must detect inline TLS")

    def test_mixed_sources_and_pairing(self):
        cert_file = write(self.root, "cert.pem", CERTIFICATE, 0o644)
        config = load_controller_config(
            {"NEST_TLS_CERT_FILE": cert_file, "NEST_TLS_KEY": PRIVATE_KEY}
        )
        assert config.tls is not None
        self.assertEqual(
            config.tls.credentials(), ((PRIVATE_KEY + "\n").encode(), CERTIFICATE.encode())
        )
        self.assertIn("Set both", problems({"NEST_TLS_CERT": CERTIFICATE}))
        self.assertIn(
            "Set only one of NEST_TLS_KEY and NEST_TLS_KEY_FILE",
            problems(
                {
                    "NEST_TLS_CERT": CERTIFICATE,
                    "NEST_TLS_KEY": PRIVATE_KEY,
                    "NEST_TLS_KEY_FILE": cert_file,
                }
            ),
        )
        text = problems({"NEST_TLS_CERT": CERTIFICATE, "NEST_TLS_KEY": SECRET_MARKER})
        self.assertIn("NEST_TLS_KEY does not look like a PEM private key", text)
        self.assertNotIn(SECRET_MARKER, text)


class ScalarSecretTests(TempDirTestCase):
    GOOGLE: ClassVar[dict[str, str]] = {
        "NEST_DATABASE_URL": "postgresql://nest@db/nest",
        "NEST_GOOGLE_CLIENT_ID": "1234-abc.apps.googleusercontent.com",
        "NEST_GOOGLE_REDIRECT_URL": "https://nest.example.com/auth/google/callback",
    }

    def test_google_client_secret_from_the_environment(self):
        config = load_controller_config({**self.GOOGLE, "NEST_GOOGLE_CLIENT_SECRET": SECRET_MARKER})
        assert config.google is not None
        self.assertEqual(config.google.read_client_secret(), SECRET_MARKER)
        self.assertNotIn(SECRET_MARKER, repr(config) + str(config.summary()))

    def test_google_client_secret_rules(self):
        secret_file = write(self.root, "google", SECRET_MARKER + "\n")
        self.assertIn("single line", problems({**self.GOOGLE, "NEST_GOOGLE_CLIENT_SECRET": "a\nb"}))
        both = problems(
            {
                **self.GOOGLE,
                "NEST_GOOGLE_CLIENT_SECRET": SECRET_MARKER,
                "NEST_GOOGLE_CLIENT_SECRET_FILE": secret_file,
            }
        )
        self.assertIn("Set only one of NEST_GOOGLE_CLIENT_SECRET", both)
        self.assertNotIn(SECRET_MARKER, both)
        self.assertIn(
            "needs a client secret", problems({**self.GOOGLE, "NEST_GOOGLE_CLIENT_SECRET": " "})
        )

    def test_smtp_ca_inline(self):
        cert, _ = openssl_pair(self.root)
        config = load_controller_config(
            {
                "NEST_DATABASE_URL": "postgresql://nest@db/nest",
                "NEST_EMAIL_ENABLED": "true",
                "NEST_EMAIL_FROM": "Nest <nest@example.com>",
                "NEST_PUBLIC_URL": "https://nest.example.com",
                "NEST_SMTP_HOST": "smtp.example.com",
                "NEST_SMTP_CA": cert,
            }
        )
        assert config.email is not None
        context = tls_context(config.email.smtp)
        self.assertEqual(len(context.get_ca_certs()), 1, "Only the inline CA is trusted")
        self.assertEqual(config.email.summary()["smtp_ca_file"], "inline")


class SSHSourceTests(TempDirTestCase):
    def mode_b(self, **extra):
        return {
            "NEST_EXECUTOR": "slurm",
            "NEST_SLURM_SSH_MODE": "B",
            "NEST_SLURM_LOGIN_NODES": "login1.example.edu",
            "NEST_SLURM_SSH_USER": "svc-nest",
            "NEST_SLURM_ACCOUNT": "physics",
            "NEST_SLURM_WORKDIR": "/scratch/nest",
            **extra,
        }

    def test_inline_mode_b_files_are_private_and_normalized(self):
        config = load_controller_config(
            self.mode_b(
                NEST_SSH_PRIVATE_KEY=PRIVATE_KEY.replace("\n", "\\n"),
                NEST_SSH_KNOWN_HOSTS_DATA=KNOWN_HOSTS.replace("\n", "\r\n"),
            )
        )
        identity, known_hosts = materialize_ssh_credentials(config.ssh)
        assert identity is not None and known_hosts is not None
        self.assertEqual(Path(identity).read_text(), PRIVATE_KEY + "\n")
        self.assertEqual(Path(known_hosts).read_text(), KNOWN_HOSTS + "\n")
        for path in (identity, known_hosts):
            self.assertEqual(mode_of(path), 0o600)
        self.assertEqual(mode_of(os.path.dirname(identity)), 0o700)

    def test_conflicting_mode_b_sources(self):
        key = write(self.root, "id", PRIVATE_KEY)
        hosts = write(self.root, "known_hosts", KNOWN_HOSTS)
        text = problems(
            self.mode_b(
                NEST_SSH_PRIVATE_KEY=PRIVATE_KEY,
                NEST_SSH_IDENTITY_FILE=key,
                NEST_SSH_KNOWN_HOSTS=hosts,
                NEST_SSH_KNOWN_HOSTS_DATA=KNOWN_HOSTS,
            )
        )
        self.assertIn("Set only one of NEST_SSH_PRIVATE_KEY and NEST_SSH_IDENTITY_FILE", text)
        self.assertIn("Set only one of NEST_SSH_KNOWN_HOSTS_DATA and NEST_SSH_KNOWN_HOSTS", text)
        self.assertNotIn("b3BlbnNzaC1", text)

    def test_weakened_inline_ssh_config_is_refused_at_startup(self):
        text = problems({"NEST_SSH_CONFIG": f"Host hpc\n    {WEAK}\n"})
        self.assertIn("NEST_SSH_CONFIG must keep host-key verification on", text)
        load_controller_config({"NEST_SSH_CONFIG": "Host hpc\n    StrictHostKeyChecking yes\n"})


class RuntimeFileTests(TempDirTestCase):
    def test_write_is_exclusive_and_private(self):
        path = runtime_files.write_private_file(str(self.root), "key", "secret\n")
        self.assertEqual(mode_of(path), 0o600)
        self.assertEqual(runtime_files.write_private_file(str(self.root), "key", "secret\n"), path)
        with self.assertRaises(runtime_files.RuntimeFileError) as caught:
            runtime_files.write_private_file(str(self.root), "key", "other\n")
        self.assertNotIn("other", str(caught.exception).replace("different", ""))
        os.symlink(path, self.root / "link")
        with self.assertRaises(runtime_files.RuntimeFileError):
            runtime_files.write_private_file(str(self.root), "link", "secret\n")
        with self.assertRaises(runtime_files.RuntimeFileError):
            runtime_files.write_private_file(str(self.root), "../escape", "x")

    def test_literal_content_is_never_expanded(self):
        text = "Host $HOME `id` $(id) ${USER}\n"
        path = runtime_files.write_private_file(str(self.root), "config", text)
        self.assertEqual(Path(path).read_text(), text)

    def test_ssh_home_materialization(self):
        ssh = self.root / "ssh"
        env = {
            "NEST_SSH_CONFIG": "Host hpc\\n    HostName login.example.edu\\n    IdentityFile ~/.ssh/id_nest",
            "NEST_SSH_PRIVATE_KEY": PRIVATE_KEY,
            "NEST_SSH_KNOWN_HOSTS_DATA": KNOWN_HOSTS,
        }
        written = runtime_files.materialize_ssh_home(str(ssh), env)
        self.assertEqual(written, ["config", "id_nest", "known_hosts"])
        self.assertIn("HostName login.example.edu\n", (ssh / "config").read_text())
        self.assertEqual((ssh / "id_nest").read_text(), PRIVATE_KEY + "\n")
        for name in written:
            self.assertEqual(mode_of(str(ssh / name)), 0o600)
        self.assertEqual(mode_of(str(ssh)), 0o700)
        # A restart with the same environment is accepted.
        self.assertEqual(runtime_files.materialize_ssh_home(str(ssh), env), written)

    def test_ssh_home_is_untouched_without_inline_config(self):
        ssh = self.root / "ssh"
        self.assertEqual(
            runtime_files.materialize_ssh_home(str(ssh), {"NEST_SSH_PRIVATE_KEY": PRIVATE_KEY}), []
        )
        self.assertFalse(ssh.exists(), "Mode B and mounted deployments are unchanged")

    def test_ssh_home_conflicts_and_validation(self):
        ssh = self.root / "ssh"
        ssh.mkdir(mode=0o700)
        (ssh / "known_hosts").write_text("mounted.example.edu ssh-ed25519 AAAA\n")
        env = {"NEST_SSH_CONFIG": "Host hpc", "NEST_SSH_KNOWN_HOSTS_DATA": KNOWN_HOSTS}
        with self.assertRaises(runtime_files.RuntimeFileError) as caught:
            runtime_files.materialize_ssh_home(str(ssh), env)
        self.assertIn("not both", str(caught.exception))
        self.assertEqual(
            (ssh / "known_hosts").read_text(), "mounted.example.edu ssh-ed25519 AAAA\n"
        )
        for bad, message in (
            ({"NEST_SSH_CONFIG": f"Host hpc\n  {WEAK}"}, "host-key verification"),
            (
                {"NEST_SSH_CONFIG": "Host x\n  UserKnownHostsFile " + "/dev/null"},
                "host-key verification",
            ),
            (
                {"NEST_SSH_CONFIG": "Host x", "NEST_SSH_PRIVATE_KEY": SECRET_MARKER},
                "does not look like",
            ),
        ):
            with (
                self.subTest(message=message),
                self.assertRaises(runtime_files.RuntimeFileError) as caught,
            ):
                runtime_files.materialize_ssh_home(str(self.root / "fresh"), bad)
            self.assertIn(message, str(caught.exception))
            self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_entrypoint_helper_reports_without_contents(self):
        stderr = io.StringIO()
        env = {"NEST_SSH_CONFIG": "Host x", "NEST_SSH_PRIVATE_KEY": SECRET_MARKER}
        with mock.patch.dict(os.environ, env), redirect_stderr(stderr):
            self.assertEqual(runtime_files.main(["ssh", str(self.root / "ssh")]), 2)
        self.assertNotIn(SECRET_MARKER, stderr.getvalue())
        self.assertEqual(runtime_files.main(["bogus"]), 2)


class PlatformsEnvTests(unittest.TestCase):
    def example(self) -> str:
        return (ROOT / "platforms.env.example").read_text()

    def native(self) -> dict[str, str]:
        lines = [line for line in self.example().splitlines() if line and not line.startswith("#")]
        return dict(line.split("=", 1) for line in lines)

    def secrets(self):
        return {
            "NEST_SSH_PRIVATE_KEY": PRIVATE_KEY,
            "NEST_SSH_KNOWN_HOSTS_DATA": KNOWN_HOSTS,
            "NEST_EXECUTOR": "slurm",
        }

    def test_inline_file_matches_native_variables(self):
        if not (ROOT / "platforms.env.example").exists():
            self.skipTest("needs the repository checkout")
        inline = load_controller_config({**self.secrets(), "NEST_PLATFORMS_ENV": self.example()})
        native = load_controller_config({**self.secrets(), **self.native()})
        self.assertEqual(inline.summary()["platforms"], native.summary()["platforms"])
        self.assertEqual(inline.default_platform, "campus")
        escaped = load_controller_config(
            {**self.secrets(), "NEST_PLATFORMS_ENV": self.example().replace("\n", "\\n")}
        )
        self.assertEqual(escaped.summary()["platforms"], native.summary()["platforms"])

    def test_values_are_literal(self):
        config = load_controller_config(
            {
                **self.secrets(),
                "NEST_PLATFORMS_ENV": (
                    "export NEST_PLATFORMS=lab\n"
                    "NEST_PLATFORM_LAB_NAME='Lab $HOME cluster'\n"
                    'NEST_PLATFORM_LAB_LOGIN_NODES="lab-login.example.edu"\n'
                    "NEST_PLATFORM_LAB_SSH_MODE=B\nNEST_PLATFORM_LAB_SSH_USER=svc\n"
                    "NEST_PLATFORM_LAB_ACCOUNT=lab\nNEST_PLATFORM_LAB_WORKDIR=/home/svc/nest\n"
                ),
            }
        )
        self.assertEqual(config.platforms["lab"].display_name, "Lab $HOME cluster")

    def test_conflicts_and_foreign_names(self):
        text = problems(
            {
                "NEST_PLATFORMS": "lab",
                "NEST_PLATFORMS_ENV": (
                    f"NEST_PLATFORMS=lab\nNEST_SSH_PRIVATE_KEY={SECRET_MARKER}\n"
                    f"{SECRET_MARKER}\nNEST_DEFAULT_PLATFORM=lab\nNEST_DEFAULT_PLATFORM=lab\n"
                ),
            }
        )
        self.assertIn(
            "NEST_PLATFORMS is set both in NEST_PLATFORMS_ENV and as its own variable", text
        )
        self.assertIn("NEST_PLATFORMS_ENV line 2 must be", text)
        self.assertIn("NEST_PLATFORMS_ENV line 3 must be", text)
        self.assertIn("NEST_DEFAULT_PLATFORM appears more than once", text)
        self.assertNotIn(SECRET_MARKER, text)


class ChildEnvironmentTests(unittest.TestCase):
    ENV = {name: SECRET_MARKER for name in CHILD_EXCLUDED_SETTINGS} | {
        "PATH": "/usr/bin",
        "HOME": "/home/nest",
    }

    def test_credentials_are_removed(self):
        env = child_environ(self.ENV)
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/home/nest"})
        self.assertTrue(set(SECRET_SETTINGS) <= set(CHILD_EXCLUDED_SETTINGS))

    def test_ssh_processes_get_the_reduced_environment(self):
        backend = SlurmBackend(SlurmConfig(ssh_host="hpc", account="physics", ssh_mode="A"))
        with mock.patch.dict(os.environ, self.ENV), mock.patch("subprocess.run") as run:
            backend._ssh_process("true")
        env = run.call_args.kwargs["env"]
        self.assertNotIn(SECRET_MARKER, " ".join(env.values()))
        self.assertEqual(env["HOME"], "/home/nest")

    def test_local_workers_never_see_controller_credentials(self):
        from nest.executor import execute

        def environment():
            return {k: v for k, v in os.environ.items() if k.startswith("NEST_")}

        with mock.patch.dict(os.environ, {**self.ENV, "PATH": os.environ.get("PATH", "")}):
            seen = execute(environment)
        self.assertFalse(set(seen) & set(CHILD_EXCLUDED_SETTINGS), seen)
        self.assertNotIn(SECRET_MARKER, " ".join(seen.values()))


class RedactionTests(unittest.TestCase):
    def test_direct_secrets_are_masked_in_logs(self):
        cert_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cert_dir)
        cert, key = openssl_pair(cert_dir)
        config = load_controller_config(
            {
                "NEST_DATABASE_URL": "postgresql://nest@db/nest",
                "NEST_DATABASE_PASSWORD": SECRET_MARKER,
                "NEST_TLS_CERT": cert,
                "NEST_TLS_KEY": key,
                "NEST_CONTROLLER_BIND": "127.0.0.1:50051",
            }
        )
        assert config.database is not None and config.tls is not None
        Database(config.database)  # registers the password with the log filter
        _logging.register_secret(cast(str, config.tls.key_pem and config.tls.key_pem.reveal()))
        key_line = key.splitlines()[1]
        record = logging.LogRecord(
            "nest.test", logging.ERROR, __file__, 1, "%s %s", (SECRET_MARKER, key_line), None
        )
        text = _logging._RedactingFormatter("%(message)s").format(record)
        self.assertNotIn(SECRET_MARKER, text)
        self.assertNotIn(key_line, text)


class PasswordWithSpecialCharactersTests(PostgresTestCase):
    """`NEST_DATABASE_PASSWORD` with URL-hostile characters: upgrade, verify, serve."""

    def test_upgrade_verify_and_open_with_a_direct_password(self):
        with temporary_database() as url:
            role = "nest_env_" + os.urandom(4).hex()
            with connect(ADMIN_URL) as admin:
                admin.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(DB_PASSWORD)
                    )
                )
                dbname = str(conninfo_to_dict(url)["dbname"])
                admin.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                        sql.Identifier(dbname), sql.Identifier(role)
                    )
                )
            self.addCleanup(self._drop_role, role)
            params = conninfo_to_dict(url)
            target = f"postgresql://{role}@{params.get('host', '127.0.0.1')}:{params.get('port', 5432)}/{dbname}"
            env = {
                **{k: v for k, v in os.environ.items() if not k.startswith("NEST_")},
                "NEST_DATABASE_URL": target,
                "NEST_DATABASE_PASSWORD": DB_PASSWORD,
            }
            for command in (["upgrade"], ["verify"]):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, "-m", "nest.db.cli", *command],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn(DB_PASSWORD, result.stdout + result.stderr)
            wrong = subprocess.run(
                [sys.executable, "-m", "nest.db.cli", "verify"],
                env={**env, "NEST_DATABASE_PASSWORD": SECRET_MARKER},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(wrong.returncode, 0)
            self.assertNotIn(SECRET_MARKER, wrong.stdout + wrong.stderr)
            config = load_controller_config(
                {"NEST_DATABASE_URL": target, "NEST_DATABASE_PASSWORD": DB_PASSWORD}
            )
            assert config.database is not None
            database = Database(config.database)
            database.open()
            try:
                with database.connection() as connection:
                    row = connection.execute("SELECT current_user").fetchone()
                    self.assertEqual(row and row[0], role)
            finally:
                database.close()

    def _drop_role(self, role):
        with connect(ADMIN_URL) as admin:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


class InlineDatabaseCATests(PostgresTLSTestCase):
    """`NEST_DATABASE_SSLROOTCERT_DATA` verifies the server exactly like a CA file."""

    def environment(self, ca_file: str) -> dict[str, str]:
        parts = conninfo_to_dict(TLS_URL)
        password = str(parts.pop("password", ""))
        url = f"postgresql://{parts.get('user', 'postgres')}@{parts['host']}:{parts.get('port', 5432)}/{parts.get('dbname', 'postgres')}"
        return {
            "NEST_DATABASE_URL": url,
            "NEST_DATABASE_PASSWORD": password,
            "NEST_DATABASE_SSLROOTCERT_DATA": Path(ca_file).read_text().replace("\n", "\\n"),
        }

    def test_inline_ca_verifies_and_encrypts(self):
        config = load_database_config(self.environment(TLS_CA_FILE))
        self.assertEqual(config.effective_sslmode, "verify-full")
        database = Database(config)
        database.open()
        try:
            with database.connection() as connection:
                row = connection.execute(
                    "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
                ).fetchone()
                self.assertEqual(row and row[0], True)
        finally:
            database.close()

    def test_inline_untrusted_ca_is_rejected(self):
        database = Database(load_database_config(self.environment(TLS_OTHER_CA_FILE)))
        with self.assertRaises(DatabaseError):
            database.open()
        database.close()


if __name__ == "__main__":
    unittest.main()
