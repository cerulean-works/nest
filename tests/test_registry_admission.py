"""Registry-backed Controller submissions (C01 Part 2).

Every test serves the real gRPC `ExecutionService`, with session authentication and
`RegistryAdmission`, in process against a real PostgreSQL (`NEST_TEST_DATABASE_URL`,
see `postgres_fixture`) and the `FakeHpc` login node, whose `ssh` and `sbatch` shims
record every command. `ControllerProcessTests` start the real Controller process
with `NEST_EXECUTION_REGISTRY=required` instead.

The central assertion: an execution and its `submitting` attempt are committed,
as seen from another connection, before `sbatch` runs; and whenever admission or
the database fails, `sbatch` never runs at all.
"""

import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from concurrent import futures
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest import mock

import cloudpickle
import grpc
import psycopg
from admin_fixture import owned_project
from fake_hpc import FakeHpc
from postgres_fixture import PostgresTestCase, connect
from psycopg.rows import dict_row

from nest.admission import (
    ControllerIdentitySource,
    RecoverySweeper,
    RegistryAdmission,
    SubmissionRequest,
    TrustedHPCIdentity,
)
from nest.audit import AuditLog
from nest.authz import AllowedProjects, ExecutionAccess, HPCIdentity, Principal, ProjectService
from nest.backends.slurm import SlurmBackend, SlurmConfig
from nest.config import (
    ConfigError,
    LoginNode,
    PlatformProfile,
    load_controller_config,
    load_database_config,
)
from nest.controller import ExecutionService, build_admission
from nest.db import (
    Database,
    DatabaseError,
    ExecutionRegistry,
    ExecutionRegistryError,
    HPCPrincipal,
    IdentityStore,
    JobStore,
    PlatformStore,
    SessionStore,
    SlurmAccountStore,
    hash_token,
    sync_platforms,
    upgrade,
)
from nest.db.cli import main as nest_db
from nest.proto import execution_pb2, execution_pb2_grpc

OPS: dict[str, Any] = {"operator": "ops-test", "reason": "registry admission tests"}
PIN = "SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"


def browser(user_id: uuid.UUID) -> Principal:
    return Principal(user_id, "browser")


class _Base(PostgresTestCase):
    """A served Mode A Platform `nano`, granted to Project `alpha` with account `proj`."""

    ssh_timeout = 10.0

    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = self.open_database()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.hpc = FakeHpc(root)
        settings = self.hpc.controller_settings()
        patcher = mock.patch.dict(
            os.environ, {k: v for k, v in settings.items() if k.startswith(("PATH", "FAKE_"))}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.identities = IdentityStore(self.db)
        self.sessions = SessionStore(self.db)
        self.audit = AuditLog(self.db)
        projects = ProjectService(self.db)
        self.projects = projects
        self.owner, self.member, self.outsider = (
            self.identities.create_user(display_name=n).id for n in ("owner", "member", "out")
        )
        self.project = owned_project(projects, self.owner, "alpha").id
        projects.add_member(browser(self.owner), self.project, self.member, "member")
        self.other = owned_project(projects, self.outsider, "beta").id

        self.profile = PlatformProfile(
            id="nano",
            ssh_mode="A",
            login_nodes=(LoginNode(FakeHpc.host),),
            account="legacy-acct",
            partition="dev",
            time="00:05:00",
            workdir=str(self.hpc.base),
            ssh_user="nestop",
        )
        sync_platforms(self.db, {"nano": self.profile}, "nano")
        self.platforms = PlatformStore(self.db)
        self.platform_id = self.platforms.sync_environment({"nano": self.profile})["nano"]
        for project in (self.project, self.other):
            self.platforms.grant_project(self.platform_id, project, **OPS)
        self.accounts = SlurmAccountStore(self.db)
        self.accounts.register_account(self.platform_id, "proj", **OPS)
        self.accounts.register_account(self.platform_id, "spare", **OPS)
        self.accounts.link_project(
            self.project, self.platform_id, "proj", modes=["A"], default=True, **OPS
        )
        self.accounts.link_project(self.project, self.platform_id, "spare", modes=["A"], **OPS)
        self.accounts.link_project(
            self.other, self.platform_id, "proj", modes=["A"], default=True, **OPS
        )

        self.backend = SlurmBackend(
            SlurmConfig(
                ssh_host=FakeHpc.host,
                account="legacy-acct",
                partition="dev",
                time="00:05:00",
                work_root=f"{self.hpc.base}/jobs",
                worker_workdir=str(self.hpc.base),
                ssh_user="nestop",
                allowed_hosts=(FakeHpc.host,),
                ssh_timeout=self.ssh_timeout,
                result_timeout=60,
                poll_interval=0.1,
            )
        )
        self.jobs = JobStore(self.db, "nano", platform_id=self.platform_id)
        self.access = ExecutionAccess(
            self.db,
            AllowedProjects([str(self.project), str(self.other)]),
            provider_id="nano",
            platform_id=self.platform_id,
            audit=self.audit,
        )
        self.admission = RegistryAdmission(
            self.db, self.backend, self.profile, self.platform_id, audit=self.audit
        )
        self.registry = ExecutionRegistry(self.db)
        self.stub = self.serve(self.admission)
        self.token_member = self.token(self.member)
        self.token_outsider = self.token(self.outsider)

    def open_database(self) -> Database:
        db = Database(
            load_database_config(
                {"NEST_DATABASE_URL": self.url, "NEST_DATABASE_POOL_MAX_SIZE": "8"}
            )
        )
        db.open()
        self.addCleanup(db.close)
        return db

    def serve(self, admission, backend=None):
        service = ExecutionService(
            backend or self.backend,
            executor="slurm",
            jobs=self.jobs,
            access=self.access,
            admission=admission,
        )
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        return execution_pb2_grpc.ExecutionServiceStub(channel)

    def token(self, user_id, scope="jobs:run"):
        session = self.identities.create_session(
            user_id,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=scope,
        )
        access = secrets.token_urlsafe(32)
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        return access

    def metadata(self, token=None, project=None, **extra):
        metadata = [
            ("authorization", f"Bearer {token or self.token_member}"),
            ("x-nest-project-id", str(project or self.project)),
        ]
        metadata.extend((k.replace("_", "-"), v) for k, v in extra.items())
        return metadata

    def batch_dir(self) -> str:
        workdir = self.hpc.base.parent / "batch"
        workdir.mkdir(exist_ok=True)
        (workdir / "hello.sbatch").write_text("#!/bin/bash\necho hello > hello.out\n")
        return str(workdir)

    def submit(self, stub=None, token=None, project=None, account="", partition="", **extra):
        return (stub or self.stub).SubmitJob(
            execution_pb2.SubmitJobRequest(
                workdir=self.batch_dir(),
                script="hello.sbatch",
                account=account,
                partition=partition,
                script_args=["a b"],
            ),
            timeout=60,
            metadata=self.metadata(token, project, **extra),
        )

    def execute(self, func=lambda: 42, token=None, project=None, **extra):
        response = self.stub.Execute(
            execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((func, (), {}))),
            timeout=60,
            metadata=self.metadata(token, project, **extra),
        )
        return cloudpickle.loads(response.result)

    def refused(self, call) -> grpc.Call:
        """The status of a refused call: its code, details and trailing metadata."""
        with self.assertRaises(grpc.RpcError) as caught:
            call()
        return cast(grpc.Call, caught.exception)

    def sql(self, query, *params):
        with connect(self.url) as conn, conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def attempts(self):
        return self.sql("SELECT * FROM execution_attempts ORDER BY id")

    def sbatch_calls(self):
        return self.hpc.calls("sbatch")

    def assert_nothing_sent(self):
        """No SSH command, let alone sbatch, reached the cluster."""
        self.assertEqual(self.hpc.calls("ssh"), [])
        self.assertEqual(self.sbatch_calls(), [])

    def spy_on_sbatch(self):
        """Records, at each sbatch, the attempts another connection sees committed."""
        seen = []
        original = SlurmBackend.submit

        def spy(backend, *args, **kwargs):
            seen.append(
                self.sql(
                    "SELECT a.status, a.slurm_job_id, e.status AS execution_status, "
                    "e.execution_id FROM execution_attempts a JOIN executions e "
                    "USING (execution_id) ORDER BY a.id"
                )
            )
            return original(backend, *args, **kwargs)

        patcher = mock.patch.object(SlurmBackend, "submit", autospec=True, side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen


class SubmissionPathTests(_Base):
    def test_execute_commits_its_attempt_before_sbatch(self):
        seen = self.spy_on_sbatch()
        self.assertEqual(self.execute(), 42)
        [worker] = self.sbatch_calls()
        [[committed]] = seen
        self.assertEqual(
            (committed["status"], committed["slurm_job_id"], committed["execution_status"]),
            ("submitting", None, "active"),
        )
        # The worker runs in the directory of the registered execution ID.
        self.assertEqual(worker["args"][0], committed["execution_id"])
        self.assertIn("--account=proj", worker["flags"])
        self.assertNotIn("--account=legacy-acct", worker["flags"])
        [attempt] = self.registry.attempts(committed["execution_id"], project_id=self.project)
        self.assertEqual(
            (attempt.status, attempt.slurm_job_id, attempt.slurm_state),
            ("succeeded", worker["job_id"], "COMPLETED"),
        )
        # Its own artifact directory, by the worker protocol pinned before sbatch.
        self.assertEqual(attempt.worker_protocol, 2)
        self.assertEqual(
            attempt.output_ref,
            f"{self.hpc.base}/jobs/{committed['execution_id']}/attempts/1/output.pkl",
        )
        self.assertEqual(
            (attempt.endpoint.host, attempt.endpoint.mode, attempt.endpoint.platform_id),
            (FakeHpc.host, "A", self.platform_id),
        )
        self.assertEqual(attempt.principal, HPCPrincipal(unix_user="nestop"))
        self.assertEqual(
            (attempt.slurm_account, attempt.slurm_account_origin), ("proj", "project_default")
        )
        self.assertRegex(attempt.source_digest or "", r"^sha256:[0-9a-f]{64}$")
        options = attempt.options
        self.assertEqual(
            (options["account"].origin, options["account"].default_source),
            ("default", "project"),
        )
        self.assertEqual(options["platform"].value, str(self.platform_id))
        self.assertEqual(options["platform"].default_source, "catalog")  # only eligible
        self.assertEqual((options["partition"].value, options["time"].value), ("dev", "00:05:00"))
        execution = self.registry.get(committed["execution_id"], project_id=self.project)
        assert execution is not None
        self.assertEqual((execution.owner_user_id, execution.status), (self.member, "succeeded"))
        # The registry is the only lifecycle authority: nothing is written to jobs.
        self.assertEqual(self.sql("SELECT * FROM jobs"), [])

    def test_submit_job_commits_its_attempt_before_sbatch_and_records_provenance(self):
        seen = self.spy_on_sbatch()
        response = self.submit(account="spare", partition="long")
        [call] = self.sbatch_calls()
        [[committed]] = seen
        self.assertEqual((committed["status"], committed["slurm_job_id"]), ("submitting", None))
        self.assertEqual(response.job_id, call["job_id"])
        self.assertIn("--account=spare", call["flags"])
        self.assertIn("--partition=long", call["flags"])
        self.assertEqual(call["args"], ["a b"])
        [attempt] = self.registry.attempts(committed["execution_id"], project_id=self.project)
        self.assertEqual((attempt.status, attempt.slurm_account_origin), ("submitted", "explicit"))
        partition = attempt.options["partition"]
        self.assertEqual(
            (partition.value, partition.origin, partition.default_value), ("long", "request", "dev")
        )
        self.assertEqual(attempt.options["script_args"].value, ["a b"])
        # A status query moves the attempt along, scoped by the attempt's Platform.
        state = self.stub.GetJobStatus(
            execution_pb2.JobStatusRequest(job_id=response.job_id),
            timeout=30,
            metadata=self.metadata(),
        ).state
        self.assertEqual(state, "COMPLETED")
        [attempt] = self.registry.attempts(committed["execution_id"], project_id=self.project)
        self.assertEqual((attempt.status, attempt.slurm_state), ("succeeded", "COMPLETED"))

    def test_client_cannot_assert_an_hpc_identity_or_entitlement(self):
        self.submit(x_nest_unix_user="root", x_nest_hpc_binding="forged", x_nest_entitled="*")
        [attempt] = self.attempts()
        self.assertEqual((attempt["unix_user"], attempt["hpc_binding_id"]), ("nestop", None))

    def test_snapshot_survives_later_policy_and_configuration_changes(self):
        self.submit()
        [before] = self.attempts()
        self.accounts.unlink_project(self.project, self.platform_id, "proj", **OPS)
        self.platforms.revoke_project(self.platform_id, self.project, **OPS)
        [after] = self.attempts()
        for column in ("slurm_account", "slurm_account_origin", "login_node_host", "unix_user"):
            self.assertEqual(after[column], before[column])


class ApplicationOutcomeTests(_Base):
    """
    A function that raises completes in Slurm (`COMPLETED`), but its attempt is
    `failed` with `remote_application_error`, decided from the worker's plain-JSON
    outcome record in the attempt's own artifact directory, never by unpickling the
    result (issue #73).
    """

    @property
    def train(self):
        """A function cloudpickle sends by value, since it is defined here."""

        def train():
            raise RuntimeError("CUDA error: device-side assert triggered")

        return train

    def flaky(self):
        """Raises until `self.flag` exists, then returns "ok"; the same payload each time."""
        flag = str(self.hpc.base.parent / "second-try")

        def flaky():
            if not os.path.exists(flag):
                raise RuntimeError("first try fails")
            return "ok"

        return flaky

    @property
    def flag(self) -> Path:
        return self.hpc.base.parent / "second-try"

    def attempt_dir(self, execution_id: str, attempt: int = 1) -> Path:
        return self.hpc.base / "jobs" / execution_id / "attempts" / str(attempt)

    def outcome_file(self, execution_id: str, attempt: int = 1) -> Path:
        return self.attempt_dir(execution_id, attempt) / "outcome.json"

    def assert_application_failure(self, attempt, error_type="builtins.RuntimeError"):
        self.assertEqual(
            (attempt["status"], attempt["slurm_state"], attempt["error_kind"]),
            ("failed", "COMPLETED", "remote_application_error"),
        )
        self.assertIn(error_type, attempt["error_message"])
        # The exception message and traceback are the caller's, not the registry's.
        self.assertNotIn("device-side", attempt["error_message"])
        self.assertIsNotNone(attempt["finished_at"])

    def record_nothing_on_completion(self):
        """The Execute RPC records nothing itself, as when a status query wins the race."""
        patcher = mock.patch.object(RegistryAdmission, "completed", autospec=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def status(self, job_id):
        return self.stub.GetJobStatus(
            execution_pb2.JobStatusRequest(job_id=job_id), timeout=30, metadata=self.metadata()
        ).state

    def deploy_legacy_worker(self):
        """A worker script from before the outcome record: it declares no protocol."""
        script = self.hpc.base / "project" / "scripts" / "worker.sbatch"
        lines = script.read_text().splitlines(keepends=True)
        script.write_text("".join(line for line in lines if "nest-worker-protocol" not in line))

    # The contract --------------------------------------------------------------

    def test_application_error_fails_the_attempt_and_delivers_the_traceback(self):
        record = self.execute(self.train)
        # Delivered to the client unchanged.
        self.assertIs(record["__nest_remote_error_v1__"], True)
        self.assertEqual(record["type"], "builtins.RuntimeError")
        self.assertEqual(record["message"], "CUDA error: device-side assert triggered")
        self.assertIn("in train", record["traceback"])
        [worker] = self.sbatch_calls()
        self.assertEqual(self.hpc.job_state(worker["job_id"]), "COMPLETED")
        [attempt] = self.attempts()
        self.assert_application_failure(attempt)
        self.assertEqual(
            (attempt["slurm_job_id"], attempt["worker_protocol"]), (worker["job_id"], 2)
        )
        # Its own artifact directory, passed to the worker as the attempt number.
        self.assertEqual(worker["args"][6], "1")
        directory = self.attempt_dir(attempt["execution_id"])
        self.assertEqual(attempt["output_ref"], f"{directory}/output.pkl")
        # The record the Controller read is plain JSON with only the type.
        self.assertEqual(
            json.loads(self.outcome_file(attempt["execution_id"]).read_text()),
            {"nest_outcome": 1, "status": "raised", "error_type": "builtins.RuntimeError"},
        )
        [execution] = self.sql("SELECT status, finished_at FROM executions")
        self.assertEqual((execution["status"], execution["finished_at"]), ("failed", None))
        # A later status query or sweep does not overwrite the application outcome.
        self.assertEqual(self.status(worker["job_id"]), "COMPLETED")
        self.admission.settle(timedelta(0))
        self.assertEqual(self.attempts(), [attempt])

    def test_returning_function_succeeds(self):
        self.assertEqual(self.execute(), 42)
        [attempt] = self.attempts()
        self.assertEqual(
            (attempt["status"], attempt["slurm_state"], attempt["error_kind"]),
            ("succeeded", "COMPLETED", None),
        )
        self.assertEqual(
            json.loads(self.outcome_file(attempt["execution_id"]).read_text()),
            {"nest_outcome": 1, "status": "returned"},
        )
        # Published atomically: no partial file is left beside the result.
        names = {p.name for p in self.attempt_dir(attempt["execution_id"]).iterdir()}
        self.assertEqual(names - {".nest-bootstrap"}, {"input.pkl", "outcome.json", "output.pkl"})

    def test_application_failure_may_be_retried_under_the_same_key(self):
        first = self.execute(self.flaky(), x_nest_idempotency_key="train-1")
        self.assertEqual(first["message"], "first try fails")
        self.flag.touch()
        self.assertEqual(self.execute(self.flaky(), x_nest_idempotency_key="train-1"), "ok")
        failed, retried = self.attempts()
        self.assert_application_failure(failed)
        self.assertEqual(retried["execution_id"], failed["execution_id"])
        self.assertEqual(
            (retried["attempt_number"], retried["status"], retried["error_kind"]),
            (2, "succeeded", None),
        )
        # Each attempt keeps its own artifacts and reference.
        execution_id = failed["execution_id"]
        self.assertEqual(failed["output_ref"], f"{self.attempt_dir(execution_id, 1)}/output.pkl")
        self.assertEqual(retried["output_ref"], f"{self.attempt_dir(execution_id, 2)}/output.pkl")
        self.assertEqual(
            json.loads(self.outcome_file(execution_id, 1).read_text())["status"], "raised"
        )
        self.assertEqual(
            json.loads(self.outcome_file(execution_id, 2).read_text())["status"], "returned"
        )
        [execution] = self.sql("SELECT status FROM executions")
        self.assertEqual(execution["status"], "succeeded")
        self.assertEqual(len(self.sbatch_calls()), 2)

    # Retry isolation -----------------------------------------------------------

    def run_retry_while_first_rpc_is_paused(self, pause_in: str):
        """
        Pauses attempt A's Execute in `pause_in` (`wait_result`, before its download,
        or `read_outcome`, after it), settles A through GetJobStatus as another
        replica would, runs retry B with the same key to completion, then resumes A.
        """
        paused, resume = threading.Event(), threading.Event()
        original = getattr(SlurmBackend, pause_in)
        calls = []

        def pause(backend, *args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                paused.set()
                resume.wait(30)
            return original(backend, *args, **kwargs)

        results: dict[str, Any] = {}

        def first():
            results["a"] = self.execute(self.flaky(), x_nest_idempotency_key="race")

        with mock.patch.object(SlurmBackend, pause_in, autospec=True, side_effect=pause):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(paused.wait(30))
            [a] = self.attempts()
            self.assertEqual(self.status(a["slurm_job_id"]), "COMPLETED")
            [a] = self.attempts()
            self.assert_application_failure(a)
            self.flag.touch()
            results["b"] = self.execute(self.flaky(), x_nest_idempotency_key="race")
            resume.set()
            thread.join(60)
        return results

    def assert_isolated(self, results):
        # A's client gets A's traceback, never B's result; B gets its own.
        self.assertEqual(results["a"]["message"], "first try fails")
        self.assertEqual(results["b"], "ok")
        a, b = self.attempts()
        self.assert_application_failure(a)
        self.assertEqual(
            (b["attempt_number"], b["status"], b["error_kind"]), (2, "succeeded", None)
        )
        self.assertNotEqual(a["output_ref"], b["output_ref"])
        execution_id = a["execution_id"]
        self.assertEqual(
            json.loads(self.outcome_file(execution_id, 1).read_text())["status"], "raised"
        )
        self.assertEqual(
            cloudpickle.loads((self.attempt_dir(execution_id, 1) / "output.pkl").read_bytes())[
                "message"
            ],
            "first try fails",
        )
        self.assertEqual(
            cloudpickle.loads((self.attempt_dir(execution_id, 2) / "output.pkl").read_bytes()), "ok"
        )

    def test_retry_never_touches_an_attempt_paused_before_its_download(self):
        self.assert_isolated(self.run_retry_while_first_rpc_is_paused("wait_result"))

    def test_retry_never_touches_an_attempt_paused_before_its_outcome_read(self):
        self.assert_isolated(self.run_retry_while_first_rpc_is_paused("read_outcome"))

    # Missing, late and unreadable records ----------------------------------------

    def remove_outcome_after_the_job(self):
        """The job completes, but its outcome record is not visible to the login node."""
        original = SlurmBackend.wait_result
        hidden: list[Path] = []

        def hide(backend, job_id, job_dir, *args, **kwargs):
            result = original(backend, job_id, job_dir, *args, **kwargs)
            record = Path(job_dir) / "outcome.json"
            hidden.append(record.with_name("outcome.hidden"))
            record.rename(hidden[-1])
            return result

        patcher = mock.patch.object(SlurmBackend, "wait_result", autospec=True, side_effect=hide)
        patcher.start()
        self.addCleanup(patcher.stop)
        return hidden

    def test_missing_outcome_record_is_never_a_success(self):
        self.remove_outcome_after_the_job()
        # The caller still gets the result.
        self.assertEqual(self.execute(), 42)
        [attempt] = self.attempts()
        # Live (so it keeps any reservation), marked pending.
        self.assertEqual(
            (attempt["status"], attempt["slurm_state"], attempt["error_kind"]),
            ("submitted", "COMPLETED", "remote_outcome_pending"),
        )
        # A status query never fails or succeeds it; a sweep waits a full threshold.
        self.assertEqual(self.status(attempt["slurm_job_id"]), "COMPLETED")
        self.admission.settle(timedelta(hours=1))
        self.assertEqual(self.attempts()[0]["status"], "submitted")
        self.admission.settle(timedelta(0))
        [attempt] = self.attempts()
        self.assertEqual(
            (attempt["status"], attempt["slurm_state"], attempt["error_kind"]),
            ("failed", "COMPLETED", "remote_outcome_missing"),
        )

    def test_a_late_outcome_record_settles_the_attempt(self):
        records = self.remove_outcome_after_the_job()
        self.execute(self.train)
        [attempt] = self.attempts()
        self.assertEqual(attempt["error_kind"], "remote_outcome_pending")
        # The shared filesystem catches up before the sweep decides.
        records[-1].rename(records[-1].with_name("outcome.json"))
        self.admission.settle(timedelta(0))
        [attempt] = self.attempts()
        self.assert_application_failure(attempt)

    def test_unreadable_outcome_leaves_the_attempt_live_for_settling(self):
        self.hpc.drop_ssh("nest-outcome")
        record = self.execute(self.train)
        self.assertEqual(record["message"], "CUDA error: device-side assert triggered")
        [attempt] = self.attempts()
        # Never recorded as succeeded; the reservation is kept until it is known.
        self.assertEqual((attempt["status"], attempt["error_kind"]), ("submitted", None))
        # The sweep cannot read it either: still live.
        self.admission.settle(timedelta(0))
        self.assertEqual(self.attempts()[0]["status"], "submitted")
        (self.hpc.state / "ssh-fault.json").unlink()
        self.admission.settle(timedelta(0))
        [attempt] = self.attempts()
        self.assert_application_failure(attempt)

    def test_an_outcome_path_that_is_not_a_regular_file_fails_the_attempt(self):
        self.record_nothing_on_completion()
        self.execute()
        [attempt] = self.attempts()
        record = self.outcome_file(attempt["execution_id"])
        record.unlink()
        record.mkdir()
        self.admission.settle(timedelta(0))
        [attempt] = self.attempts()
        self.assertEqual(
            (attempt["status"], attempt["slurm_state"], attempt["error_kind"]),
            ("failed", "COMPLETED", "remote_outcome_unreadable"),
        )

    def test_status_query_settles_a_completed_application_failure_as_failed(self):
        self.record_nothing_on_completion()
        self.execute(self.train)
        [attempt] = self.attempts()
        self.assertEqual(attempt["status"], "submitted")
        self.assertEqual(self.status(attempt["slurm_job_id"]), "COMPLETED")
        [attempt] = self.attempts()
        self.assert_application_failure(attempt)

    def test_invalid_outcome_record_fails_the_attempt(self):
        self.record_nothing_on_completion()
        self.execute()
        [attempt] = self.attempts()
        self.outcome_file(attempt["execution_id"]).write_bytes(
            b'{"nest_outcome": "sk-live-SECRET-SENTINEL", "status": "returned"}'
        )
        self.admission.settle(timedelta(0))
        [attempt] = self.attempts()
        self.assertEqual(
            (attempt["status"], attempt["slurm_state"], attempt["error_kind"]),
            ("failed", "COMPLETED", "remote_outcome_invalid"),
        )
        self.assertNotIn("SECRET-SENTINEL", attempt["error_message"])

    # Legacy workers ------------------------------------------------------------

    def test_legacy_worker_is_refused_before_anything_is_staged(self):
        self.deploy_legacy_worker()
        error = self.refused(self.execute)
        self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
        self.assertEqual(
            dict(error.trailing_metadata())["nest-error-kind"], "worker_protocol_unsupported"
        )
        self.assertIn("NEST_LEGACY_WORKERS=allow", error.details())
        self.assertEqual(self.sbatch_calls(), [])
        self.assertFalse(any("input.pkl" in " ".join(c["argv"]) for c in self.hpc.calls("ssh")))
        [attempt] = self.attempts()
        self.assertEqual(
            (attempt["status"], attempt["error_kind"], attempt["worker_protocol"]),
            ("failed", "worker_protocol_unsupported", None),
        )

    def test_allowed_legacy_worker_is_an_unverified_success_in_the_shared_directory(self):
        admission = RegistryAdmission(
            self.db,
            self.backend,
            self.profile,
            self.platform_id,
            audit=self.audit,
            legacy_workers="allow",
        )
        self.admission = admission
        self.stub = self.serve(admission)
        self.deploy_legacy_worker()
        self.record_nothing_on_completion()
        self.execute()
        [worker] = self.sbatch_calls()
        self.assertEqual(worker["args"][6], "")  # no attempt directory
        [attempt] = self.attempts()
        self.assertEqual(attempt["worker_protocol"], 1)
        shared = self.hpc.base / "jobs" / attempt["execution_id"]
        # A worker that predates the record writes none.
        (shared / "outcome.json").unlink()
        admission.settle(timedelta(0))
        [attempt] = self.attempts()
        self.assertEqual(
            (attempt["status"], attempt["error_kind"], attempt["worker_protocol"]),
            ("succeeded", None, 1),
        )
        self.assertEqual(attempt["output_ref"], f"{shared}/output.pkl")

    def test_worker_protocol_is_pinned_once_before_submission(self):
        self.record_nothing_on_completion()
        self.execute()
        [attempt] = self.attempts()
        with self.assertRaises(ExecutionRegistryError):
            self.registry.record_worker_protocol(attempt["id"], 1)
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.sql(
                "UPDATE execution_attempts SET worker_protocol = 1 WHERE id = %s", attempt["id"]
            )
        self.assertEqual(self.attempts()[0]["worker_protocol"], 2)

    # Other kinds and paths -------------------------------------------------------

    def test_batch_jobs_complete_without_reading_an_outcome(self):
        response = self.submit()
        self.assertEqual(self.status(response.job_id), "COMPLETED")
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["error_kind"]), ("succeeded", None))
        self.assertFalse(
            any("nest-outcome" in " ".join(call["argv"]) for call in self.hpc.calls("ssh"))
        )

    def test_legacy_jobs_record_an_application_failure(self):
        stub = self.serve(None)
        response = stub.Execute(
            execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((self.train, (), {}))),
            timeout=60,
            metadata=self.metadata(),
        )
        record = cloudpickle.loads(response.result)
        self.assertEqual(record["type"], "builtins.RuntimeError")
        [job] = self.sql("SELECT status, error_kind, error_message FROM jobs")
        self.assertEqual((job["status"], job["error_kind"]), ("failed", "remote_application_error"))
        self.assertNotIn("device-side", job["error_message"])

    def test_legacy_jobs_mark_an_unreadable_outcome_unknown(self):
        self.hpc.drop_ssh("nest-outcome")
        stub = self.serve(None)
        response = stub.Execute(
            execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((self.train, (), {}))),
            timeout=60,
            metadata=self.metadata(),
        )
        self.assertEqual(cloudpickle.loads(response.result)["type"], "builtins.RuntimeError")
        [job] = self.sql("SELECT status, error_kind FROM jobs")
        self.assertEqual(
            (job["status"], job["error_kind"]), ("unknown", "remote_outcome_unreadable")
        )


class DatabaseFailureTests(_Base):
    def test_failed_registration_or_attempt_commit_submits_nothing(self):
        failure = DatabaseError("Cannot register an execution: connection lost")
        for method in ("register", "begin_attempt"):
            for call in (self.submit, self.execute):
                with (
                    self.subTest(method=method, rpc=call.__name__),
                    mock.patch.object(ExecutionRegistry, method, side_effect=failure),
                ):
                    error = self.refused(call)
                    self.assertEqual(error.code(), grpc.StatusCode.UNAVAILABLE)
                    metadata = dict(error.trailing_metadata())
                    self.assertEqual(metadata["nest-error-kind"], "database_unavailable")
        self.assert_nothing_sent()
        self.assertEqual(self.attempts(), [])

    def test_unavailable_database_submits_nothing(self):
        self.db.close()
        for call in (self.submit, self.execute):
            with self.subTest(rpc=call.__name__):
                self.assertEqual(self.refused(call).code(), grpc.StatusCode.UNAVAILABLE)
        self.assert_nothing_sent()

    def lose_job_id_records(self):
        """Makes every attempt to record a Slurm job ID fail, without retry delays."""
        failure = DatabaseError("Cannot mark an execution attempt submitted")
        for patcher in (
            mock.patch.object(ExecutionRegistry, "record_submission", side_effect=failure),
            mock.patch.object(RegistryAdmission, "RECORD_RETRY_DELAYS", (0.0, 0.0)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return failure

    def test_unrecorded_job_id_is_never_acknowledged_as_success(self):
        self.lose_job_id_records()
        for call in (self.submit, self.execute):
            with self.subTest(rpc=call.__name__):
                before = len(self.sbatch_calls())
                error = self.refused(call)
                self.assertEqual(error.code(), grpc.StatusCode.UNKNOWN)
                metadata = dict(error.trailing_metadata())
                self.assertEqual(metadata["nest-error-kind"], "submission_unknown")
                [job] = self.sbatch_calls()[before:]  # submitted once, never resubmitted
                # The job is recoverable from the answer itself.
                self.assertEqual(metadata["nest-job-id"], str(job["job_id"]))
                self.assertIn("nest-execution-id", metadata)
                self.assertIn(f"--slurm-job-id {job['job_id']}", error.details())
        # Execute did not wait for, or return, a result it could not record.
        self.assertEqual([c for c in self.hpc.calls("ssh") if "output.pkl" in str(c)], [])
        self.assertEqual({a["status"] for a in self.attempts()}, {"submitting"})

    def test_recovery_records_a_job_id_the_controller_received(self):
        self.lose_job_id_records()
        error = self.refused(self.submit)
        job_id = int(dict(error.trailing_metadata())["nest-job-id"])
        # While the database stays down, recovery keeps the job ID for later.
        self.admission.recover(timedelta(hours=1))
        self.assertEqual(self.attempts()[0]["status"], "submitting")
        mock.patch.stopall()
        # Even after the attempt was marked unknown, the kept job ID is recorded.
        self.sql("UPDATE execution_attempts SET updated_at = now() - interval '1 day'")
        self.registry.mark_interrupted(timedelta(hours=1))
        self.admission.recover(timedelta(hours=1))
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["slurm_job_id"]), ("submitted", job_id))
        self.assertEqual(len(self.sbatch_calls()), 1)

    def test_restart_before_recording_leaves_an_unknown_attempt_to_reconcile(self):
        self.lose_job_id_records()
        error = self.refused(lambda: self.submit(x_nest_idempotency_key="k"))
        mock.patch.stopall()
        self.sql("UPDATE execution_attempts SET updated_at = now() - interval '1 day'")
        # A new Controller has no memory of the job ID: the attempt becomes unknown.
        restarted = RegistryAdmission(self.db, self.backend, self.profile, self.platform_id)
        [interrupted] = restarted.recover(timedelta(hours=1))
        self.assertEqual(
            (interrupted.status, interrupted.error_kind), ("unknown", "controller_interrupted")
        )
        retry = self.refused(lambda: self.submit(x_nest_idempotency_key="k"))
        self.assertEqual(retry.code(), grpc.StatusCode.ABORTED)
        # The job ID the client was given is the operator's evidence.
        job_id = dict(error.trailing_metadata())["nest-job-id"]
        self.registry.reconcile(
            interrupted.id, evidence="job ID in the UNKNOWN answer", slurm_job_id=int(job_id)
        )
        self.assertEqual(self.submit(x_nest_idempotency_key="k").job_id, int(job_id))
        self.assertEqual(len(self.sbatch_calls()), 1)


class UnknownOutcomeTests(_Base):
    ssh_timeout = 1.0

    def test_lost_sbatch_acknowledgement_is_unknown_and_never_resubmitted(self):
        self.hpc.hang_ssh("sbatch --parsable", after=True)
        error = self.refused(lambda: self.submit(x_nest_idempotency_key="run-1"))
        self.assertEqual(error.code(), grpc.StatusCode.UNKNOWN)
        metadata = dict(error.trailing_metadata())
        self.assertEqual(metadata["nest-error-kind"], "submission_unknown")
        execution_id = metadata["nest-execution-id"]
        [attempt] = self.attempts()
        self.assertEqual(
            (attempt["status"], attempt["slurm_job_id"], attempt["error_kind"]),
            ("unknown", None, "submission_unknown"),
        )
        [job] = self.sbatch_calls()  # sbatch ran remotely; its job ID never arrived
        (self.hpc.state / "ssh-fault.json").unlink()
        # A retry with the same key is refused until the outcome is reconciled.
        retry = self.refused(lambda: self.submit(x_nest_idempotency_key="run-1"))
        self.assertEqual(retry.code(), grpc.StatusCode.ABORTED)
        self.assertIn("unknown", retry.details())
        self.assertEqual(len(self.sbatch_calls()), 1)

        # An operator finds the job and reconciles with that evidence.
        code, _, err = self.nest_db(
            "execution", "reconcile", str(attempt["id"]), "--slurm-job-id", str(job["job_id"])
        )
        self.assertEqual(code, 0, err)
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["slurm_job_id"]), ("submitted", job["job_id"]))
        [event] = self.audit.events(event="execution.reconciled")
        self.assertEqual(event.details["evidence"], "sacct shows the job")
        self.assertEqual(event.project_id, self.project)
        # Now the same key returns the reconciled job, still without a new sbatch.
        replay = self.submit(x_nest_idempotency_key="run-1")
        self.assertEqual(replay.job_id, job["job_id"])
        self.assertEqual(len(self.sbatch_calls()), 1)
        self.assertEqual(
            self.sql("SELECT count(*) AS n FROM executions WHERE execution_id = %s", execution_id),
            [{"n": 1}],
        )

    def test_reconciled_not_submitted_allows_a_retry(self):
        self.hpc.hang_ssh("sbatch --parsable", after=False)
        self.refused(lambda: self.submit(x_nest_idempotency_key="run-2"))
        self.assertEqual(self.sbatch_calls(), [])
        [attempt] = self.attempts()
        self.assertEqual(attempt["status"], "unknown")
        (self.hpc.state / "ssh-fault.json").unlink()
        # A state Nest still knows is never overwritten by reconciliation.
        code, _, _ = self.nest_db(
            "execution", "reconcile", str(attempt["id"] + 1000), "--not-submitted"
        )
        self.assertEqual(code, 1)
        code, _, err = self.nest_db("execution", "reconcile", str(attempt["id"]), "--not-submitted")
        self.assertEqual(code, 0, err)
        code, out, _ = self.nest_db("execution", "list")
        self.assertEqual((code, out), (0, ""))
        response = self.submit(x_nest_idempotency_key="run-2")
        first, second = self.attempts()
        self.assertEqual(
            (first["status"], first["error_kind"]), ("failed", "reconciled_not_submitted")
        )
        self.assertEqual(
            (second["attempt_number"], second["status"], second["slurm_job_id"]),
            (2, "submitted", response.job_id),
        )

    def test_lost_worker_acknowledgement_in_execute_is_unknown(self):
        self.hpc.hang_ssh("sbatch --parsable", after=True)
        error = self.refused(self.execute)
        self.assertEqual(error.code(), grpc.StatusCode.UNKNOWN)
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["slurm_job_id"]), ("unknown", None))
        self.assertEqual(len(self.sbatch_calls()), 1)

    def nest_db(self, *args):
        argv = list(args)
        if argv[:2] == ["execution", "reconcile"]:
            argv += ["--operator", "ops", "--reason", "sacct shows the job"]
        out, err = StringIO(), StringIO()
        with (
            mock.patch.dict(os.environ, {"NEST_DATABASE_URL": self.url}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = nest_db(argv)
        return code, out.getvalue(), err.getvalue()


class ConcurrencyAndRecoveryTests(_Base):
    def test_duplicate_requests_submit_once(self):
        first = self.submit(x_nest_idempotency_key="dup")
        again = self.submit(x_nest_idempotency_key="dup")
        self.assertEqual(again.job_id, first.job_id)
        self.assertEqual(len(self.sbatch_calls()), 1)
        # The same key for a different request is refused, not merged.
        conflict = self.refused(lambda: self.submit(x_nest_idempotency_key="dup", account="spare"))
        self.assertEqual(conflict.code(), grpc.StatusCode.FAILED_PRECONDITION)
        self.assertIn("idempotency_conflict", conflict.details())
        # Another User's identical key is another execution.
        self.submit(token=self.token_outsider, project=self.other, x_nest_idempotency_key="dup")
        self.assertEqual(len(self.sbatch_calls()), 2)

    def test_concurrent_attempts_submit_once(self):
        release = threading.Event()
        original = SlurmBackend.submit

        def slow(backend, *args, **kwargs):
            release.wait(10)
            return original(backend, *args, **kwargs)

        outcomes = []

        def call():
            try:
                outcomes.append(self.submit(x_nest_idempotency_key="race").job_id)
            except grpc.RpcError as exc:
                outcomes.append(exc.code())  # ty: ignore[missing-attribute]

        with mock.patch.object(SlurmBackend, "submit", autospec=True, side_effect=slow):
            threads = [threading.Thread(target=call) for _ in range(4)]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 10
            while len(outcomes) < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            release.set()
            for thread in threads:
                thread.join(20)
        self.assertEqual(len(self.sbatch_calls()), 1)
        job_ids = [o for o in outcomes if isinstance(o, int)]
        self.assertEqual(set(job_ids), {self.sbatch_calls()[0]["job_id"]})
        self.assertTrue(all(o == grpc.StatusCode.ABORTED for o in outcomes if o not in job_ids))
        self.assertEqual(len(self.attempts()), 1)

    def test_restart_marks_interrupted_attempts_unknown(self):
        # A Controller that stops after committing the attempt, before sbatch.
        principal = Principal(self.member, "cli", frozenset({"jobs:run"}))
        request = SubmissionRequest("batch", workdir="/w", script="s", idempotency_key="crash")
        admitted = self.admission.admit(
            principal, self.project, request, execution_id=uuid.uuid4().hex
        )
        self.assertEqual(admitted.attempt.status, "submitting")
        # "Restart": a new pool and admission see only what was committed.
        restarted = self.open_database()
        admission = RegistryAdmission(restarted, self.backend, self.profile, self.platform_id)
        [live] = ExecutionRegistry(restarted).live_attempts(self.platform_id)
        self.assertEqual(live.status, "submitting")
        # Too recent to rule out another Controller's submission in progress: untouched.
        RecoverySweeper(admission, timedelta(hours=1), interval=3600).start().stop()
        self.assertEqual(self.attempts()[0]["status"], "submitting")
        self.sql("UPDATE execution_attempts SET updated_at = now() - interval '2 hours'")
        RecoverySweeper(admission, timedelta(hours=1), interval=3600).start().stop()
        [attempt] = self.attempts()
        self.assertEqual(
            (attempt["status"], attempt["error_kind"]), ("unknown", "controller_interrupted")
        )
        # The same request is never resubmitted blindly.
        retry = self.refused(
            lambda: self.stub.SubmitJob(
                execution_pb2.SubmitJobRequest(workdir="/w", script="s"),
                timeout=30,
                metadata=self.metadata(x_nest_idempotency_key="crash"),
            )
        )
        self.assertEqual(retry.code(), grpc.StatusCode.ABORTED)
        self.assertEqual(self.sbatch_calls(), [])


class AuthorizationTests(_Base):
    def test_missing_or_revoked_grants_and_accounts_submit_nothing(self):
        beta = self.platforms.create_platform("beta", "Beta", **OPS)
        self.platforms.add_login_node(beta.id, "login.beta", transport_modes=["A"], **OPS)
        self.platforms.grant_project(beta.id, self.project, **OPS)
        cases = [
            ("unknown platform", {"x_nest_platform": "nope"}, grpc.StatusCode.NOT_FOUND, None),
            (
                "other platform",
                {"x_nest_platform": "beta"},
                grpc.StatusCode.FAILED_PRECONDITION,
                "platform_not_served",
            ),
            ("ambiguous", {}, grpc.StatusCode.FAILED_PRECONDITION, "ambiguous"),
            (
                "unlinked account",
                {"x_nest_platform": "nano", "account": "nobody"},
                grpc.StatusCode.PERMISSION_DENIED,
                "not_permitted",
            ),
        ]
        for name, extra, code, reason in cases:
            with self.subTest(name):
                error = self.refused(lambda e=extra: self.submit(**e))
                self.assertEqual(error.code(), code)
                if reason:
                    self.assertIn(reason, error.details())
        # A disabled Project default Platform is refused, never replaced.
        self.projects_default(beta.id)
        self.platforms.set_status(beta.id, "disabled", **OPS)
        error = self.refused(self.submit)
        self.assertIn("default_unavailable", error.details())
        self.projects_default(None)
        self.accounts.update_account(self.platform_id, "spare", status="disabled", **OPS)
        error = self.refused(lambda: self.submit(account="spare"))
        self.assertIn("account_disabled", error.details())
        self.accounts.unlink_project(self.project, self.platform_id, "proj", **OPS)
        error = self.refused(self.submit)
        self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
        self.assertIn("no_account", error.details())
        self.platforms.revoke_project(self.platform_id, self.project, **OPS)
        error = self.refused(self.execute)
        self.assertIn("none_authorized", error.details())
        self.assert_nothing_sent()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.sql("SELECT * FROM executions"), [])
        reasons = {e.reason for e in self.audit.events(event="execution.admit", limit=100)}
        self.assertLessEqual({"platform_not_served", "ambiguous", "none_authorized"}, reasons)

    def projects_default(self, platform_id):
        from nest.authz import PlatformCatalog

        PlatformCatalog(self.db).set_project_default(browser(self.owner), self.project, platform_id)

    def test_job_operations_are_scoped_to_project_and_platform(self):
        mine = self.submit().job_id
        theirs = self.submit(token=self.token_outsider, project=self.other).job_id
        for job in (theirs, 99999):
            for rpc in ("GetJobStatus", "CancelJob"):
                with self.subTest(job=job, rpc=rpc):
                    request = (
                        execution_pb2.JobStatusRequest(job_id=job)
                        if rpc == "GetJobStatus"
                        else execution_pb2.CancelJobRequest(job_id=job)
                    )
                    error = self.refused(
                        lambda r=rpc, q=request: getattr(self.stub, r)(
                            q, timeout=10, metadata=self.metadata()
                        )
                    )
                    self.assertEqual(error.code(), grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.hpc.calls("scancel"), [])
        # An attempt on another Platform with the same Slurm job ID is another job.
        self.assertIsNone(ExecutionRegistry(self.db).job_owner(uuid.uuid4(), mine))
        # A legacy jobs row reusing my job ID for another Project is older: the
        # registry attempt, the newer record, decides.
        with connect(self.url) as conn:
            conn.execute(
                "INSERT INTO jobs (execution_id, provider_id, kind, status, slurm_job_id, "
                "project_id, submitted_by_user_id, created_at) VALUES "
                "(%s, 'nano', 'batch', 'submitted', %s, %s, %s, now() - interval '1 day')",
                (uuid.uuid4().hex, mine, self.other, self.outsider),
            )
        state = self.stub.GetJobStatus(
            execution_pb2.JobStatusRequest(job_id=mine), timeout=10, metadata=self.metadata()
        ).state
        self.assertEqual(state, "COMPLETED")
        self.stub.CancelJob(
            execution_pb2.CancelJobRequest(job_id=mine), timeout=10, metadata=self.metadata()
        )
        self.assertEqual([c["job_id"] for c in self.hpc.calls("scancel")], [str(mine)])
        [cancel] = [
            e for e in self.audit.events(event="run.cancel", limit=100) if e.outcome == "succeeded"
        ]
        self.assertEqual(cancel.project_id, self.project)


class ModeBBoundaryTests(_Base):
    """Mode B has no verified per-User binding or transport yet: it fails closed."""

    def setUp(self):
        super().setUp()
        self.profile_b = PlatformProfile(
            id="nanob",
            ssh_mode="B",
            login_nodes=(LoginNode(FakeHpc.host, 22),),
            account="legacy-acct",
            workdir=str(self.hpc.base),
            ssh_user="nestop",
        )
        mirrored = self.platforms.sync_environment({"nano": self.profile, "nanob": self.profile_b})
        self.platform_b = mirrored["nanob"]
        self.platforms.grant_project(self.platform_b, self.project, **OPS)
        self.accounts.register_account(self.platform_b, "bacct", **OPS)
        self.accounts.link_project(self.project, self.platform_b, "bacct", modes=["B"], **OPS)
        self.backend_b = SlurmBackend(
            SlurmConfig(
                ssh_host=FakeHpc.host,
                ssh_port=22,
                account="legacy-acct",
                work_root=f"{self.hpc.base}/jobs",
                worker_workdir=str(self.hpc.base),
                ssh_mode="B",
                ssh_user="nestop",
                identity_file="/nonexistent/controller-key",
                known_hosts_file="/nonexistent/known_hosts",
                allowed_hosts=(FakeHpc.host,),
            )
        )

    def stub_b(self, identities=None):
        admission = RegistryAdmission(
            self.db,
            self.backend_b,
            self.profile_b,
            self.platform_b,
            identities=identities,
            audit=self.audit,
        )
        return self.serve(admission, self.backend_b)

    def mode_b_submit(self, stub, **extra):
        return self.submit(stub=stub, x_nest_platform="nanob", **extra)

    def test_without_a_verified_binding_mode_b_is_refused(self):
        error = self.refused(lambda: self.mode_b_submit(self.stub_b()))
        self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
        self.assertIn("hpc_binding_unavailable", error.details())
        self.assert_nothing_sent()

    def test_the_controller_identity_is_never_a_mode_b_fallback(self):
        class ControllerAsModeB:
            def resolve(self, principal, platform, mode):
                return TrustedHPCIdentity(
                    HPCIdentity.controller(platform.id), HPCPrincipal("nestop")
                )

        error = self.refused(lambda: self.mode_b_submit(self.stub_b(ControllerAsModeB())))
        self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
        self.assertIn("identity_not_allowed", error.details())
        self.assert_nothing_sent()

    def test_a_verified_binding_still_has_no_controller_credential_transport(self):
        member = self.member

        class VerifiedBinding:
            def resolve(self, principal, platform, mode):
                return TrustedHPCIdentity(
                    HPCIdentity.user(platform.id, member, verified_default_account="bacct"),
                    HPCPrincipal("alice", 1001, binding_id="b-1", binding_version=1),
                )

        self.accounts.unlink_project(self.project, self.platform_b, "bacct", **OPS)
        self.accounts.set_native_default(self.project, self.platform_b, True, **OPS)
        stub = self.stub_b(VerifiedBinding())
        error = self.refused(lambda: self.mode_b_submit(stub))
        self.assertIn("mode_b_transport_unavailable", error.details())
        # Resolved to the User's native default, never to a Controller account.
        resolved = [
            e
            for e in self.audit.events(event="slurm_account.resolve", limit=10)
            if e.outcome == "denied"
        ]
        self.assertEqual(resolved, [])
        # A binding bound to another User is refused before any account is chosen.
        outsider = self.outsider

        class StolenBinding(VerifiedBinding):
            def resolve(self, principal, platform, mode):
                return TrustedHPCIdentity(
                    HPCIdentity.user(platform.id, outsider), HPCPrincipal("mallory")
                )

        error = self.refused(lambda: self.mode_b_submit(self.stub_b(StolenBinding())))
        self.assertIn("identity_not_allowed", error.details())
        self.assert_nothing_sent()
        self.assertEqual(self.attempts(), [])


class ControllerProcessTests(PostgresTestCase):
    """The real Controller process with NEST_EXECUTION_REGISTRY=required."""

    def test_controller_admits_both_rpcs_through_the_registry_and_recovers(self):
        from test_controller import controller_at_free_port

        url = self.database()
        with connect(url) as conn:
            upgrade(conn)
        db = Database(load_database_config({"NEST_DATABASE_URL": url}))
        db.open()
        self.addCleanup(db.close)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        hpc = FakeHpc(root)
        identities = IdentityStore(db)
        user = identities.create_user(display_name="member").id
        project = owned_project(ProjectService(db), user, "alpha").id
        session = identities.create_session(
            user,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        token = secrets.token_urlsafe(32)
        SessionStore(db).issue_access_token(
            session.id, token_hash=hash_token(token), ttl_seconds=600
        )
        # The operator provisions the environment Platform's grant and account
        # explicitly; nothing is inferred from the environment.
        profile = PlatformProfile(
            "default",
            "A",
            (LoginNode(FakeHpc.host),),
            "test-account",
            workdir=str(hpc.base),
            ssh_user="nestop",
        )
        platform_id = PlatformStore(db).sync_environment({"default": profile})["default"]
        PlatformStore(db).grant_project(platform_id, project, **OPS)
        accounts = SlurmAccountStore(db)
        accounts.register_account(platform_id, "proj", **OPS)
        accounts.link_project(project, platform_id, "proj", modes=["A"], default=True, **OPS)
        # An attempt a stopped Controller left without a Slurm job ID.
        sync_platforms(db, {"default": profile}, "default")
        backend = SlurmBackend(SlurmConfig(ssh_host=FakeHpc.host, account="test-account"))
        stale = RegistryAdmission(db, backend, profile, platform_id).admit(
            Principal(user, "cli", frozenset({"jobs:run"})),
            project,
            SubmissionRequest("batch", workdir="/w", script="s"),
            execution_id=uuid.uuid4().hex,
        )
        with connect(url) as conn:
            conn.execute("UPDATE execution_attempts SET updated_at = now() - interval '1 day'")

        workdir = root / "batch"
        workdir.mkdir()
        (workdir / "hello.sbatch").write_text("#!/bin/bash\necho hello\n")
        metadata = [("authorization", f"Bearer {token}"), ("x-nest-project-id", str(project))]
        settings = {
            **hpc.controller_settings(),
            "NEST_DATABASE_URL": url,
            "NEST_SLURM_SSH_USER": "nestop",
            "NEST_PROVIDER_ALLOWED_PROJECTS": "*",
            "NEST_POLL_INTERVAL": "0.2",
        }
        with controller_at_free_port(**settings) as (_, stub, _address):
            payload = cloudpickle.dumps((lambda value: value * 2, (21,), {}))
            response = stub.Execute(
                execution_pb2.ExecuteRequest(payload=payload), timeout=60, metadata=metadata
            )
            self.assertEqual(cloudpickle.loads(response.result), 42)
            batch = stub.SubmitJob(
                execution_pb2.SubmitJobRequest(workdir=str(workdir), script="hello.sbatch"),
                timeout=60,
                metadata=metadata,
            )
        registry = ExecutionRegistry(db)
        recovered = registry.get_attempt(stale.attempt.id)
        assert recovered is not None
        self.assertEqual(
            (recovered.status, recovered.error_kind), ("unknown", "controller_interrupted")
        )
        executions = registry.list_for_project(project)
        self.assertEqual(len(executions), 3)
        submitted = [
            registry.attempts(e.execution_id, project_id=project)[0]
            for e in executions
            if e.execution_id != stale.execution_id
        ]
        self.assertEqual(
            sorted((a.status, a.slurm_account, a.principal.unix_user) for a in submitted),
            [("submitted", "proj", "nestop"), ("succeeded", "proj", "nestop")],
        )
        self.assertIn(batch.job_id, {a.slurm_job_id for a in submitted})
        self.assertTrue(all("--account=proj" in c["flags"] for c in hpc.calls("sbatch")))
        with connect(url) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone(), (0,))
            [started] = conn.execute(
                "SELECT details->>'execution_registry' FROM security_audit_events "
                "WHERE event = 'controller.started'"
            ).fetchall()
        self.assertEqual(started, ("required",))


class ControllerIdentityTests(unittest.TestCase):
    def test_mode_a_identity_comes_from_the_profile_only(self):
        profile = PlatformProfile("p", "A", (LoginNode("h"),), "acct", ssh_user="op")
        platform = mock.Mock(id=uuid.uuid4(), slug="p")
        trusted = ControllerIdentitySource(profile).resolve(
            Principal(uuid.uuid4(), "cli"), platform, "A"
        )
        self.assertEqual(trusted.identity.kind, "controller")
        self.assertEqual(trusted.principal, HPCPrincipal("op"))


class ConfigurationTests(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {
        "NEST_EXECUTOR": "slurm",
        "NEST_SLURM_HOST": "login",
        "NEST_SLURM_ACCOUNT": "acct",
        "NEST_SLURM_WORKDIR": "/w",
        "NEST_DATABASE_URL": "postgresql://nest@db.example.internal:5432/nest",
        "NEST_CONTROLLER_BIND": "127.0.0.1:50051",
    }

    def load(self, **settings):
        return load_controller_config({**self.BASE, **settings})

    def problems(self, **settings) -> str:
        with self.assertRaises(ConfigError) as caught:
            self.load(**settings)
        return str(caught.exception)

    def test_session_authentication_requires_the_registry_by_default(self):
        config = self.load(NEST_SLURM_SSH_USER="op")
        self.assertEqual((config.auth, config.execution_registry), ("session", "required"))
        self.assertEqual(config.summary()["execution_registry"], "required")
        self.assertIn("needs the operator's UNIX user", self.problems())

    def test_legacy_is_explicit_and_mode_a_only(self):
        config = self.load(NEST_EXECUTION_REGISTRY="legacy")
        self.assertEqual(config.execution_registry, "legacy")
        self.assertIsNone(build_admission(config, mock.Mock(), mock.Mock(), mock.Mock()))
        problems = self.problems(
            NEST_EXECUTION_REGISTRY="legacy",
            NEST_SLURM_SSH_MODE="B",
            NEST_SLURM_SSH_USER="op",
            NEST_SSH_KNOWN_HOSTS_DATA="login ssh-ed25519 AAAA",
            NEST_SSH_PRIVATE_KEY="-----BEGIN OPENSSH " + "PRIVATE KEY-----\nx\n",
        )
        self.assertIn("Mode B never falls back", problems)

    def test_without_session_authentication_only_legacy_exists(self):
        config = self.load(NEST_CONTROLLER_AUTH="none")
        self.assertEqual(config.execution_registry, "legacy")
        problems = self.problems(NEST_CONTROLLER_AUTH="none", NEST_EXECUTION_REGISTRY="required")
        self.assertIn("needs NEST_CONTROLLER_AUTH=session", problems)
        self.assertIn("must be one of", self.problems(NEST_EXECUTION_REGISTRY="sometimes"))


if __name__ == "__main__":
    unittest.main()
