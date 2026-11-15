"""PostgreSQL persistence: configuration, migrations, `nest-db`, and the Controller.

Configuration tests need no server. The others run against a real PostgreSQL named
by `NEST_TEST_DATABASE_URL` (see `postgres_fixture`). These are `unittest` cases so
they also run inside the Controller image, where PostgreSQL tests skip.
"""

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import cloudpickle
import grpc
from postgres_fixture import (
    ADMIN_URL,
    PG_BIN,
    REQUIRED,
    TLS_CA_FILE,
    TLS_OTHER_CA_FILE,
    TLS_URL,
    PostgresTestCase,
    PostgresTLSTestCase,
    connect,
    temporary_database,
)
from psycopg import sql
from psycopg_pool import PoolTimeout
from test_controller import controller_at_free_port, controller_environment

from nest.config import ConfigError, load_controller_config, load_database_config
from nest.controller import ExecutionService
from nest.db import (
    Database,
    DatabaseError,
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
        text = self.problems({"NEST_DATABASE_URL": url + "?sslmode=strict-marker"})
        self.assertIn("invalid sslmode in its query string", text)
        self.assertNotIn("strict-marker", text)
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


class PoolOpenFailureTests(unittest.TestCase):
    """A failed pool open leaves no scrub filter behind on `psycopg.pool`."""

    def setUp(self):
        self.pool_logger = logging.getLogger("psycopg.pool")
        before = list(self.pool_logger.filters)
        self.addCleanup(lambda: setattr(self.pool_logger, "filters", before))
        self.config = _db_config("postgresql://nest@db.internal/nest")

    def open_with_pool(self, pool_class):
        database = Database(self.config)
        with (
            mock.patch.object(Database, "connect"),  # the direct probe succeeds
            mock.patch.object(Database, "warn_if_unencrypted"),
            mock.patch("nest.db.connection.ConnectionPool", pool_class),
        ):
            database.open()

    def scrub_filters(self):
        return [f for f in self.pool_logger.filters if type(f).__name__ == "_ScrubFilter"]

    def test_repeated_pool_failures_do_not_accumulate_filters(self):
        failures = (
            ("timeout", PoolTimeout("pool timed out"), DatabaseError),
            ("other error", RuntimeError("pool exploded"), RuntimeError),
        )
        for label, error, raised in failures:
            with self.subTest(failure=label):
                pool_class = mock.Mock()
                pool_class.return_value.open.side_effect = error
                for _ in range(3):
                    with self.assertRaises(raised):
                        self.open_with_pool(pool_class)
                self.assertEqual(self.scrub_filters(), [])
                # The half-opened pool is closed on every failure.
                self.assertEqual(pool_class.return_value.close.call_count, 3)

    def test_failure_creating_the_pool_removes_the_filter(self):
        pool_class = mock.Mock(side_effect=ValueError("bad pool arguments"))
        with self.assertRaises(ValueError):
            self.open_with_pool(pool_class)
        self.assertEqual(self.scrub_filters(), [])

    def test_successful_open_keeps_one_filter_until_close(self):
        database = Database(self.config)
        pool_class = mock.Mock()
        with (
            mock.patch.object(Database, "connect"),
            mock.patch.object(Database, "warn_if_unencrypted"),
            mock.patch("nest.db.connection.ConnectionPool", pool_class),
        ):
            database.open()
        self.assertEqual(len(self.scrub_filters()), 1)
        database.close()
        self.assertEqual(self.scrub_filters(), [])


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

    def test_renaming_an_applied_migration_is_detected(self):
        directory = Path(tempfile.mkdtemp())
        (directory / "0001_original_name.sql").write_text("CREATE TABLE a (id int);")
        original = load_migrations(directory)
        upgrade(self.conn, original)
        (directory / "0001_original_name.sql").rename(directory / "0001_new_name.sql")
        renamed = load_migrations(directory)
        # Same version and SQL, so the checksum alone would not notice.
        self.assertEqual(renamed[0].checksum, original[0].checksum)
        status = schema_status(self.conn, renamed)
        self.assertEqual([m.label for m in status.renamed], ["0001_new_name"])
        self.assertEqual(status.modified, [])
        message = "0001_original_name was renamed to 0001_new_name after it was applied"
        with self.assertRaisesRegex(MigrationError, message):
            verify(self.conn, renamed)
        (directory / "0002_next.sql").write_text("CREATE TABLE b (id int);")
        with self.assertRaisesRegex(MigrationError, message):
            upgrade(self.conn, load_migrations(directory))
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
        execution_id = "c" * 32
        record = jobs.begin(
            "execution",
            execution_id,
            request_id="r1",
            job_dir="/w/jobs/" + execution_id,
            gpu=1,
            cpu=4,
        )
        row = jobs.by_execution_id(execution_id)
        assert row is not None
        self.assertEqual((row["id"], row["status"]), (record, "submitting"))
        self.assertEqual(row["job_dir"], "/w/jobs/" + execution_id)
        # Execution IDs are unique and must be 32 lowercase hexadecimal characters.
        for bad in (execution_id, "not-an-execution-id"):
            with self.subTest(execution_id=bad), self.assertRaises(DatabaseUnavailableError):
                jobs.begin("batch", bad)
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
            jobs.begin("batch", "b" * 32)
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

    def slurm_controller_settings(self):
        """Returns a migrated database URL, a FakeHpc, and Controller settings."""
        from fake_hpc import FakeHpc

        url = self.database()
        with connect(url) as conn:
            upgrade(conn)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        hpc = FakeHpc(root)
        return url, hpc, {**hpc.controller_settings(), "NEST_DATABASE_URL": url}

    @staticmethod
    def worker_jobs(hpc):
        return [c for c in hpc.calls("sbatch") if c["script"] == "project/scripts/worker.sbatch"]

    @staticmethod
    def job_rows(url):
        from psycopg.rows import dict_row

        with connect(url) as conn, conn.cursor(row_factory=dict_row) as cursor:
            return cursor.execute("SELECT * FROM jobs ORDER BY id").fetchall()

    def test_execution_id_is_shared_by_record_backend_and_job_directory(self):
        url, hpc, settings = self.slurm_controller_settings()
        with controller_at_free_port(**settings) as (_, stub, _address):
            payload = cloudpickle.dumps((lambda value: value * 2, (21,), {}))
            response = stub.Execute(execution_pb2.ExecuteRequest(payload=payload), timeout=60)
            self.assertEqual(cloudpickle.loads(response.result), 42)
        [job] = self.worker_jobs(hpc)
        [row] = self.job_rows(url)
        execution_id = job["args"][0]
        self.assertRegex(row["execution_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(row["execution_id"], execution_id)
        self.assertEqual(row["job_dir"], str(hpc.base / "jobs" / execution_id))
        self.assertTrue((hpc.base / "jobs" / execution_id / "input.pkl").exists())
        self.assertEqual((row["status"], row["slurm_job_id"]), ("succeeded", job["job_id"]))

    def test_ambiguous_submission_record_names_its_execution_directory(self):
        url, hpc, settings = self.slurm_controller_settings()
        hpc.hang_ssh("sbatch --parsable", after=True)
        with (
            controller_at_free_port(**settings, NEST_SSH_TIMEOUT="1") as (_, stub, _a),
            self.assertRaises(grpc.RpcError) as caught,
        ):
            stub.Execute(
                execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((print, (), {}))),
                timeout=60,
            )
        error = caught.exception
        self.assertEqual(error.code(), grpc.StatusCode.UNKNOWN)  # ty: ignore[missing-attribute]
        # sbatch ran once and was never retried; the Controller got no job ID.
        self.assertEqual(len(hpc.calls("sbatch")), 1)
        [job] = self.worker_jobs(hpc)
        [row] = self.job_rows(url)
        execution_id = job["args"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["error_kind"], "submission_unknown")
        self.assertIsNone(row["slurm_job_id"])
        # The record alone is enough to find the execution directory and its output.
        self.assertEqual(row["execution_id"], execution_id)
        self.assertEqual(row["job_dir"], str(hpc.base / "jobs" / execution_id))
        self.assertTrue((hpc.base / "jobs" / execution_id / "input.pkl").exists())
        self.assertIn(f"Execution ID: {execution_id}", error.details())  # ty: ignore[missing-attribute]

    def test_failure_before_submission_is_recorded_without_a_slurm_job(self):
        url, hpc, settings = self.slurm_controller_settings()
        with (
            controller_at_free_port(**{**settings, "NEST_SLURM_HOST": "unknown-hpc"}) as (
                _,
                stub,
                _a,
            ),
            self.assertRaises(grpc.RpcError),
        ):
            stub.Execute(
                execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((print, (), {}))),
                timeout=60,
            )
        self.assertEqual(hpc.calls("sbatch"), [])
        [row] = self.job_rows(url)
        self.assertEqual((row["status"], row["error_kind"]), ("failed", "ssh_error"))
        self.assertIsNone(row["slurm_job_id"])
        self.assertRegex(row["execution_id"], r"^[0-9a-f]{32}$")

    def test_records_survive_a_controller_restart(self):
        url, hpc, settings = self.slurm_controller_settings()
        root = hpc.base.parent
        workdir = root / "batch"
        workdir.mkdir()
        (workdir / "hello.sbatch").write_text("#!/bin/bash\necho hello > hello.out\n")

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


class QueryValueRedactionTests(PostgresTestCase):
    """URL passwords and query values never reach errors, `nest-db`, or Controller logs."""

    CASES = (
        # (query string, marker that must never appear, failing stage)
        ("sslmode=sslmode-marker-1", "sslmode-marker-1", "config"),
        ("keepalives=keepalive-marker-2", "keepalive-marker-2", "connect"),
        ("target_session_attrs=tsa-marker-3", "tsa-marker-3", "connect"),
        ("gssencmode=gss-marker-4", "gss-marker-4", "connect"),
        ("options=-coptparam-marker-5%3Dvalue-marker-6", "marker-5", "connect"),
        ("options=-coptparam-marker-5%3Dvalue-marker-6", "marker-6", "connect"),
        ("host=host-marker-7.invalid", "host-marker-7", "connect"),
    )

    def setUp(self):
        super().setUp()
        # A throwaway login role whose password is PASSWORD, so marker URLs carry a
        # password that assert_clean can look for and that a server checking
        # passwords (such as the postgres image's default scram-sha-256) accepts.
        # Its cleanup is registered before the database's, so it runs after the
        # database (and everything the role owns in it) is dropped.
        self.role = f"nest_redact_{uuid.uuid4().hex[:12]}"
        with connect(ADMIN_URL) as admin:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(self.role), sql.Literal(PASSWORD)
                )
            )
        self.addCleanup(self._drop_role)
        self.url = self.database()
        with connect(ADMIN_URL) as admin:
            admin.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(urlsplit(self.url).path.strip("/")),
                    sql.Identifier(self.role),
                )
            )

    def _drop_role(self):
        with connect(ADMIN_URL) as admin:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(self.role)))

    def marker_url(self, query: str = "") -> str:
        parts = urlsplit(self.url)
        url = f"postgresql://{self.role}:{PASSWORD}@{parts.hostname}:{parts.port}{parts.path}"
        return f"{url}?{query}" if query else url

    def run_entry_point(self, module, *args, url):
        env = controller_environment(NEST_CONTROLLER_BIND="127.0.0.1:0")
        env.update(NEST_DATABASE_URL=url, NEST_DATABASE_CONNECT_TIMEOUT="3")
        result = subprocess.run(
            [sys.executable, "-m", module, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    def assert_clean(self, text, marker):
        self.assertNotIn(marker, text)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("Traceback", text)

    def test_markers_never_appear_in_errors_or_output(self):
        for query, marker, stage in self.CASES:
            url = self.marker_url(query)
            with self.subTest(query=query, marker=marker):
                if stage == "config":
                    with self.assertRaises(ConfigError) as caught:
                        load_controller_config({"NEST_DATABASE_URL": url})
                    self.assert_clean(str(caught.exception), marker)
                    self.assertIn("NEST_DATABASE_URL", str(caught.exception))
                else:
                    config = _db_config(url, NEST_DATABASE_CONNECT_TIMEOUT="3")
                    self.assert_clean(str(config.summary()), marker)
                    with self.assertRaises(DatabaseError) as caught:
                        Database(config).open()
                    self.assert_clean(str(caught.exception), marker)
                    # Still actionable: names the redacted URL and what to check.
                    self.assertIn(
                        "Cannot connect to PostgreSQL at postgresql://", str(caught.exception)
                    )
                expected = 2 if stage == "config" else 3
                code, output = self.run_entry_point("nest.db.cli", "verify", url=url)
                self.assertEqual(code, expected, output)
                self.assert_clean(output, marker)
                code, output = self.run_entry_point("nest.controller", url=url)
                self.assertEqual(code, expected, output)
                self.assert_clean(output, marker)

    def test_successful_startup_logs_no_query_values(self):
        # Migrate as the role the Controller connects as, so it owns the schema.
        with connect(self.marker_url()) as conn:
            upgrade(conn)
        url = self.marker_url("application_name=app-marker-8&keepalives_idle=30")
        env = controller_environment(NEST_CONTROLLER_BIND="127.0.0.1:0", NEST_DATABASE_URL=url)
        process = subprocess.Popen(
            [sys.executable, "-m", "nest.controller"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                if "Controller listening" in line:
                    break
        finally:
            process.terminate()
            rest, _ = process.communicate(timeout=15)
        output = "".join(lines) + rest
        self.assertIn("Database connected url=postgresql://", output)
        self.assertIn("?***", output)
        self.assert_clean(output, "app-marker-8")


class TLSTests(PostgresTLSTestCase):
    """`NEST_DATABASE_SSL*` against a real TLS-enabled PostgreSQL."""

    def tls_url(self, host: str = "localhost") -> str:
        parts = urlsplit(TLS_URL)
        auth = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
        return f"postgresql://{auth}{host}:{parts.port or 5432}{parts.path}"

    def open(self, url: str, **settings: str) -> Database:
        database = Database(_db_config(url, NEST_DATABASE_CONNECT_TIMEOUT="5", **settings))
        database.open()
        self.addCleanup(database.close)
        return database

    def test_verify_full_checks_the_certificate_and_encrypts(self):
        database = self.open(self.tls_url(), NEST_DATABASE_SSLROOTCERT=TLS_CA_FILE)
        self.assertEqual(database.config.effective_sslmode, "verify-full")
        with database.connection() as conn:
            row = conn.execute(
                "SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
            ).fetchone()
        assert row is not None
        self.assertTrue(row[0])
        self.assertTrue(row[1].startswith("TLS"))

    def test_verify_full_rejects_an_untrusted_certificate(self):
        with self.assertRaisesRegex(DatabaseError, "certificate verify failed"):
            self.open(self.tls_url(), NEST_DATABASE_SSLROOTCERT=TLS_OTHER_CA_FILE)

    def test_verify_full_rejects_a_host_name_mismatch(self):
        # The certificate names only DNS:localhost, so 127.0.0.1 does not match.
        url = self.tls_url("127.0.0.1")
        with self.assertRaisesRegex(DatabaseError, "does not match host name"):
            self.open(url, NEST_DATABASE_SSLROOTCERT=TLS_CA_FILE)
        # verify-ca still checks the CA but not the host name.
        self.open(url, NEST_DATABASE_SSLROOTCERT=TLS_CA_FILE, NEST_DATABASE_SSLMODE="verify-ca")

    def test_controller_and_nest_db_use_the_tls_settings(self):
        env = controller_environment(
            NEST_CONTROLLER_BIND="127.0.0.1:0",
            NEST_DATABASE_URL=self.tls_url(),
            NEST_DATABASE_SSLROOTCERT=TLS_OTHER_CA_FILE,
            NEST_DATABASE_CONNECT_TIMEOUT="5",
        )
        for module, args in (("nest.controller", ()), ("nest.db.cli", ("verify",))):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, *args],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("certificate verify failed", result.stderr)


def _pg_tool(name: str) -> str | None:
    import shutil

    path = os.path.join(PG_BIN, name) if PG_BIN else shutil.which(name)
    return path if path and os.access(path, os.X_OK) else None


class BackupRestoreTests(PostgresTestCase):
    """The documented pg_dump / pg_restore / nest-db verify workflow."""

    def setUp(self):
        super().setUp()
        self.pg_dump, self.pg_restore = _pg_tool("pg_dump"), _pg_tool("pg_restore")
        if not (self.pg_dump and self.pg_restore):
            message = "pg_dump and pg_restore are needed (set NEST_TEST_PG_BIN)"
            if REQUIRED:
                self.fail(message)
            self.skipTest(message)

    def test_dump_and_restore_preserve_records_and_schema(self):
        source = self.database()
        with connect(source) as conn:
            upgrade(conn)
        database = Database(_db_config(source))
        database.open()
        self.addCleanup(database.close)
        config = load_controller_config(
            {"NEST_SLURM_HOST": "hpc", "NEST_SLURM_ACCOUNT": "acct", "NEST_SLURM_WORKDIR": "/w"}
        )
        sync_providers(database, config.providers, config.default_provider)
        jobs = JobStore(database, "default")
        record = jobs.begin("execution", "d" * 32, job_dir="/w/jobs/" + "d" * 32, gpu=1)
        jobs.submitted(record, 77, "/w/jobs/" + "d" * 32)
        jobs.finish(record, "succeeded")
        jobs.begin("batch", "e" * 32, script="hello.sbatch")
        database.close()

        def snapshot(url):
            with connect(url) as conn:
                return (
                    conn.execute(
                        "SELECT id, execution_id, kind, status, slurm_job_id, job_dir, gpu "
                        "FROM jobs ORDER BY id"
                    ).fetchall(),
                    conn.execute(
                        "SELECT id, login_nodes, account, is_default FROM provider_profiles"
                    ).fetchall(),
                    conn.execute(
                        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                    ).fetchall(),
                )

        expected = snapshot(source)
        dump = Path(tempfile.mkdtemp()) / "nest.dump"
        self.addCleanup(lambda: __import__("shutil").rmtree(dump.parent))
        assert self.pg_dump and self.pg_restore
        subprocess.run(
            [self.pg_dump, "--format=custom", "--no-owner", f"--file={dump}", source],
            check=True,
            capture_output=True,
        )
        with temporary_database() as restored:
            subprocess.run(
                [self.pg_restore, "--no-owner", "--exit-on-error", f"--dbname={restored}", dump],
                check=True,
                capture_output=True,
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith("NEST_DATABASE_")}
            result = subprocess.run(
                [sys.executable, "-m", "nest.db.cli", "verify"],
                env={**env, "NEST_DATABASE_URL": restored},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(restored), expected)
            # New records continue after the restored ones.
            with connect(restored) as conn:
                conn.execute(
                    "INSERT INTO jobs (execution_id, provider_id, kind) "
                    "VALUES (%s, 'default', 'batch')",
                    ("f" * 32,),
                )
                ids = [row[0] for row in conn.execute("SELECT id FROM jobs ORDER BY id")]
        self.assertEqual(len(expected[0]), 2)
        self.assertEqual(ids[-1], max(row[0] for row in expected[0]) + 1)


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

    def test_verify_and_upgrade_both_reject_a_renamed_migration(self):
        url = self.database()
        self.assertEqual(self.nest_db("upgrade", NEST_DATABASE_URL=url).returncode, 0)
        with connect(url) as conn:
            conn.execute("UPDATE schema_migrations SET name = 'old_name' WHERE version = 1")
        for command in ("verify", "upgrade"):
            with self.subTest(command=command):
                result = self.nest_db(command, NEST_DATABASE_URL=url)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("0001_old_name was renamed to 0001_providers_and_jobs", result.stderr)
        self.assertIn("RENAMED", self.nest_db("status", NEST_DATABASE_URL=url).stdout)

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
