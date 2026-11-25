"""Trusted-pilot admission limits and durable reservations (F02a).

Every test serves the real `ExecutionService` with session authentication and a
`RegistryAdmission` that enforces `AdmissionLimits`, against a real PostgreSQL
(`NEST_TEST_DATABASE_URL`, see `postgres_fixture`) and the `FakeHpc` login node,
reusing `test_registry_admission._Base`.

The central assertions: a refused submission leaves nothing registered and sends
nothing to the cluster; concurrent submissions on two Controller replicas never
oversubscribe a limit; and a reservation is released only by its attempt reaching
a final state, never by a timeout, a lost RPC, a restart or an unknown outcome.
"""

import os
import secrets
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import cast
from unittest import mock

import grpc
import psycopg
from fake_hpc import FakeHpc
from test_registry_admission import _Base, browser

from nest.admission import (
    AdmissionRefusedError,
    RecoverySweeper,
    RegistryAdmission,
    SubmissionRequest,
)
from nest.authz import Principal
from nest.backends.slurm import SlurmBackend, SlurmConfig
from nest.config import AdmissionLimits, ConfigError, load_controller_config, slurm_time_seconds
from nest.db import hash_token
from nest.db.cli import main as nest_db_main
from nest.db.executions import (
    AdmissionLimitError,
    ExecutionRegistry,
    Reservation,
    check_request_limits,
)
from nest.proto import execution_pb2

LIMITS = AdmissionLimits(
    active_per_user=2,
    active_per_project=3,
    active_per_platform=4,
    cpu_per_request=8,
    gpu_per_request=2,
    cpu_per_project=12,
    gpu_per_project=3,
    walltime_seconds=3600,
    submissions_per_minute=50,
    environment_builds=1,
)


class _LimitsBase(_Base):
    limits = LIMITS

    def setUp(self):
        super().setUp()
        self.admission = self.limited_admission()
        self.stub = self.serve(self.admission)
        # The owner of `alpha` also submits, to share the Project's limits.
        self.token_owner = self.token(self.owner)

    def limited_admission(self, db=None, limits=None) -> RegistryAdmission:
        return RegistryAdmission(
            db or self.db,
            self.backend,
            self.profile,
            self.platform_id,
            audit=self.audit,
            limits=limits or self.limits,
        )

    def session(self, user_id):
        """A CLI session for `user_id`: its access token and session ID."""
        session = self.identities.create_session(
            user_id,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        access = secrets.token_urlsafe(32)
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        return access, session.id

    def batch(self, stub=None, token=None, project=None, gpu=0, cpu=0, time="", **extra):
        return (stub or self.stub).SubmitJob(
            execution_pb2.SubmitJobRequest(
                workdir=self.batch_dir(),
                script="hello.sbatch",
                gpu=gpu,
                cpu=cpu,
                time=time,
            ),
            timeout=60,
            metadata=self.metadata(token, project, **extra),
        )

    def held(self):
        return self.sql(
            "SELECT r.*, a.status FROM execution_reservations r "
            "JOIN execution_attempts a ON a.id = r.attempt_id "
            "WHERE a.status IN ('submitting', 'submitted', 'running', 'unknown') ORDER BY r.attempt_id"
        )

    def finish(self, job_id, state="COMPLETED"):
        (self.hpc.state / f"job-{job_id}.state").write_text(state)

    def denials(self):
        return [e for e in self.audit.events(event="execution.admit") if e.outcome == "denied"]

    def nest_db(self, *args):
        argv = [*args, "--operator", "ops", "--reason", "sacct has no such job"]
        out, err = StringIO(), StringIO()
        with (
            mock.patch.dict(os.environ, {"NEST_DATABASE_URL": self.url}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = nest_db_main(argv)
        return code, out.getvalue(), err.getvalue()


class LimitTests(_LimitsBase):
    def test_active_per_user_refuses_with_nothing_sent_and_releases_on_completion(self):
        self.hpc.hold_jobs("RUNNING")
        first = self.batch()
        self.batch()
        sent = len(self.sbatch_calls())
        error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertIn("limit_active_per_user", error.details())
        self.assertIn("Nothing was submitted", error.details())
        self.assertEqual(len(self.sbatch_calls()), sent)
        # The refused request left no attempt and no reservation; its registration
        # stays pending, so a retry with the same idempotency key can proceed later.
        self.assertEqual(len(self.attempts()), 2)
        self.assertEqual(
            self.sql("SELECT status, count(*) AS n FROM executions GROUP BY status ORDER BY 1"),
            [{"status": "active", "n": 2}, {"status": "pending", "n": 1}],
        )
        [denied] = self.denials()
        self.assertEqual(denied.reason, "limit_active_per_user")
        self.assertEqual(denied.actor_user_id, self.member)
        self.assertEqual(denied.project_id, self.project)
        self.assertEqual(
            {k: denied.details[k] for k in ("limit", "current", "requested")},
            {"limit": 2, "current": 2, "requested": 1},
        )
        self.assertNotIn(self.token_member, str(denied.details))

        # A status query that observes the job finished releases its reservation.
        self.finish(first.job_id)
        state = self.stub.GetJobStatus(
            execution_pb2.JobStatusRequest(job_id=first.job_id),
            timeout=30,
            metadata=self.metadata(),
        ).state
        self.assertEqual(state, "COMPLETED")
        self.assertEqual(len(self.held()), 1)
        self.batch()
        self.assertEqual(len(self.held()), 2)

    def test_project_and_platform_limits_span_users(self):
        self.hpc.hold_jobs("RUNNING")
        self.batch()
        self.batch()
        self.batch(token=self.token_owner)
        error = self.refused(lambda: self.batch(token=self.token_owner))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertIn("limit_active_per_project", error.details())
        # Another Project still has room until the Platform is full.
        token_outsider = self.token_outsider
        self.batch(token=token_outsider, project=self.other)
        error = self.refused(lambda: self.batch(token=token_outsider, project=self.other))
        self.assertIn("limit_active_per_platform", error.details())
        self.assertEqual(len(self.held()), 4)

    def test_resource_sums_per_project(self):
        self.hpc.hold_jobs("RUNNING")
        self.batch(gpu=2, cpu=8)
        error = self.refused(lambda: self.batch(token=self.token_owner, gpu=2))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertIn("limit_gpu_per_project", error.details())
        error = self.refused(lambda: self.batch(token=self.token_owner, cpu=5))
        self.assertIn("limit_cpu_per_project", error.details())
        self.batch(token=self.token_owner, gpu=1, cpu=4)
        [first, second] = self.held()
        self.assertEqual((first["gpu"], first["cpu"]), (2, 8))
        self.assertEqual((second["gpu"], second["cpu"], second["walltime_seconds"]), (1, 4, 300))
        self.assertEqual(second["limits"]["gpu_per_project"], 3)

    def test_per_request_bounds_are_refused_before_registration(self):
        for label, kwargs, reason in (
            ("cpu", {"cpu": 9}, "limit_cpu_per_request"),
            ("gpu", {"gpu": 3}, "limit_gpu_per_request"),
            ("walltime", {"time": "02:00:00"}, "limit_walltime"),
            ("unlimited", {"time": "UNLIMITED"}, "limit_walltime_unbounded"),
        ):
            with self.subTest(label):
                error = self.refused(lambda kwargs=kwargs: self.batch(**kwargs))
                self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
                self.assertIn(reason, error.details())
        self.assert_nothing_sent()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM executions"), [{"n": 0}])
        self.assertEqual(len(self.denials()), 4)

    def test_submissions_per_minute_counts_finished_attempts_too(self):
        self.admission = self.limited_admission(
            limits=AdmissionLimits(submissions_per_minute=2, active_per_user=10)
        )
        self.stub = self.serve(self.admission)
        self.batch()
        self.batch()
        error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertIn("limit_submissions_per_minute", error.details())
        self.assertIn("in the last minute", error.details())

    def test_duplicate_retry_is_answered_without_a_new_reservation(self):
        self.hpc.hold_jobs("RUNNING")
        first = self.batch(x_nest_idempotency_key="key-1")
        self.batch()
        # At the per-User limit, the same request again returns the same job.
        again = self.batch(x_nest_idempotency_key="key-1")
        self.assertEqual(again.job_id, first.job_id)
        self.assertEqual(len(self.sbatch_calls()), 2)
        self.assertEqual(len(self.held()), 2)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM execution_reservations"), [{"n": 2}])

    def test_invalid_limits_are_refused(self):
        with self.assertRaises(ValueError):
            AdmissionLimits(active_per_user=0)
        with self.assertRaises(ValueError):
            ExecutionRegistry(self.db).begin_attempt(
                "0" * 32,
                endpoint=mock.Mock(),
                principal=mock.Mock(binding_id=None, binding_version=None),
                account="proj",
                slurm_account_id=1,
                slurm_account_origin="explicit",
                options={},
                reservation=Reservation(),
            )


class ConcurrencyTests(_LimitsBase):
    def test_concurrent_submissions_on_two_replicas_never_oversubscribe(self):
        self.hpc.hold_jobs("RUNNING")
        replica = self.limited_admission(db=self.open_database())
        stubs = [self.stub, self.serve(replica)]
        tokens = [self.token(self.member) for _ in range(8)]
        barrier = threading.Barrier(len(tokens))
        outcomes: list[object] = [None] * len(tokens)

        def submit(index):
            barrier.wait()
            try:
                outcomes[index] = self.batch(stub=stubs[index % 2], token=tokens[index]).job_id
            except grpc.RpcError as exc:
                outcomes[index] = cast(grpc.Call, exc).code()

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(len(tokens))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        admitted = [o for o in outcomes if isinstance(o, int)]
        self.assertEqual(len(admitted), LIMITS.active_per_user, outcomes)
        self.assertEqual(
            [o for o in outcomes if not isinstance(o, int)],
            [grpc.StatusCode.RESOURCE_EXHAUSTED] * (len(tokens) - LIMITS.active_per_user),
        )
        self.assertEqual(len(self.sbatch_calls()), LIMITS.active_per_user)
        self.assertEqual(len(self.held()), LIMITS.active_per_user)

    def test_concurrent_retries_of_one_key_submit_once(self):
        self.hpc.hold_jobs("RUNNING")
        replica = self.limited_admission(db=self.open_database())
        stubs = [self.stub, self.serve(replica)]
        barrier = threading.Barrier(6)
        results: list[object] = []

        def submit(index):
            barrier.wait()
            try:
                results.append(self.batch(stub=stubs[index % 2], x_nest_idempotency_key="k").job_id)
            except grpc.RpcError as exc:
                results.append(cast(grpc.Call, exc).code())

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(len(self.sbatch_calls()), 1)
        self.assertEqual(len(self.held()), 1)
        # Losers of the race are answered with the job, or told it is in flight.
        job_ids = {r for r in results if isinstance(r, int)}
        self.assertLessEqual(len(job_ids), 1)
        self.assertTrue(
            all(isinstance(r, int) or r == grpc.StatusCode.ABORTED for r in results), results
        )


class OutageTests(_LimitsBase):
    def test_admission_data_unavailable_fails_closed(self):
        with mock.patch(
            "nest.db.executions._reserve", side_effect=psycopg.OperationalError("db gone")
        ):
            error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.UNAVAILABLE)
        self.assertEqual(dict(error.trailing_metadata())["nest-error-kind"], "database_unavailable")
        self.assert_nothing_sent()
        # The attempt was rolled back with its reservation.
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.sql("SELECT count(*) AS n FROM execution_reservations"), [{"n": 0}])

    def test_policy_outage_at_authentication_fails_closed(self):
        with mock.patch.object(
            self.access.sessions, "authenticate", side_effect=psycopg.OperationalError("down")
        ):
            error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.UNAVAILABLE)
        self.assert_nothing_sent()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM executions"), [{"n": 0}])


class LifecycleTests(_LimitsBase):
    ssh_timeout = 2.0

    def test_unknown_outcome_keeps_its_reservation_until_reconciled(self):
        self.hpc.hang_ssh("sbatch --parsable", after=True)
        error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.UNKNOWN)
        (self.hpc.state / "ssh-fault.json").unlink()
        [held] = self.held()
        self.assertEqual(held["status"], "unknown")
        # A restarted Controller (a new pool and admission) still counts it.
        restarted = self.limited_admission(db=self.open_database())
        RecoverySweeper(
            restarted, timedelta(seconds=1), interval=3600, settle_after=timedelta(0)
        ).sweep()
        self.assertEqual(len(self.held()), 1)
        self.hpc.hold_jobs("RUNNING")
        self.batch()
        error = self.refused(self.batch)
        self.assertIn("limit_active_per_user", error.details())

        # Reconciling it as not submitted releases it.
        code, _, err = self.nest_db(
            "execution", "reconcile", str(held["attempt_id"]), "--not-submitted"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.held()), 1)
        self.batch()

    def test_restart_while_submitting_keeps_the_reservation(self):
        self.hpc.hold_jobs("RUNNING")
        request = SubmissionRequest("batch", workdir=self.batch_dir(), script="hello.sbatch")
        principal = Principal.from_session(self.sessions_for(self.member))
        self.admission.admit(principal, self.project, request, execution_id=uuid.uuid4().hex)
        # The Controller stops before sbatch. Recovery marks the attempt unknown.
        restarted = self.limited_admission(db=self.open_database())
        self.sql("UPDATE execution_attempts SET updated_at = now() - interval '1 hour'")
        restarted.recover(timedelta(minutes=5))
        [held] = self.held()
        self.assertEqual(held["status"], "unknown")

    def sessions_for(self, user_id):
        _, session_id = self.session(user_id)
        [session] = [s for s in self.sessions.active_sessions(user_id) if s.id == session_id]
        return session

    def test_cancellation_releases_only_once_slurm_reports_it(self):
        self.hpc.hold_jobs("RUNNING")
        job = self.batch()
        self.stub.CancelJob(
            execution_pb2.CancelJobRequest(job_id=job.job_id), timeout=30, metadata=self.metadata()
        )
        # The request alone does not release it: the job may still be running.
        self.assertEqual(len(self.held()), 1)
        self.admission.settle(timedelta(0))
        self.assertEqual(self.held(), [])
        [attempt] = self.attempts()
        self.assertEqual(attempt["status"], "cancelled")

    def test_settle_never_releases_on_unknown_or_failed_queries(self):
        self.hpc.hold_jobs("RUNNING")
        lost = self.batch()
        failing = self.batch()
        # Slurm no longer knows `lost`; queries for `failing` cannot reach the cluster.
        (self.hpc.state / f"job-{lost.job_id}.state").unlink()
        self.assertEqual(self.admission.settle(timedelta(0)), 2)
        self.assertEqual(len(self.held()), 2)
        self.hpc.drop_ssh("squeue")
        self.assertEqual(self.admission.settle(timedelta(0)), 2)
        self.assertEqual(len(self.held()), 2)
        (self.hpc.state / "ssh-fault.json").unlink()
        self.finish(failing.job_id, "FAILED")
        self.admission.settle(timedelta(0))
        [left] = self.held()
        self.assertEqual(left["status"], "submitted")
        # A recently changed attempt, or one an RPC here waits on, is left alone.
        self.assertEqual(self.admission.settle(timedelta(hours=1)), 0)

    def test_execute_timeout_keeps_the_job_reserved(self):
        self.hpc.hold_jobs("RUNNING")
        backend = SlurmBackend(
            SlurmConfig(
                ssh_host=FakeHpc.host,
                account="legacy-acct",
                partition="dev",
                time="00:05:00",
                work_root=f"{self.hpc.base}/jobs",
                worker_workdir=str(self.hpc.base),
                ssh_user="nestop",
                allowed_hosts=(FakeHpc.host,),
                ssh_timeout=10,
                result_timeout=1,
                poll_interval=0.1,
            )
        )
        admission = RegistryAdmission(
            self.db, backend, self.profile, self.platform_id, audit=self.audit, limits=LIMITS
        )
        self.stub = self.serve(admission, backend)
        error = self.refused(self.execute)
        self.assertIn(error.code(), (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNKNOWN))
        [held] = self.held()
        self.assertEqual(held["status"], "submitted")

    def test_client_gone_before_sbatch_releases_and_sends_nothing(self):
        with mock.patch("nest.controller._still_wanted", return_value=False):
            error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.CANCELLED)
        self.assert_nothing_sent()
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["error_kind"]), ("failed", "client_gone"))
        self.assertEqual(self.held(), [])


class RecheckTests(_LimitsBase):
    """Grants and revocations are checked again in the transaction that reserves."""

    def admit(self, session_id, user_id=None, project=None):
        principal = Principal(user_id or self.member, "cli", frozenset({"jobs:run"}), session_id)
        request = SubmissionRequest("batch", workdir=self.batch_dir(), script="hello.sbatch")
        return self.admission.admit(
            principal, project or self.project, request, execution_id=uuid.uuid4().hex
        )

    def test_revoked_session_is_refused_at_reservation(self):
        _, session_id = self.session(self.member)
        self.identities.revoke_session(session_id)
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertEqual(caught.exception.reason, "submitter_not_authorized")
        self.assertEqual(caught.exception.code, grpc.StatusCode.PERMISSION_DENIED)
        self.assertEqual(self.attempts(), [])

    def change_before_reservation(self, change):
        """Applies `change` after the Platform and account checks, just before the attempt."""
        original = self.admission.registry.begin_attempt

        def begin_attempt(*args, **kwargs):
            change()
            return original(*args, **kwargs)

        patcher = mock.patch.object(self.admission.registry, "begin_attempt", begin_attempt)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_removed_member_is_refused_at_reservation(self):
        _, session_id = self.session(self.member)
        self.change_before_reservation(
            lambda: self.projects.remove_member(browser(self.owner), self.project, self.member)
        )
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertEqual(caught.exception.reason, "submitter_not_authorized")
        self.assertEqual(self.attempts(), [])

    def test_viewer_is_refused_at_reservation(self):
        _, session_id = self.session(self.member)
        self.change_before_reservation(
            lambda: self.projects.change_role(
                browser(self.owner), self.project, self.member, "viewer"
            )
        )
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertEqual(caught.exception.reason, "submitter_not_authorized")

    def test_session_revoked_after_authorization_is_refused(self):
        _, session_id = self.session(self.member)
        self.change_before_reservation(lambda: self.identities.revoke_session(session_id))
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertEqual(caught.exception.reason, "submitter_not_authorized")
        self.assertEqual(self.held(), [])

    def test_rotated_session_of_a_live_family_is_legitimate_reuse(self):
        refresh = hash_token(secrets.token_urlsafe())
        session = self.identities.create_session(
            self.member,
            kind="cli",
            token_hash=refresh,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        # The SDK refreshes while a call authorized with the old session is in flight.
        successor = self.sessions.rotate(
            refresh,
            new_refresh_hash=hash_token(secrets.token_urlsafe()),
            new_access_hash=hash_token(secrets.token_urlsafe()),
            refresh_ttl=3600,
            access_ttl=600,
        ).session
        session_id = session.id
        self.admit(session_id)
        self.assertEqual(len(self.held()), 1)
        # Once the family is revoked (sign-out), the rotated session no longer counts.
        self.identities.revoke_session(successor.id)
        with self.assertRaises(AdmissionRefusedError):
            self.admit(session_id)

    def test_same_session_may_submit_repeatedly(self):
        token, _ = self.session(self.member)
        self.batch(token=token)
        self.batch(token=token)
        self.assertEqual(len(self.sbatch_calls()), 2)


class EnvironmentBuildTests(_LimitsBase):
    def test_busy_build_slots_refuse_before_registration(self):
        slot = self.admission.environment_slot()
        self.addCleanup(slot.release)
        error = self.refused(
            lambda: self.stub.Execute(
                execution_pb2.ExecuteRequest(
                    payload=b"x",
                    environment=execution_pb2.EnvironmentFiles(
                        pyproject_toml=b"[project]\nname='x'\n", uv_lock=b"version = 1\n"
                    ),
                ),
                timeout=30,
                metadata=self.metadata(),
            )
        )
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertEqual(
            dict(error.trailing_metadata())["nest-error-kind"], "limit_environment_builds"
        )
        self.assert_nothing_sent()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM executions"), [{"n": 0}])
        slot.release()
        slot.release()  # idempotent
        self.admission.environment_slot().release()


class LimitConfigTests(unittest.TestCase):
    def test_defaults_are_bounded(self):
        limits = load_controller_config({}).limits
        self.assertEqual(limits, AdmissionLimits())
        self.assertEqual(limits.walltime_seconds, 86_400)
        for value in limits.summary().values():
            self.assertGreater(value, 0)

    def test_settings_are_read_and_validated(self):
        config = load_controller_config(
            {
                "NEST_LIMIT_ACTIVE_PER_USER": "3",
                "NEST_LIMIT_GPU_PER_PROJECT": "7",
                "NEST_LIMIT_WALLTIME": "2-00:00:00",
            }
        )
        self.assertEqual(config.limits.active_per_user, 3)
        self.assertEqual(config.limits.gpu_per_project, 7)
        self.assertEqual(config.limits.walltime_seconds, 2 * 86_400)
        for name, value in (
            ("NEST_LIMIT_ACTIVE_PER_USER", "0"),
            ("NEST_LIMIT_ACTIVE_PER_PROJECT", "-1"),
            ("NEST_LIMIT_CPU_PER_REQUEST", "many"),
            ("NEST_LIMIT_GPU_PER_REQUEST", "99999999"),
            ("NEST_LIMIT_WALLTIME", "UNLIMITED"),
            ("NEST_LIMIT_WALLTIME", "0"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError) as caught:
                load_controller_config({name: value})
            self.assertIn(name, str(caught.exception))

    def test_slurm_time_forms(self):
        for value, seconds in (
            ("30", 1800),
            ("30:15", 1815),
            ("01:02:03", 3723),
            ("2-03", 2 * 86_400 + 3 * 3600),
            ("1-00:30", 86_400 + 1800),
            ("1-00:00:01", 86_401),
        ):
            with self.subTest(value):
                self.assertEqual(slurm_time_seconds(value), seconds)
        for value in ("UNLIMITED", "INFINITE", "", "1:2:3:4", "-1"):
            self.assertIsNone(slurm_time_seconds(value))

    def test_request_limits_without_a_database(self):
        check_request_limits(LIMITS, Reservation(cpu=8, gpu=2, walltime_seconds=3600))
        with self.assertRaises(AdmissionLimitError) as caught:
            check_request_limits(LIMITS, Reservation(walltime_seconds=None))
        self.assertEqual(caught.exception.reason, "walltime_unbounded")
