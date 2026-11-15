"""PostgreSQL persistence: configuration, migrations, `nest-db`, and the Controller.

Configuration tests need no server. The others run against a real PostgreSQL named
by `NEST_TEST_DATABASE_URL` (see `postgres_fixture`). These are `unittest` cases so
they also run inside the Controller image, where PostgreSQL tests skip.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import cloudpickle
import grpc
from postgres_fixture import PostgresTestCase, connect
from test_controller import controller_at_free_port, controller_environment

from nest.config import ConfigError, load_controller_config, load_database_config
from nest.controller import ExecutionService
from nest.db import (
    Database,
    JobStore,
    MigrationError,
    load_migrations,
    schema_status,
    sync_providers,
    upgrade,
    verify,
)
from nest.db.migrate import MIGRATION_LOCK_ID
from nest.errors import ERROR_KIND_METADATA, DatabaseUnavailableError
from nest.proto import execution_pb2

PASSWORD = "hunter2-db-secret"


def write(directory: Path, name: str, text: str, mode: int = 0o600) -> str:
    path = directory / name
    path.write_text(text)
    path.chmod(mode)
    return str(path)


class DatabaseConfigTests(unittest.TestCase):
    def setUp(self):
        self.files = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.files))

    def problems(self, settings):
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(settings)
        return str(caught.exception)

    def test_database_is_opt_in(self):
        self.assertIsNone(load_controller_config({}).database)

    def test_required_database_needs_a_url(self):
        text = self.problems({"NEST_DATABASE_REQUIRED": "true"})
        self.assertIn("NEST_DATABASE_REQUIRED=true but NEST_DATABASE_URL is unset", text)
        self.assertIn(
            "NEST_DATABASE_REQUIRED must be true or false",
            self.problems({"NEST_DATABASE_REQUIRED": "maybe"}),
        )

    def test_database_settings_without_a_url_are_rejected(self):
        text = self.problems({"NEST_DATABASE_SSLMODE": "verify-full"})
        self.assertIn("NEST_DATABASE_SSLMODE have no effect without NEST_DATABASE_URL", text)

    def test_url_must_be_postgresql_with_host_and_database(self):
        for url, message in (
            (f"mysql://nest:{PASSWORD}@db/nest", "must start with postgresql://"),
            (f"sqlite:///nest-{PASSWORD}.db", "must start with postgresql://"),
            (f"postgresql://nest:{PASSWORD}@/nest", "must name the database host"),
            (f"postgresql://nest:{PASSWORD}@db", "must name the database"),
            (f"postgresql://nest:{PASSWORD}@db:99999/nest", "not a valid URL"),
        ):
            with self.subTest(url=url):
                text = self.problems({"NEST_DATABASE_URL": url})
                self.assertIn(message, text)
                self.assertNotIn(PASSWORD, text)

    def test_valid_url_is_wrapped_and_redacted(self):
        url = f"postgresql://nest:{PASSWORD}@db.internal:5432/nest?sslmode=verify-full"
        config = load_controller_config({"NEST_DATABASE_URL": url})
        assert config.database is not None
        self.assertEqual(config.database.url.reveal(), url)
        self.assertEqual(config.database.effective_sslmode, "verify-full")
        for text in (repr(config), str(config.summary()), repr(config.database)):
            self.assertNotIn(PASSWORD, text)
        self.assertEqual(
            config.summary()["database"]["url"],  # ty: ignore[not-subscriptable]
            "postgresql://nest:***@db.internal:5432/nest?***",
        )

    def test_sslmode_is_validated_and_set_in_one_place(self):
        url = "postgresql://nest@db/nest"
        self.assertIn(
            "NEST_DATABASE_SSLMODE must be one of",
            self.problems({"NEST_DATABASE_URL": url, "NEST_DATABASE_SSLMODE": "on"}),
        )
        self.assertIn(
            "sslmode='strict'", self.problems({"NEST_DATABASE_URL": url + "?sslmode=strict"})
        )
        self.assertIn(
            "sslmode is set in both",
            self.problems(
                {
                    "NEST_DATABASE_URL": url + "?sslmode=require",
                    "NEST_DATABASE_SSLMODE": "verify-full",
                }
            ),
        )

    def test_ca_file_defaults_to_verify_full(self):
        ca = write(self.files, "ca.crt", "ca", 0o644)
        config = load_database_config(
            {"NEST_DATABASE_URL": "postgresql://nest@db/nest", "NEST_DATABASE_SSLROOTCERT": ca}
        )
        self.assertEqual(config.sslmode, "verify-full")
        self.assertEqual(config.sslrootcert, ca)
        text = self.problems(
            {
                "NEST_DATABASE_URL": "postgresql://nest@db/nest",
                "NEST_DATABASE_SSLROOTCERT": ca,
                "NEST_DATABASE_SSLMODE": "prefer",
            }
        )
        self.assertIn("NEST_DATABASE_SSLROOTCERT is not used with sslmode=prefer", text)

    def test_client_certificate_needs_a_private_key(self):
        cert = write(self.files, "client.crt", "cert", 0o644)
        key = write(self.files, "client.key", "key", 0o644)
        url = "postgresql://nest@db/nest"
        self.assertIn(
            "Set both NEST_DATABASE_SSLCERT and NEST_DATABASE_SSLKEY",
            self.problems({"NEST_DATABASE_URL": url, "NEST_DATABASE_SSLCERT": cert}),
        )
        self.assertIn(
            "readable by other users",
            self.problems(
                {
                    "NEST_DATABASE_URL": url,
                    "NEST_DATABASE_SSLCERT": cert,
                    "NEST_DATABASE_SSLKEY": key,
                }
            ),
        )
        os.chmod(key, 0o600)
        config = load_database_config(
            {"NEST_DATABASE_URL": url, "NEST_DATABASE_SSLCERT": cert, "NEST_DATABASE_SSLKEY": key}
        )
        self.assertEqual((config.sslcert, config.sslkey), (cert, key))

    def test_pool_sizes_and_timeouts_are_validated(self):
        url = "postgresql://nest@db/nest"
        text = self.problems(
            {
                "NEST_DATABASE_URL": url,
                "NEST_DATABASE_POOL_MIN_SIZE": "5",
                "NEST_DATABASE_POOL_MAX_SIZE": "2",
            }
        )
        self.assertIn("NEST_DATABASE_POOL_MIN_SIZE (5) exceeds NEST_DATABASE_POOL_MAX_SIZE", text)
        for name, value in (
            ("NEST_DATABASE_POOL_MAX_SIZE", "0"),
            ("NEST_DATABASE_POOL_MIN_SIZE", "two"),
            ("NEST_DATABASE_CONNECT_TIMEOUT", "-1"),
            ("NEST_DATABASE_POOL_TIMEOUT", "never"),
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.problems({"NEST_DATABASE_URL": url, name: value}))
        config = load_database_config(
            {
                "NEST_DATABASE_URL": url,
                "NEST_DATABASE_POOL_MIN_SIZE": "0",
                "NEST_DATABASE_POOL_MAX_SIZE": "4",
                "NEST_DATABASE_CONNECT_TIMEOUT": "2.5",
            }
        )
        self.assertEqual((config.pool_min_size, config.pool_max_size), (0, 4))
        self.assertEqual(config.connect_timeout, 2.5)

    def test_password_file_is_exclusive_with_a_url_password(self):
        secret = write(self.files, "password", PASSWORD + "\n")
        text = self.problems(
            {
                "NEST_DATABASE_URL": f"postgresql://nest:{PASSWORD}@db/nest",
                "NEST_DATABASE_PASSWORD_FILE": secret,
            }
        )
        self.assertIn("NEST_DATABASE_URL or NEST_DATABASE_PASSWORD_FILE", text)
        self.assertNotIn(PASSWORD, text)

    def test_conninfo_carries_tls_timeout_and_password_file(self):
        ca = write(self.files, "ca.crt", "ca", 0o644)
        secret = write(self.files, "password", PASSWORD + "\n")
        config = load_database_config(
            {
                "NEST_DATABASE_URL": "postgresql://nest@db.internal/nest",
                "NEST_DATABASE_SSLROOTCERT": ca,
                "NEST_DATABASE_PASSWORD_FILE": secret,
                "NEST_DATABASE_CONNECT_TIMEOUT": "2.5",
            }
        )
        conninfo = Database(config).conninfo()
        for part in (
            "sslmode=verify-full",
            f"sslrootcert={ca}",
            "connect_timeout=3",
            f"password={PASSWORD}",
            "client_encoding=UTF8",
            "application_name=nest-controller",
        ):
            self.assertIn(part, conninfo)
        self.assertNotIn(PASSWORD, str(config.summary()))

    def test_nest_db_requires_a_url(self):
        with self.assertRaises(ConfigError) as caught:
            load_database_config({})
        self.assertIn("NEST_DATABASE_URL is required", str(caught.exception))

    def test_packaged_migrations_form_one_chain(self):
        migrations = load_migrations()
        self.assertEqual([m.version for m in migrations], list(range(1, len(migrations) + 1)))
        self.assertEqual(migrations[0].label, "0001_providers_and_jobs")
        for migration in migrations:
            self.assertNotRegex(migration.sql.upper(), r"\b(BEGIN|COMMIT|ROLLBACK)\s*;")

    def test_migration_files_must_be_numbered_without_gaps(self):
        for names, message in (
            (["0001_a.sql", "0003_c.sql"], "without gaps"),
            (["0001_a.sql", "2_b.sql"], "must be named"),
        ):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                for name in names:
                    Path(directory, name).write_text("SELECT 1;")
                with self.assertRaisesRegex(MigrationError, message):
                    load_migrations(Path(directory))


class _Aborted(Exception):
    pass


class _Context:
    def __init__(self):
        self.status = None
        self.message = None
        self.trailing = ()

    def invocation_metadata(self):
        return ()

    def set_trailing_metadata(self, metadata):
        self.trailing = metadata

    def abort(self, status, message):
        self.status, self.message = status, message
        raise _Aborted

    def code(self):
        return self.status

    def details(self):
        return self.message


class UnavailableDatabaseTests(unittest.TestCase):
    def test_submission_is_refused_before_any_side_effect(self):
        backend = mock.Mock()
        jobs = mock.Mock()
        jobs.begin.side_effect = DatabaseUnavailableError("down")
        service = ExecutionService(backend, executor="slurm", jobs=jobs)
        for call, request in (
            (service.SubmitJob, execution_pb2.SubmitJobRequest(workdir="/w", script="a.sbatch")),
            (service.Execute, execution_pb2.ExecuteRequest(payload=b"x")),
        ):
            with self.subTest(rpc=call.__name__):
                context = _Context()
                with self.assertRaises(_Aborted):
                    call(request, context)
                self.assertEqual(context.status, grpc.StatusCode.UNAVAILABLE)
                self.assertIn((ERROR_KIND_METADATA, "database_unavailable"), context.trailing)
        backend.submit.assert_not_called()
        backend.submit_payload.assert_not_called()
        backend.prepare_environment_bytes.assert_not_called()


class MigrationTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        self.conn = connect(self.url)
        self.addCleanup(self.conn.close)

    def migrations(self, *bodies: str):
        directory = Path(tempfile.mkdtemp())
        for version, body in enumerate(bodies, start=1):
            (directory / f"{version:04d}_step_{version}.sql").write_text(body)
        return load_migrations(directory)

    def tables(self):
        rows = self.conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        return {row[0] for row in rows}

    def test_upgrade_applies_once_and_verify_passes(self):
        with self.assertRaisesRegex(MigrationError, "run `nest-db upgrade`"):
            verify(self.conn)
        applied = upgrade(self.conn)
        self.assertEqual([m.version for m in applied], [m.version for m in load_migrations()])
        self.assertEqual(upgrade(self.conn), [])
        verify(self.conn)
        self.assertTrue({"schema_migrations", "provider_profiles", "jobs"} <= self.tables())
        rows = self.conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        self.assertEqual(rows, [(m.version, m.checksum) for m in load_migrations()])

    def test_pending_migrations_fail_verification(self):
        first = self.migrations("CREATE TABLE a (id int);")
        upgrade(self.conn, first)
        both = self.migrations("CREATE TABLE a (id int);", "CREATE TABLE b (id int);")
        with self.assertRaisesRegex(MigrationError, "at version 1 but this Nest needs 2"):
            verify(self.conn, both)
        self.assertEqual([m.version for m in upgrade(self.conn, both)], [2])
        verify(self.conn, both)

    def test_applied_migrations_are_immutable(self):
        upgrade(self.conn, self.migrations("CREATE TABLE a (id int);"))
        edited = self.migrations("CREATE TABLE a (id bigint);")
        with self.assertRaisesRegex(MigrationError, "changed after it was applied"):
            verify(self.conn, edited)
        with self.assertRaisesRegex(MigrationError, "changed after it was applied"):
            upgrade(self.conn, edited + self.migrations("", "CREATE TABLE b (id int);")[1:])
        self.assertNotIn("b", self.tables())

    def test_a_newer_database_is_rejected(self):
        upgrade(self.conn, self.migrations("SELECT 1;", "SELECT 2;"))
        older = self.migrations("SELECT 1;")
        with self.assertRaisesRegex(MigrationError, "which this Nest version does not have"):
            verify(self.conn, older)
        with self.assertRaisesRegex(MigrationError, "does not have"):
            upgrade(self.conn, older)

    def test_failed_migration_rolls_back_completely(self):
        migrations = self.migrations(
            "CREATE TABLE a (id int);",
            "CREATE TABLE b (id int); INSERT INTO b VALUES (1); SELECT 1 / 0;",
        )
        with self.assertRaisesRegex(MigrationError, "0002_step_2 failed and was rolled back"):
            upgrade(self.conn, migrations)
        self.assertIn("a", self.tables())
        self.assertNotIn("b", self.tables())
        status = schema_status(self.conn, migrations)
        self.assertEqual(sorted(status.applied), [1])
        self.assertEqual([m.version for m in status.pending], [2])
        # The lock was released, so a fixed chain can be applied.
        lock = self.conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
        ).fetchone()
        self.assertEqual(lock, (0,))

    def test_concurrent_upgrades_apply_each_migration_once(self):
        migrations = self.migrations(
            "SELECT pg_sleep(0.5); CREATE TABLE a (id int);", "CREATE TABLE b (id int);"
        )
        results, errors = [], []

        def run():
            try:
                with connect(self.url) as conn:
                    results.append([m.version for m in upgrade(conn, migrations)])
            except (MigrationError, OSError) as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [[], [], [1, 2]])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM schema_migrations").fetchone(), (2,)
        )

    def test_upgrade_waits_for_the_lock_then_gives_up(self):
        with connect(self.url) as holder:
            holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
            started = time.monotonic()
            with self.assertRaisesRegex(MigrationError, "Another migration holds the lock"):
                upgrade(self.conn, lock_timeout=0.5)
            self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertNotIn("schema_migrations", self.tables())


def _db_config(url: str, **settings: str):
    return load_database_config({"NEST_DATABASE_URL": url, **settings})


class StoreTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = Database(_db_config(self.url, NEST_DATABASE_POOL_MAX_SIZE="2"))
        self.db.open()
        self.addCleanup(self.db.close)

    def test_provider_profiles_follow_the_environment(self):
        config = load_controller_config(
            {
                "NEST_PROVIDERS": "hpc,gpu",
                "NEST_DEFAULT_PROVIDER": "gpu",
                "NEST_PROVIDER_HPC_LOGIN_NODES": "login1,login2:2222",
                "NEST_PROVIDER_HPC_ACCOUNT": "acct",
                "NEST_PROVIDER_GPU_LOGIN_NODES": "gpu-login",
                "NEST_PROVIDER_GPU_ACCOUNT": "gpu-acct",
            }
        )
        sync_providers(self.db, config.providers, config.default_provider)
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, login_nodes, is_default, active FROM provider_profiles ORDER BY id"
            ).fetchall()
        self.assertEqual(
            rows,
            [("gpu", ["gpu-login"], True, True), ("hpc", ["login1", "login2:2222"], False, True)],
        )
        only_hpc = {"hpc": config.providers["hpc"]}
        sync_providers(self.db, only_hpc, "hpc")
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, is_default, active FROM provider_profiles ORDER BY id"
            ).fetchall()
        self.assertEqual(rows, [("gpu", False, False), ("hpc", True, True)])

    def test_job_lifecycle_is_recorded(self):
        config = load_controller_config(
            {"NEST_SLURM_HOST": "hpc", "NEST_SLURM_ACCOUNT": "acct", "NEST_SLURM_WORKDIR": "/w"}
        )
        sync_providers(self.db, config.providers, config.default_provider)
        jobs = JobStore(self.db, "default")
        record = jobs.begin("execution", request_id="r1", gpu=1, cpu=4)
        self.assertEqual(jobs.get(record)["status"], "submitting")  # ty: ignore[not-subscriptable]
        jobs.submitted(record, 42, "/w/jobs/x")
        jobs.observe(42, "RUNNING")
        jobs.finish(record, "succeeded")
        row = jobs.find(42)
        assert row is not None
        self.assertEqual(
            (row["status"], row["slurm_state"], row["job_dir"], row["gpu"], row["cpu"]),
            ("succeeded", "RUNNING", "/w/jobs/x", 1, 4),
        )
        self.assertIsNotNone(row["finished_at"])

    def test_closed_pool_refuses_new_records(self):
        jobs = JobStore(self.db, "default")
        self.db.close()
        with self.assertRaises(DatabaseUnavailableError):
            jobs.begin("batch")
        # Updates after a side effect are logged, never raised.
        jobs.finish(1, "failed")


class ControllerDatabaseTests(PostgresTestCase):
    def exit_status(self, **settings):
        result = subprocess.run(
            [sys.executable, "-m", "nest.controller"],
            env=controller_environment(NEST_CONTROLLER_BIND="127.0.0.1:0", **settings),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode, result.stderr

    def test_unmigrated_database_stops_startup(self):
        url = self.database()
        code, stderr = self.exit_status(NEST_DATABASE_URL=url)
        self.assertEqual(code, 3)
        self.assertIn("run `nest-db upgrade`", stderr)

    def test_unreachable_database_stops_startup(self):
        code, stderr = self.exit_status(
            NEST_DATABASE_URL=f"postgresql://nest:{PASSWORD}@127.0.0.1:1/nest",
            NEST_DATABASE_CONNECT_TIMEOUT="2",
        )
        self.assertEqual(code, 3)
        self.assertIn("Cannot connect to PostgreSQL at postgresql://nest:***@127.0.0.1:1", stderr)

    def test_wrong_password_stops_startup_without_echoing_it(self):
        parts = urlsplit(self.database())
        code, stderr = self.exit_status(
            NEST_DATABASE_URL=f"postgresql://nest_no_such_user:{PASSWORD}@{parts.hostname}:"
            f"{parts.port}{parts.path}"
        )
        self.assertEqual(code, 3)
        self.assertIn("nest_no_such_user", stderr)

    def test_required_tls_is_enforced(self):
        url = self.database()
        with connect(url) as conn:
            ssl = conn.execute("SHOW ssl").fetchone()
        if ssl == ("on",):
            self.skipTest("test server accepts TLS")
        code, stderr = self.exit_status(NEST_DATABASE_URL=url, NEST_DATABASE_SSLMODE="require")
        self.assertEqual(code, 3)
        self.assertIn("SSL", stderr)

    def test_required_database_without_url_is_a_config_error(self):
        code, stderr = self.exit_status(NEST_DATABASE_REQUIRED="true")
        self.assertEqual(code, 2)
        self.assertIn("NEST_DATABASE_URL is unset", stderr)

    def test_records_survive_a_controller_restart(self):
        from fake_hpc import FakeHpc

        url = self.database()
        with connect(url) as conn:
            upgrade(conn)
        root = Path(tempfile.mkdtemp())
        hpc = FakeHpc(root)
        workdir = root / "batch"
        workdir.mkdir()
        (workdir / "hello.sbatch").write_text("#!/bin/bash\necho hello > hello.out\n")
        settings = {**hpc.controller_settings(), "NEST_DATABASE_URL": url}

        with controller_at_free_port(**settings) as (_, stub, _address):
            submitted = stub.SubmitJob(
                execution_pb2.SubmitJobRequest(workdir=str(workdir), script="hello.sbatch"),
                timeout=30,
            )
            payload = cloudpickle.dumps((lambda value: value * 2, (21,), {}))
            response = stub.Execute(execution_pb2.ExecuteRequest(payload=payload), timeout=60)
            self.assertEqual(cloudpickle.loads(response.result), 42)

        with connect(url) as conn:
            before = conn.execute(
                "SELECT id, kind, status, slurm_job_id FROM jobs ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [row[1:3] for row in before], [("batch", "submitted"), ("execution", "succeeded")]
        )
        self.assertEqual(before[0][3], submitted.job_id)

        # A new Controller process, with a changed profile, sees the same records.
        with controller_at_free_port(**settings, NEST_SLURM_PARTITION="long") as (_, stub, _a):
            state = stub.GetJobStatus(
                execution_pb2.JobStatusRequest(job_id=submitted.job_id), timeout=30
            ).state
        self.assertEqual(state, "COMPLETED")

        with connect(url) as conn:
            after = conn.execute(
                "SELECT id, kind, status, slurm_job_id FROM jobs ORDER BY id"
            ).fetchall()
            observed = conn.execute(
                "SELECT slurm_state FROM jobs WHERE id = %s", (before[0][0],)
            ).fetchone()
            profile = conn.execute(
                "SELECT partition, is_default, active FROM provider_profiles WHERE id = 'default'"
            ).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(observed, ("COMPLETED",))
        self.assertEqual(profile, ("long", True, True))


class NestDbCommandTests(PostgresTestCase):
    def nest_db(self, *args, **settings):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NEST_DATABASE_")}
        env.update(settings)
        return subprocess.run(
            [sys.executable, "-m", "nest.db.cli", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_upgrade_verify_and_status(self):
        url = self.database()
        result = self.nest_db("verify", NEST_DATABASE_URL=url)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("nest-db upgrade", result.stderr)
        result = self.nest_db("upgrade", NEST_DATABASE_URL=url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("applied 0001_providers_and_jobs", result.stdout)
        self.assertIn("already current", self.nest_db("upgrade", NEST_DATABASE_URL=url).stdout)
        result = self.nest_db("verify", NEST_DATABASE_URL=url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Schema is current at version", result.stdout)
        result = self.nest_db("status", NEST_DATABASE_URL=url)
        self.assertIn("applied  0001_providers_and_jobs", result.stdout)

        with connect(url) as conn:
            conn.execute("UPDATE schema_migrations SET checksum = 'x' WHERE version = 1")
        result = self.nest_db("verify", NEST_DATABASE_URL=url)
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertIn("MODIFIED", self.nest_db("status", NEST_DATABASE_URL=url).stdout)

    def test_configuration_and_connection_errors(self):
        result = self.nest_db("verify")
        self.assertEqual(result.returncode, 2)
        self.assertIn("NEST_DATABASE_URL is required", result.stderr)
        result = self.nest_db(
            "upgrade",
            NEST_DATABASE_URL=f"postgresql://nest:{PASSWORD}@127.0.0.1:1/nest",
            NEST_DATABASE_CONNECT_TIMEOUT="2",
        )
        self.assertEqual(result.returncode, 3)
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)


class PublicApiTests(unittest.TestCase):
    def test_database_error_is_exported(self):
        import nest.db

        self.assertTrue(issubclass(nest.db.MigrationError, nest.db.DatabaseError))


if __name__ == "__main__":
    unittest.main()
